"""HTTP-level checks through the FastAPI app (SQLite-backed)."""

import os
import sys
import tempfile

os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('SEED_DEMO', 'false')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.database import Base, Session, engine
from src.main import app
from src.models.entities import Sample, Transfer
from src.utils.security import password_hash


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        from src.models.entities import User
        db.add(User(email='operator@qingyu.test', password=password_hash('Demo2026!'), role='operator'))
        db.add(User(email='viewer@qingyu.test', password=password_hash('Demo2026!'), role='viewer'))
        parent = Sample(code='QY-H', title='饮用水复测留样', quantity=60, location='冷藏间 A-07')
        db.add(parent)
        db.commit()
        db.add(Transfer(code='MOVE-H1', sample_id=parent.id, destination='理化中心', quantity=15, note=''))
        db.commit()
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(engine)


def login(client, email='operator@qingyu.test'):
    response = client.post('/auth/login', json={'email': email, 'password': 'Demo2026!'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def sample_id(code='QY-H'):
    with Session() as db:
        return db.scalar(select(Sample).where(Sample.code == code)).id


def test_preview_and_confirm_over_http(client):
    headers = login(client)
    sid = sample_id()
    preview = client.post(
        f'/samples/{sid}/allocations/preview',
        headers=headers,
        json={'portions': [{'code': 'QY-H-01', 'quantity': 15}, {'code': 'QY-H-02', 'quantity': 45}]},
    )
    assert preview.status_code == 201, preview.text
    batch = preview.json()
    assert batch['status'] == 'preview'
    assert batch['used_portion_count'] == 1

    confirmed = client.post(
        f"/allocations/{batch['batch_id']}/confirm",
        headers=headers,
        json={'request_key': 'http-test-request-key'},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['used_portion_count'] == 1

    repeated = client.post(
        f"/allocations/{batch['batch_id']}/confirm",
        headers=headers,
        json={'request_key': 'http-test-request-key'},
    )
    assert repeated.status_code == 200
    assert repeated.json()['checksum'] == confirmed.json()['checksum']


def test_viewer_cannot_preview(client):
    headers = login(client, 'viewer@qingyu.test')
    sid = sample_id()
    response = client.post(
        f'/samples/{sid}/allocations/preview',
        headers=headers,
        json={'portions': [{'code': 'QY-H-01', 'quantity': 15}]},
    )
    assert response.status_code == 403


def test_transfer_to_stopped_parent_rejected(client):
    headers = login(client)
    sid = sample_id()
    preview = client.post(
        f'/samples/{sid}/allocations/preview',
        headers=headers,
        json={'portions': [{'code': 'QY-H-01', 'quantity': 15}, {'code': 'QY-H-02', 'quantity': 45}]},
    ).json()
    client.post(
        f"/allocations/{preview['batch_id']}/confirm",
        headers=headers,
        json={'request_key': 'http-test-key-2'},
    )
    response = client.post(
        '/transfers',
        headers=headers,
        json={'code': 'MOVE-H2', 'sample_id': sid, 'destination': '金属室', 'quantity': 5},
    )
    assert response.status_code == 422
    assert '停止接单' in response.json()['detail']


def test_confirm_conflict_when_ledger_changed(client):
    headers = login(client)
    sid = sample_id()
    preview = client.post(
        f'/samples/{sid}/allocations/preview',
        headers=headers,
        json={'portions': [{'code': 'QY-H-01', 'quantity': 15}, {'code': 'QY-H-02', 'quantity': 45}]},
    ).json()
    with Session() as db:
        db.get(Sample, sid).quantity = 50
        db.commit()
    response = client.post(
        f"/allocations/{preview['batch_id']}/confirm",
        headers=headers,
        json={'request_key': 'http-test-key-3'},
    )
    assert response.status_code == 409
    assert '保留原账' in response.json()['detail']
