import os, sys, tempfile
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('SEED_DEMO', 'false')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import select

from src.config.database import Base, Session, engine
from src.models.entities import Audit, Sample, Transfer
from src.services import allocations
from src.services.trials import mirror_quantity


class FakeUser:
    email = 'operator@qingyu.test'
    role = 'operator'


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as session:
        parent = Sample(code='QY-A', title='近海水质留样', quantity=120, location='冷藏间 A-03')
        session.add(parent)
        session.commit()
        session.refresh(parent)
        yield session
    Base.metadata.drop_all(engine)


def add_transfer(db, sample, code, quantity, destination='理化中心'):
    transfer = Transfer(code=code, sample_id=sample.id, destination=destination,
                        quantity=quantity, note='')
    db.add(transfer)
    db.commit()
    db.refresh(transfer)
    return transfer


def portion_rows(*pairs):
    return [{'code': code, 'quantity': quantity} for code, quantity in pairs]


def preview_input(rows):
    from src.schemas.allocation import AllocationPreviewInput
    return AllocationPreviewInput(portions=portion_rows(*rows))


def confirm_input(key='00000000-test-key'):
    from src.schemas.allocation import AllocationConfirmInput
    return AllocationConfirmInput(request_key=key)


def sample_by_code(db, code):
    return db.scalar(select(Sample).where(Sample.code == code))


def test_greedy_fails_but_exact_allocation_exists():
    portions = [('p1', 6), ('p2', 5), ('p3', 5)]
    demands = [('X', 10), ('Y', 6)]
    assert allocations.greedy(portions, demands) is None
    result = allocations.allocate(portions, demands)
    assert result is not None and result.optimal
    plan = result.plan
    quantities = dict(portions)
    assert sum(quantities[c] for c in plan['X']) == 10
    assert sum(quantities[c] for c in plan['Y']) == 6
    assert len(set(plan['X']) | set(plan['Y'])) == 3


def test_exact_allocation_minimises_portion_count():
    # Taking the largest portion first costs 3 pieces; the optimum is 2.
    portions = [('p1', 8), ('p2', 4), ('p3', 3), ('p4', 7)]
    demands = [('A', 8), ('B', 7)]
    result = allocations.allocate(portions, demands)
    assert result.plan == {'A': ['p1'], 'B': ['p4']}
    assert result.optimal
    assert sum(len(v) for v in result.plan.values()) == 2


def test_allocation_rejects_when_total_short():
    assert allocations.allocate([('a', 5)], [('A', 6)]) is None
    assert allocations.allocate([('a', 5)], [('A', 3), ('B', 3)]) is None


def test_preview_blocks_oversized_split_and_keeps_ledger(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 30)
    with pytest.raises(Exception) as exc:
        allocations.preview(db, parent.id, preview_input([('QY-A-01', 80), ('QY-A-02', 50)]), FakeUser())
    assert '放不下' in str(exc.value)
    assert sample_by_code(db, 'QY-A').quantity == 120
    assert db.scalar(select(Transfer).where(Transfer.code == 'MOVE-1')).status == 'pending'


def test_preview_shows_assignment_without_touching_ledger(db):
    parent = sample_by_code(db, 'QY-A')
    first = add_transfer(db, parent, 'MOVE-1', 30)
    second = add_transfer(db, parent, 'MOVE-2', 20)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 30), ('QY-A-02', 20), ('QY-A-03', 70)]), FakeUser())
    assert view['status'] == 'preview'
    assert view['sample']['quantity'] == 120
    assert view['sample']['accepting'] is True
    assert view['used_portion_count'] == 2
    assert view['remainder'] == 0
    assignment = {a['transfer_id']: a for a in view['assignments']}
    assert [p['code'] for p in assignment[first.id]['portions']] == ['QY-A-01']
    assert [p['code'] for p in assignment[second.id]['portions']] == ['QY-A-02']
    assert db.scalar(select(Transfer).where(Transfer.code == 'MOVE-1')).status == 'pending'
    assert not db.scalar(select(Sample).where(Sample.code == 'QY-A-01'))


def test_confirm_applies_children_rehook_and_stop_together(db):
    parent = sample_by_code(db, 'QY-A')
    first = add_transfer(db, parent, 'MOVE-1', 30)
    second = add_transfer(db, parent, 'MOVE-2', 20)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 30), ('QY-A-02', 20), ('QY-A-03', 70)]), FakeUser())
    result = allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())

    assert result['used_portion_count'] == 2
    assert result['new_transfer_ids'] == []
    db.expire_all()
    parent = db.get(Sample, parent.id)
    assert parent.accepting is False
    children = {s.code: s for s in db.scalars(select(Sample).where(Sample.parent_id == parent.id))}
    assert set(children) == {'QY-A-01', 'QY-A-02', 'QY-A-03'}
    assert children['QY-A-01'].transfer_id == first.id
    assert children['QY-A-02'].transfer_id == second.id
    assert children['QY-A-03'].transfer_id is None
    assert db.get(Transfer, first.id).status == 'allotted'
    assert db.get(Transfer, first.id).sample_id == children['QY-A-01'].id
    assert db.get(Transfer, first.id).source_sample_id == parent.id
    assert result['batch_id'] == view['batch_id']
    assert [t['transfer_code'] for t in result['transfers']] == ['MOVE-1', 'MOVE-2']


def test_confirm_is_idempotent_after_dropped_connection(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 30)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 30), ('QY-A-02', 90)]), FakeUser())
    first = allocations.confirm(db, view['batch_id'], confirm_input('00000000-retry-key'), FakeUser())
    second = allocations.confirm(db, view['batch_id'], confirm_input('00000000-retry-key'), FakeUser())
    assert second == first
    assert len(list(db.scalars(select(Sample).where(Sample.parent_id == parent.id)))) == 2
    # A fresh request key on an already confirmed batch still returns the same result.
    third = allocations.confirm(db, view['batch_id'], confirm_input('00000000-other-key'), FakeUser())
    assert third['checksum'] == first['checksum']
    assert len(list(db.scalars(select(Sample).where(Sample.parent_id == parent.id)))) == 2


def test_confirm_keeps_original_ledger_when_total_changed(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 30)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 30), ('QY-A-02', 90)]), FakeUser())
    parent.quantity = 100
    db.commit()
    with pytest.raises(Exception) as exc:
        allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    assert '保留原账' in str(exc.value)
    db.expire_all()
    assert db.get(Sample, parent.id).accepting is True
    assert not db.scalar(select(Sample).where(Sample.code == 'QY-A-01'))
    assert db.scalar(select(Transfer).where(Transfer.code == 'MOVE-1')).status == 'pending'


def test_confirm_places_new_transfer_registered_during_preview(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 30)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 30), ('QY-A-02', 20), ('QY-A-03', 70)]), FakeUser())
    late = add_transfer(db, parent, 'MOVE-9', 20, destination='金属分析室')

    detail = allocations.detail(db, view['batch_id'])
    assert [t['transfer_id'] for t in detail['new_pending_transfers']] == [late.id]

    result = allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    assert result['new_transfer_ids'] == [late.id]
    db.expire_all()
    late = db.get(Transfer, late.id)
    assert late.status == 'allotted'
    child = db.get(Sample, late.sample_id)
    assert child.code == 'QY-A-02'
    assert child.transfer_id == late.id
    assert result['used_portion_count'] == 2


def test_confirm_keeps_seen_destinations_unchanged(db):
    parent = sample_by_code(db, 'QY-A')
    first = add_transfer(db, parent, 'MOVE-1', 30)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 30), ('QY-A-02', 30), ('QY-A-03', 60)]), FakeUser())
    seen = {a['transfer_id']: [p['code'] for p in a['portions']] for a in view['assignments']}
    add_transfer(db, parent, 'MOVE-2', 30)
    result = allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    db.expire_all()
    for transfer_id, codes in seen.items():
        transfer = db.get(Transfer, transfer_id)
        for code in codes:
            assert db.scalar(select(Sample).where(Sample.code == code)).transfer_id == transfer.id
        assert transfer.id == first.id or transfer.status == 'allotted'
    mapped = {t['transfer_id']: [p['code'] for p in t['portions']] for t in result['transfers']}
    assert mapped[first.id] == seen[first.id]


def test_confirm_rejects_when_new_transfer_cannot_fit(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 30)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 30), ('QY-A-02', 90)]), FakeUser())
    add_transfer(db, parent, 'MOVE-9', 50)
    with pytest.raises(Exception) as exc:
        allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    assert '保留原账' in str(exc.value)
    db.expire_all()
    assert db.get(Sample, parent.id).accepting is True
    assert not db.scalar(select(Sample).where(Sample.code == 'QY-A-01'))
    assert db.get(Transfer, db.scalar(select(Transfer).where(Transfer.code == 'MOVE-1')).id).status == 'pending'


def test_greedy_stuck_scenario_can_be_confirmed(db):
    parent = sample_by_code(db, 'QY-A')
    # Two applications: 10 and 6; portions 6/5/5 make plain greedy give up.
    add_transfer(db, parent, 'MOVE-1', 10)
    add_transfer(db, parent, 'MOVE-2', 6)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 6), ('QY-A-02', 5), ('QY-A-03', 5)]), FakeUser())
    assert view['greedy_feasible'] is False
    result = allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    assert result['used_portion_count'] == 3
    db.expire_all()
    quantities = {}
    for transfer in db.scalars(select(Transfer).where(Transfer.status == 'allotted')):
        rows = list(db.scalars(select(Sample).where(Sample.transfer_id == transfer.id)))
        assert sum(row.quantity for row in rows) == transfer.quantity
        quantities[transfer.code] = len(rows)
    assert quantities == {'MOVE-1': 2, 'MOVE-2': 1}


def test_preview_rejects_duplicate_codes(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 10)
    with pytest.raises(Exception) as exc:
        allocations.preview(db, parent.id, preview_input([('QY-A-01', 10), ('QY-A-01', 10)]), FakeUser())
    assert '重复' in str(exc.value)


def test_audit_written_on_preview_and_confirm(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 10)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 10)]), FakeUser())
    allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    actions = [row.action for row in db.scalars(select(Audit).order_by(Audit.id))]
    assert any('预览分装' in a for a in actions)
    assert any('确认分装' in a for a in actions)


def test_solver_returns_best_found_when_budget_runs_out():
    # Many equal-size demands over many unit portions: exact proof is expensive,
    # a feasible plan is cheap. The solver must return it instead of giving up.
    portions = [(f'p{i}', (i % 7) + 1) for i in range(60)]
    demands = [(f'd{j}', 20) for j in range(10)]
    result = allocations.allocate(portions, demands, time_limit=0.05, node_limit=10_000)
    assert result is not None and result.plan
    assert sum(len(v) for v in result.plan.values()) == 40


def test_solver_proves_small_optimum():
    result = allocations.allocate([('a', 6), ('b', 5), ('c', 5)], [('X', 10), ('Y', 6)])
    assert result.optimal is True


def test_trial_mirror_uses_application_quantity_for_allotted_transfer(db):
    parent = sample_by_code(db, 'QY-A')
    add_transfer(db, parent, 'MOVE-1', 30)
    add_transfer(db, parent, 'MOVE-2', 20)
    view = allocations.preview(db, parent.id, preview_input([('QY-A-01', 20), ('QY-A-02', 10), ('QY-A-04', 20), ('QY-A-03', 70)]), FakeUser())
    allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    db.expire_all()
    multi = db.scalar(select(Transfer).where(Transfer.code == 'MOVE-1'))
    assert len(list(db.scalars(select(Sample).where(Sample.transfer_id == multi.id)))) == 2
    assert mirror_quantity(db, multi) == 30
    assert db.get(Sample, parent.id).accepting is False
