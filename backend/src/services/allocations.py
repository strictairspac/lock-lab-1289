"""Aliquot preview / confirmation workflow.

Preview freezes a portion plan plus the assignment shown to the operator.
Confirmation is one database transaction that:

* re-checks the parent sample total (a changed total keeps the original ledger),
* locks the parent row so a transfer request cannot slip in between the check and
  the moment the parent stops accepting new requests,
* keeps every assignment the operator already saw unchanged,
* places transfer applications registered during the preview on the remaining
  portions, and
* writes the child samples, re-hooks the applications and stops the parent.

Retrying a confirmation after a dropped connection is safe: the batch is
locked, and a confirmed batch returns its stored result instead of splitting
again.
"""

import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models.entities import (
    AllocationBatch,
    Audit,
    Sample,
    Transfer,
    TransferAllocation,
)
from src.services.allocation import AllocationError, allocate, greedy


def _now():
    return datetime.now(timezone.utc)


def _sample(db, sample_id, lock=False):
    stmt = select(Sample).where(Sample.id == sample_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def _pending(db, sample_id):
    return list(
        db.scalars(
            select(Transfer)
            .where(Transfer.sample_id == sample_id, Transfer.status == 'pending')
            .order_by(Transfer.id)
        )
    )


def _portion_map(portions):
    return {p['code']: p['quantity'] for p in portions}


def _sorted_portions(codes, quantities):
    return [
        {'code': code, 'quantity': quantities[code]}
        for code in sorted(codes, key=lambda c: (-quantities[c], c))
    ]


def _build_assignments(transfers, allocation, quantities):
    assignments = []
    for transfer in transfers:
        codes = allocation.get(transfer.id, [])
        assignment = {
            'transfer_id': transfer.id,
            'transfer_code': transfer.code,
            'destination': transfer.destination,
            'quantity': transfer.quantity,
            'portions': _sorted_portions(codes, quantities),
        }
        assignments.append(assignment)
    return assignments


def _unused(batch):
    assigned = {p['code'] for a in (batch.assignments or []) for p in a['portions']}
    return [p for p in (batch.portions or []) if p['code'] not in assigned]


def _used_count(assignments):
    return sum(len(a['portions']) for a in assignments)


def _sample_view(sample):
    return {
        'id': sample.id,
        'code': sample.code,
        'title': sample.title,
        'quantity': sample.quantity,
        'location': sample.location,
        'accepting': sample.accepting,
        'parent_id': sample.parent_id,
    }


def _batch_view(batch, sample, new_transfers=None):
    unused = _unused(batch)
    return {
        'batch_id': batch.id,
        'status': batch.status,
        'sample': _sample_view(sample) if sample else None,
        'expected_quantity': batch.expected_quantity,
        'portions': batch.portions,
        'assignments': batch.assignments,
        'unused_portions': unused,
        'remainder': batch.expected_quantity - sum(p['quantity'] for p in batch.portions),
        'used_portion_count': _used_count(batch.assignments),
        'optimal': batch.optimal,
        'request_key': batch.request_key,
        'result': batch.result,
        'created_at': batch.created_at.isoformat() if batch.created_at else None,
        'confirmed_at': batch.confirmed_at.isoformat() if batch.confirmed_at else None,
        'new_pending_transfers': [
            {
                'transfer_id': t.id,
                'transfer_code': t.code,
                'destination': t.destination,
                'quantity': t.quantity,
            }
            for t in (new_transfers or [])
        ],
    }


def _validate_portions(portions, sample):
    codes = [p.code for p in portions]
    if len(set(codes)) != len(codes):
        raise HTTPException(422, '分装编号重复，请为每一份填写唯一编号')
    total = sum(p.quantity for p in portions)
    if total > sample.quantity:
        raise HTTPException(
            422,
            f'分装合计 {total} 份超过样本可用量 {sample.quantity} 份，放不下，保留原账',
        )
    return total


def preview(db, sample_id, data, user):
    sample = _sample(db, sample_id)
    if not sample:
        raise HTTPException(404, '样本不存在')
    if sample.parent_id is not None:
        raise HTTPException(422, '子样本已是分装结果，不可再次分装')
    if not sample.accepting:
        raise HTTPException(422, '原样本已停止接单，不能发起新的分装')

    _validate_portions(data.portions, sample)
    portions = [{'code': p.code, 'quantity': p.quantity} for p in data.portions]
    codes = [p['code'] for p in portions]
    existing = list(db.scalars(select(Sample.code).where(Sample.code.in_(codes))))
    if existing:
        raise HTTPException(409, f'分装编号已被样本台账占用：{"、".join(sorted(existing))}')

    quantities = _portion_map(portions)
    transfers = _pending(db, sample_id)
    demands = [(t.id, t.quantity) for t in transfers]
    try:
        allocation = allocate([(p['code'], p['quantity']) for p in portions], demands)
    except AllocationError as exc:
        raise HTTPException(422, str(exc))
    if allocation is None:
        raise HTTPException(422, '现有分装份数无法整笔安置全部待移交申请，请调整分装后重试')

    assignments = _build_assignments(transfers, allocation.plan, quantities)
    batch = AllocationBatch(
        sample_id=sample.id,
        status='preview',
        expected_quantity=sample.quantity,
        portions=portions,
        assignments=assignments,
        optimal=allocation.optimal,
        preview_transfers=[
            {
                'transfer_id': t.id,
                'quantity': t.quantity,
                'portion_codes': [p['code'] for p in a['portions']],
            }
            for t, a in zip(transfers, assignments)
        ],
        result=None,
    )
    db.add(batch)
    db.add(Audit(actor=user.email, action=f'预览分装 {sample.code} · {len(portions)} 份'))
    db.commit()
    db.refresh(batch)
    view = _batch_view(batch, sample)
    view['greedy_feasible'] = greedy(
        [(p['code'], p['quantity']) for p in portions], demands
    ) is not None
    view['method'] = '分支定界 · 最少分装点' if allocation.optimal else '分支定界 · 最优性未证明'
    return view


def detail(db, batch_id):
    batch = db.get(AllocationBatch, batch_id)
    if not batch:
        raise HTTPException(404, '分装预览不存在')
    sample = db.get(Sample, batch.sample_id)
    pinned = {a['transfer_id'] for a in (batch.assignments or [])}
    new_transfers = [] if batch.status != 'preview' else [
        t for t in _pending(db, batch.sample_id) if t.id not in pinned
    ]
    return _batch_view(batch, sample, new_transfers)


def listing(db, sample_id):
    result = []
    batches = select(AllocationBatch).where(AllocationBatch.sample_id == sample_id)
    for batch in db.scalars(batches.order_by(AllocationBatch.id.desc()).limit(50)):
        result.append(_batch_view(batch, db.get(Sample, batch.sample_id)))
    return result


def _pinned_check(db, batch, sample):
    pinned_codes = set()
    for assignment in batch.assignments or []:
        transfer = db.get(Transfer, assignment['transfer_id'])
        if (
            not transfer
            or transfer.status != 'pending'
            or transfer.sample_id != sample.id
            or transfer.quantity != assignment['quantity']
        ):
            raise HTTPException(409, '预览中的移交申请已变化，保留原账，未执行分装')
        if sum(p['quantity'] for p in assignment['portions']) != assignment['quantity']:
            raise HTTPException(409, '预览分配数量不一致，保留原账，未执行分装')
        for portion in assignment['portions']:
            if portion['code'] not in _portion_map(batch.portions):
                raise HTTPException(409, '预览分装编号已失效，保留原账，未执行分装')
            pinned_codes.add(portion['code'])
    return pinned_codes


def _fetch_children(db, codes):
    existing = list(db.scalars(select(Sample.code).where(Sample.code.in_(codes))))
    if existing:
        raise HTTPException(409, f'分装编号已被样本台账占用：{"、".join(sorted(existing))}，保留原账')


def confirm(db, batch_id, data, user):
    batch = db.get(AllocationBatch, batch_id)
    if not batch:
        raise HTTPException(404, '分装预览不存在')

    # One lock order everywhere: parent sample first, then the batch row.
    sample = _sample(db, batch.sample_id, lock=True)
    if not sample:
        raise HTTPException(404, '样本不存在')
    batch = db.scalar(
        select(AllocationBatch).where(AllocationBatch.id == batch_id).with_for_update()
    )
    if batch.status == 'confirmed':
        return batch.result
    if batch.status != 'preview':
        raise HTTPException(409, '该分装预览不可确认')

    duplicate = db.scalar(
        select(AllocationBatch).where(AllocationBatch.request_key == data.request_key)
    )
    if duplicate:
        if duplicate.id == batch.id and duplicate.status == 'confirmed':
            return duplicate.result
        raise HTTPException(409, '请求编号已用于其他分装，保留原账')

    if sample.quantity != batch.expected_quantity:
        raise HTTPException(409, '样本可用量在预览后发生变化，保留原账，未执行分装')
    if not sample.accepting:
        raise HTTPException(409, '原样本已停止接单，保留原账，未执行分装')

    pinned_codes = _pinned_check(db, batch, sample)
    pinned_ids = {a['transfer_id'] for a in batch.assignments or []}
    current = _pending(db, sample.id)
    new_transfers = [t for t in current if t.id not in pinned_ids]

    quantities = _portion_map(batch.portions)
    remaining = [(p['code'], p['quantity']) for p in batch.portions if p['code'] not in pinned_codes]
    new_allocation = None
    if new_transfers:
        try:
            new_allocation = allocate(remaining, [(t.id, t.quantity) for t in new_transfers])
        except AllocationError as exc:
            raise HTTPException(409, str(exc) + '，保留原账，未执行分装')
        if new_allocation is None:
            raise HTTPException(409, '预览期间新登记的申请无法安置，保留原账，未执行分装')

    codes = [p['code'] for p in batch.portions]
    _fetch_children(db, codes)

    assignments = [dict(a) for a in (batch.assignments or [])]
    assignments.extend(_build_assignments(new_transfers, new_allocation.plan if new_allocation else {}, quantities))

    code_owner = {}
    for assignment in assignments:
        for portion in assignment['portions']:
            code_owner[portion['code']] = assignment['transfer_id']

    children = {}
    for portion in batch.portions:
        child = Sample(
            code=portion['code'],
            title=f"{sample.title} · 分装",
            quantity=portion['quantity'],
            location=sample.location,
            parent_id=sample.id,
            allocation_batch_id=batch.id,
            accepting=False,
            transfer_id=code_owner.get(portion['code']),
        )
        db.add(child)
        db.flush()
        children[portion['code']] = child

    transfer_views = []
    for assignment in assignments:
        transfer = db.get(Transfer, assignment['transfer_id'])
        first_child = children[assignment['portions'][0]['code']]
        transfer.status = 'allotted'
        transfer.allocation_batch_id = batch.id
        transfer.source_sample_id = sample.id
        transfer.sample_id = first_child.id
        assigned = []
        for portion in assignment['portions']:
            child = children[portion['code']]
            db.add(
                TransferAllocation(
                    batch_id=batch.id,
                    transfer_id=transfer.id,
                    child_sample_id=child.id,
                    portion_code=portion['code'],
                    quantity=portion['quantity'],
                )
            )
            assigned.append(
                {
                    'code': portion['code'],
                    'quantity': portion['quantity'],
                    'child_sample_id': child.id,
                }
            )
        transfer_views.append(
            {
                'transfer_id': transfer.id,
                'transfer_code': transfer.code,
                'destination': transfer.destination,
                'quantity': transfer.quantity,
                'portions': assigned,
                'new_during_preview': transfer.id in {t.id for t in new_transfers},
            }
        )

    sample.accepting = False

    result_optimal = bool(batch.optimal) and (
        new_allocation.optimal if new_allocation is not None else True
    )
    result = {
        'batch_id': batch.id,
        'sample': _sample_view(sample),
        'expected_quantity': batch.expected_quantity,
        'portion_total': sum(p['quantity'] for p in batch.portions),
        'remainder': batch.expected_quantity - sum(p['quantity'] for p in batch.portions),
        'used_portion_count': _used_count(assignments),
        'optimal': result_optimal,
        'transfers': transfer_views,
        'new_transfer_ids': [t.id for t in new_transfers],
        'child_samples': [
            {
                'id': children[p['code']].id,
                'code': p['code'],
                'quantity': p['quantity'],
                'parent_id': sample.id,
                'transfer_id': code_owner.get(p['code']),
            }
            for p in batch.portions
        ],
        'unused_portions': [
            p for p in batch.portions if p['code'] not in code_owner
        ],
        'request_key': data.request_key,
        'method': '分支定界 · 最少分装点' if result_optimal else '分支定界 · 最优性未证明',
        'confirmed_at': _now().isoformat(),
    }
    result['checksum'] = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

    batch.status = 'confirmed'
    batch.request_key = data.request_key
    batch.confirmed_at = _now()
    batch.result = result
    db.add(Audit(actor=user.email, action=f'确认分装 {sample.code} · {batch.id}'))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(AllocationBatch).where(AllocationBatch.request_key == data.request_key)
        )
        if existing and existing.status == 'confirmed':
            return existing.result
        raise HTTPException(409, '分装编号或请求编号冲突，保留原账，未执行分装')
    return result
