"""Concurrency checks against a real PostgreSQL 16 server.

Run with:
    DATABASE_URL=postgresql+psycopg://locklab@localhost:17742/locklab \
        pytest tests/test_allocations_pg.py -q

The tests are skipped when the configured database is not reachable so the
SQLite suite still runs on machines without PostgreSQL.
"""

import os
import sys
import threading
import time

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.database import Base, Session, engine
from src.models.entities import Sample, Transfer
from src.services import allocations
from src.services.transfers import create as create_transfer
from src.services.samples import update as update_sample


class FakeUser:
    email = 'operator@qingyu.test'
    role = 'operator'


def _postgres():
    if not engine.url.drivername.startswith('postgresql'):
        return False
    try:
        with engine.connect():
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _postgres(), reason='needs a reachable PostgreSQL database')


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as session:
        parent = Sample(code='QY-PG', title='近海水质留样', quantity=120, location='冷藏间 A-03')
        session.add(parent)
        session.commit()
        session.refresh(parent)
        yield session
    Base.metadata.drop_all(engine)


def preview_input(pairs):
    from src.schemas.allocation import AllocationPreviewInput
    return AllocationPreviewInput(portions=[{'code': code, 'quantity': quantity} for code, quantity in pairs])


def confirm_input(key='00000000-pg-key'):
    from src.schemas.allocation import AllocationConfirmInput
    return AllocationConfirmInput(request_key=key)


def add_transfer(db, sample, code, quantity):
    transfer = Transfer(code=code, sample_id=sample.id, destination='理化中心',
                        quantity=quantity, note='')
    db.add(transfer)
    db.commit()
    db.refresh(transfer)
    return transfer


def test_transfer_registered_during_confirm_blocks_then_cannot_slip_in(db):
    """A transfer created while confirmation holds the parent lock must either
    be seen by the confirmation or be refused afterwards - never lost."""
    parent = db.scalar(select(Sample).where(Sample.code == 'QY-PG'))
    add_transfer(db, parent, 'MOVE-1', 30)
    view = allocations.preview(db, parent.id, preview_input([('QY-PG-01', 30), ('QY-PG-02', 20), ('QY-PG-03', 70)]), FakeUser())

    outcomes = {}

    def late_writer():
        with Session() as other:
            try:
                time.sleep(0.05)
                from src.schemas.transfer import TransferInput
                create_transfer(
                    other,
                    TransferInput(code='MOVE-LATE', sample_id=parent.id, destination='金属室', quantity=20),
                    FakeUser(),
                )
                outcomes['created'] = True
            except Exception as exc:  # noqa: BLE001 - assertion captures the type
                outcomes['error'] = exc

    thread = threading.Thread(target=late_writer)
    # Hold the parent row lock the same way confirm() does, then release it.
    with Session() as locker:
        locked = locker.scalar(select(Sample).where(Sample.id == parent.id).with_for_update())
        assert locked is not None
        thread.start()
        time.sleep(0.4)
        assert thread.is_alive(), 'late transfer should be blocked by the row lock'
        locker.rollback()
    thread.join(10)
    assert not thread.is_alive()

    if outcomes.get('created'):
        # The late request committed before confirmation: it must be placed.
        result = allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
        late = db.scalar(select(Transfer).where(Transfer.code == 'MOVE-LATE'))
        assert late.id in result['new_transfer_ids']
        assert late.status == 'allotted'
    else:
        # The late request found the parent stopped: nothing was lost or split.
        allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
        late = db.scalar(select(Transfer).where(Transfer.code == 'MOVE-LATE'))
        assert late.status == 'pending'
        with pytest.raises(Exception) as exc:
            from src.schemas.transfer import TransferInput
            with Session() as other:
                create_transfer(
                    other,
                    TransferInput(code='MOVE-LATE-2', sample_id=parent.id, destination='金属室', quantity=20),
                    FakeUser(),
                )
        assert '停止接单' in str(exc.value)


def test_confirm_waits_for_transfer_writer_then_places_it(db):
    """Precise interleaving: a transfer insert completes inside the preview
    window; confirmation must lock, see it, and place it without moving the
    assignments the operator already saw."""
    parent = db.scalar(select(Sample).where(Sample.code == 'QY-PG'))
    add_transfer(db, parent, 'MOVE-1', 30)
    view = allocations.preview(db, parent.id, preview_input([('QY-PG-01', 30), ('QY-PG-02', 20), ('QY-PG-03', 70)]), FakeUser())
    seen = {a['transfer_id']: [p['code'] for p in a['portions']] for a in view['assignments']}

    from src.schemas.transfer import TransferInput
    with Session() as other:
        late = create_transfer(
            other,
            TransferInput(code='MOVE-LATE', sample_id=parent.id, destination='金属室', quantity=20),
            FakeUser(),
        )
        late_id = late['id']

    result = allocations.confirm(db, view['batch_id'], confirm_input(), FakeUser())
    db.expire_all()
    late_row = db.get(Transfer, late_id)
    assert late_id in result['new_transfer_ids']
    assert late_row.status == 'allotted'
    assert result['used_portion_count'] == 2
    for transfer_id, codes in seen.items():
        mapped = next(t for t in result['transfers'] if t['transfer_id'] == transfer_id)
        assert [p['code'] for p in mapped['portions']] == codes
