"""Regression cases found during the production-oriented review."""

from datetime import date
from math import nan
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cbr_client import CBRClient
from app.cbr_reports import download_report
from app.forecast import forecast_monthly
from app.importer import put_rows
from app.database import get_db
from app.main import app
from app.models import SyncRun
from app.quality import quality_report
from app.schedule_service import refresh_lock
from app.transform import ParsedRow
from app import autosync, schedule_service
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base


def _row(value):
    return ParsedRow('code', 'Name', 'RF', 'unit', 0, 0, None, None, None,
                     None, date(2026, 8, 1), '2026-08', value)


def test_batch_duplicate_same_value_is_not_counted_twice(db):
    counts = put_rows(db, [_row(10), _row(10)], source_kind='cbr', source_url='https://www.cbr.ru/')
    db.commit()
    assert counts['inserted'] == 1
    assert counts['updated'] == 0


def test_batch_conflicting_duplicate_fails_closed(db):
    with pytest.raises(ValueError, match='Conflicting observations'):
        put_rows(db, [_row(10), _row(11)], source_kind='cbr', source_url='https://www.cbr.ru/')
    db.rollback()


def test_cbr_api_rejects_oversized_stream():
    def response(request):
        return httpx.Response(200, headers={'content-type': 'application/json'},
                              content=b' ' * (CBRClient.MAX_RESPONSE_BYTES + 1))

    with CBRClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(ValueError, match='15 MB'):
            client.get('publications')


def test_report_rejects_oversized_stream():
    def response(request):
        return httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'},
                              content=b' ' * 2_000_001)

    with pytest.raises(ValueError, match='2 MB'):
        download_report(date(2026, 8, 1), transport=httpx.MockTransport(response))


def test_single_process_import_lock_rejects_duplicate():
    with refresh_lock() as first:
        with refresh_lock() as second:
            assert first is True
            assert second is False


def test_quality_reports_monthly_scheduler_run(db):
    db.add(SyncRun(source='schedule:2026-10', status='success'))
    db.commit()
    assert quality_report(db)['last_refresh']['status'] == 'success'


def test_forecast_rejects_nonfinite_value():
    points = []
    for year in (2024, 2025):
        for month in range(1, 13):
            points.append((date(year, month, 1), 10.0))
    points[-1] = (points[-1][0], nan)
    with pytest.raises(ValueError, match='конечным'):
        forecast_monthly(points)


def test_csv_does_not_emit_spreadsheet_formula(db):
    row = _row(11)
    row = ParsedRow(row.code, row.name, row.dimension, '=1+2', row.publication_id,
                    row.dataset_id, row.measure_id, row.element_id, row.col_id,
                    row.unit_id, row.period, row.raw_period, row.value)
    put_rows(db, [row], source_kind='cbr', source_url='https://www.cbr.ru/')
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            csv_content = client.get('/api/v1/exports/observations.csv', params={'code':'code'}).text
            assert "'=1+2" in csv_content
    finally:
        app.dependency_overrides.clear()


def test_refresh_contended_run_is_reported(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(autosync, 'SessionLocal', session_factory)
    @contextmanager
    def unavailable():
        yield False
    monkeypatch.setattr(schedule_service, 'refresh_lock', unavailable)
    result = autosync.refresh_all(reason='manual_refresh')
    assert result['status'] == 'busy'
    with session_factory() as db:
        run = db.scalar(select(SyncRun))
        assert run.source == 'manual_refresh'
        assert run.status == 'busy'
    engine.dispose()
