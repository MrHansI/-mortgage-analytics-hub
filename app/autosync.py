"""Best-effort startup/daily refresh; import failures never destroy the last good data.

Do not use this thread scheduler with multiple uvicorn workers in production:
use one dedicated ETL worker/cron for clustered deployment.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread, Event
from sqlalchemy import select
from .database import SessionLocal, init_db
from .models import SyncRun, utcnow
from .cbr_reports import sync_report_month
from .cbr_workbooks import fetch_workbook
from .regional import sync_volume_workbook

_REFRESH_LOCK = Lock()
_LAST_JOB: Thread | None = None


def _month_shift(d: date, months: int) -> date:
    idx = d.year * 12 + d.month - 1 + months
    return date(idx // 12, idx % 12 + 1, 1)


def _refresh_all_unlocked(*, reason: str = 'manual', backfill: int = 3,
                workbook_path: str | Path = 'downloads/cbr_volume.xlsx',
                today: date | None = None) -> dict:
    """Synchronous ETL runner. Each dataset has own atomic transaction/audit record."""
    init_db()
    today = today or datetime.now(timezone.utc).date()
    # Reviews are normally published with a lag; make the lag configurable later.
    latest = _month_shift(date(today.year, today.month, 1), -2)
    if backfill < 1 or backfill > 12:
        raise ValueError('backfill must be 1–12')
    with SessionLocal() as db:
        run = SyncRun(source=reason[:30], status='running')
        db.add(run); db.commit(); job_id = run.id
    failures, counters = [], {'inserted':0, 'updated':0, 'unchanged':0}
    for i in reversed(range(backfill)):
        period = _month_shift(latest, -i)
        try:
            with SessionLocal() as db:
                result = sync_report_month(db, period)
            for key in counters: counters[key] += result.get(key, 0)
        except Exception as exc:
            failures.append(f'Обзор {period:%Y-%m}: {type(exc).__name__}: {str(exc)[:160]}')
    target = Path(workbook_path)
    pending = target.with_name('.'+target.name+'.pending.xlsx')
    try:
        # Never replace the old workbook until the new one was successfully parsed and committed.
        fetch_workbook('volume', pending)
        with SessionLocal() as db:
            result = sync_volume_workbook(db, pending, retrieval='online')
        for key in counters: counters[key] += result.get(key,0)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending.replace(target)
    except Exception as exc:
        failures.append(f'Онлайн региональный XLSX: {type(exc).__name__}: {str(exc)[:160]}')
        # If CBR is unreachable, existing official cached workbook can still
        # populate a new/empty DB (clearly marked as a cache, not a fresh pull).
        if target.is_file():
            try:
                with SessionLocal() as db:
                    result = sync_volume_workbook(db, target, retrieval='cache')
                for key in counters: counters[key] += result.get(key,0)
            except Exception as cache_exc:
                failures.append(f'Локальный XLSX: {type(cache_exc).__name__}: {str(cache_exc)[:160]}')
    finally:
        pending.unlink(missing_ok=True)
    with SessionLocal() as db:
        row = db.get(SyncRun, job_id)
        row.status = 'partial' if failures and sum(counters.values()) else 'error' if failures else 'success'
        row.error = '\n'.join(failures)[:1500] if failures else None
        row.finished_at = utcnow()
        row.inserted, row.updated, row.unchanged = [counters[k] for k in ('inserted','updated','unchanged')]
        db.commit()
    return {'run_id':job_id, 'status':row.status, **counters,'errors':failures}


def refresh_all(*, reason: str = 'manual', backfill: int = 3,
                workbook_path: str | Path = 'downloads/cbr_volume.xlsx',
                today: date | None = None) -> dict:
    from .schedule_service import refresh_lock
    with refresh_lock() as acquired:
        if not acquired:
            # Make cross-container contention visible rather than reporting a
            # successful background refresh that did no work.
            with SessionLocal() as db:
                db.add(SyncRun(source=reason[:30], status='busy', finished_at=utcnow(),
                               error='Another data refresh is in progress'))
                db.commit()
            return {'status': 'busy', 'inserted': 0, 'updated': 0,
                    'unchanged': 0, 'errors': ['Another data refresh is in progress']}
        return _refresh_all_unlocked(reason=reason, backfill=backfill,
                                     workbook_path=workbook_path, today=today)


def enqueue_refresh(reason: str = 'manual_refresh', force: bool = True) -> bool:
    """No duplicate concurrent refresh. Thread intentionally isolates HTTP from ASGI startup."""
    global _LAST_JOB
    if not _REFRESH_LOCK.acquire(blocking=False):
        return False
    def worker():
        try:
            refresh_all(reason=reason)
        finally:
            _REFRESH_LOCK.release()
    _LAST_JOB = Thread(target=worker, name='cbr-refresh', daemon=True)
    _LAST_JOB.start()
    return True


def refresh_if_stale() -> bool:
    """Refresh once per 24h, independent of reopening browser tabs."""
    with SessionLocal() as db:
        recent = db.scalar(select(SyncRun).where(SyncRun.source.in_(['auto_refresh','manual_refresh']),
                 SyncRun.finished_at.is_not(None),
                 SyncRun.finished_at >= utcnow() - timedelta(hours=24)).order_by(SyncRun.id.desc()))
    if recent:
        return False
    return enqueue_refresh('auto_refresh')


def scheduler_loop(stop: Event):
    # Initial invocation is handled separately by lifespan. Follow-up daily checks.
    while not stop.wait(24*3600):
        try:
            refresh_if_stale()
        except Exception:
            # Errors inside refresh_all are recorded in sync_runs; preflight exceptions
            # should not kill the periodic loop.
            pass
