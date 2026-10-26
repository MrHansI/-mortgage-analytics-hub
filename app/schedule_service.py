"""DB-audited monthly scheduler with retry and PostgreSQL advisory lock.

Only one scheduler service should be deployed. On PostgreSQL the advisory lock
is a second line of defense if two schedulers are accidentally started.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from threading import Lock
import time
from sqlalchemy import select, text
from .database import SessionLocal
from .models import SyncRun, utcnow

LOCK_ID = 613228041
REFRESH_LOCK_ID = 613228042
RETRY_INTERVAL = timedelta(hours=6)
_LOCAL_REFRESH_LOCK = Lock()


def is_due(now: datetime, last: SyncRun | None) -> bool:
    now = now.astimezone(timezone.utc)
    if now.day < 5 or (now.day == 5 and now.hour < 8):
        return False
    if last is None:
        return True
    if last.status == 'success':
        return False
    last_time = last.finished_at or last.started_at
    return now.replace(tzinfo=None) - last_time >= RETRY_INTERVAL


@contextmanager
def scheduled_lock(db, *, lock_id: int = LOCK_ID):
    """PostgreSQL lock is held for the complete import, released even on exception."""
    if db.bind.dialect.name != 'postgresql':
        # SQLite is supported for local single-process development only.
        yield True
        return
    connection = db.connection()
    acquired = bool(connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': lock_id}).scalar())
    try:
        yield acquired
    finally:
        if acquired:
            connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lock_id})
            db.commit()


@contextmanager
def refresh_lock():
    """Prevent API refreshes and the scheduler from importing simultaneously.

    PostgreSQL locks work across containers; the local lock covers SQLite's
    documented single-process development mode.
    """
    with SessionLocal() as db:
        if db.bind.dialect.name == 'postgresql':
            with scheduled_lock(db, lock_id=REFRESH_LOCK_ID) as acquired:
                yield acquired
        else:
            acquired = _LOCAL_REFRESH_LOCK.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    _LOCAL_REFRESH_LOCK.release()


def run_due(*, now: datetime | None = None, targets='config/targets.json',
            refresh=None, target_sync=None) -> dict:
    from .autosync import refresh_all
    from .cli import sync_targets
    refresh = refresh or refresh_all
    target_sync = target_sync or sync_targets
    now = now or datetime.now(timezone.utc)
    stamp = f'schedule:{now.year}-{now.month:02d}'
    # Hold PostgreSQL lock in a separate connection while ETL uses other sessions.
    with SessionLocal() as lease:
        with scheduled_lock(lease) as acquired:
            if not acquired:
                return {'status':'locked'}
            with SessionLocal() as db:
                last = db.scalar(select(SyncRun).where(SyncRun.source == stamp)
                                 .order_by(SyncRun.id.desc()).limit(1))
                if not is_due(now, last):
                    return {'status':'not_due'}
                run = SyncRun(source=stamp, status='running')
                db.add(run); db.commit()
                run_id = run.id
            errors=[]
            try:
                result = refresh(reason='schedule')
                if result['status'] != 'success':
                    errors.append(f"ETL: {result['status']}: "+'; '.join(result.get('errors', []))[:400])
            except Exception as exc:
                errors.append(f'ETL: {type(exc).__name__}: {str(exc)[:200]}')
            try:
                target_sync(targets)
            except Exception as exc:
                errors.append(f'Data Service: {type(exc).__name__}: {str(exc)[:200]}')
            with SessionLocal() as db:
                run = db.get(SyncRun, run_id)
                run.status = 'partial' if errors else 'success'
                run.error = '; '.join(errors)[:1200] if errors else None
                run.finished_at = utcnow()
                db.commit()
            return {'status':'partial' if errors else 'success', 'run_id':run_id, 'errors':errors}


def serve_forever(targets: str):
    from .database import init_db
    init_db()
    print('Monthly CBR scheduler: from day 5 at 08:00 UTC; retry incomplete work every 6h',flush=True)
    while True:
        try:
            print(run_due(targets=targets),flush=True)
        except Exception as exc:
            # Avoid terminating worker on transient SQL or upstream issues.
            print('Scheduler error: '+repr(exc),flush=True)
        time.sleep(3600)
