from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest
from fastapi.testclient import TestClient
from app.database import Base, get_db
from app.main import app
from app.models import SyncRun
from app.security import validate_settings
from app.schedule_service import is_due
from app import schedule_service


def test_production_settings_fail_closed(monkeypatch):
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('ADMIN_TOKEN','')
    with pytest.raises(RuntimeError, match='ADMIN_TOKEN'):
        validate_settings()
    monkeypatch.setenv('ADMIN_TOKEN','x'*40)
    monkeypatch.setenv('AUTO_SYNC_ON_START','1')
    with pytest.raises(RuntimeError, match='AUTO_SYNC'):
        validate_settings()
    monkeypatch.setenv('AUTO_SYNC_ON_START','0')
    validate_settings()


def test_schedule_due_and_retry():
    now=datetime(2026,10,5,7,0,tzinfo=timezone.utc)
    assert not is_due(now,None)
    now=now.replace(hour=8)
    assert is_due(now,None)
    run=SyncRun(status='error',source='schedule:2026-10',started_at=datetime(2026,10,5,7))
    assert not is_due(now,run)
    assert is_due(now+timedelta(hours=6),run)
    run.status='success'
    assert not is_due(now+timedelta(days=4),run)


def test_scheduler_success_runs_once_and_partial_retries(monkeypatch):
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(schedule_service,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
    calls=[]
    def refresh(**kwargs):
        calls.append('refresh');return {'status':'success'}
    def targets(path):
        calls.append('targets')
    at=datetime(2026,10,11,10,0,tzinfo=timezone.utc)
    a=schedule_service.run_due(now=at, refresh=refresh,target_sync=targets)
    b=schedule_service.run_due(now=at, refresh=refresh,target_sync=targets)
    assert a['status']=='success' and b['status']=='not_due'
    assert calls==['refresh','targets']
    engine.dispose()


def test_public_sync_log_redacts_raw_exception(db):
    db.add(SyncRun(status='error',source='auto_refresh',error='password=secret internal exception'))
    db.commit()
    app.dependency_overrides[get_db]=lambda:db
    try:
        with TestClient(app) as c:
            r=c.get('/api/v1/sync-runs').json()
            assert r[0]['error'] and 'secret' not in str(r)
            assert c.get('/api/v1/admin/sync-runs').status_code==403
            assert c.get('/api/v1/data-quality').status_code==200
            assert c.get('/api/v1/readiness').status_code==200
            assert c.get('/').status_code==200
            assert c.get('/static/app.js').status_code==200
    finally:
        app.dependency_overrides.clear()
