"""Atomic/idempotent ingestion and audit trail."""
from datetime import datetime
from math import isfinite
from urllib.parse import urlsplit
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Observation, Series, SyncRun, ObservationRevision, utcnow
from .provenance import record_json_artifact
from .transform import ParsedRow, parse_data
from .cbr_client import CBRClient


def put_rows(db: Session, rows: list[ParsedRow], *, source_kind: str, source_url: str,
             sync_run_id: int | None = None) -> dict:
    if not rows:
        raise ValueError("Nothing to ingest")
    series_cache: dict[str, Series] = {}
    obs_cache: dict[str, dict] = {}
    batch_seen: dict[tuple[str, object], float] = {}
    counters = {"inserted": 0, "updated": 0, "unchanged": 0, "series": 0}
    for record in rows:
        if not isfinite(float(record.value)):
            raise ValueError("Non-finite observation rejected")
        if not record.code or not record.name or not record.unit:
            raise ValueError("Missing series metadata")
        key = (record.code, record.period)
        if key in batch_seen:
            if batch_seen[key] != record.value:
                raise ValueError(f"Conflicting observations for {record.code} / {record.period}")
            continue
        batch_seen[key] = record.value
        series = series_cache.get(record.code)
        if series is None:
            series = db.scalar(select(Series).where(Series.code == record.code))
            if series is None:
                series = Series(code=record.code, name=record.name, unit=record.unit,
                                dimension=record.dimension, publication_id=record.publication_id,
                                dataset_id=record.dataset_id, measure_id=record.measure_id,
                                element_id=record.element_id, col_id=record.col_id, unit_id=record.unit_id,
                                source_url=source_url, source_kind=source_kind)
                db.add(series)
                db.flush()
                counters["series"] += 1
            else:
                series.name = record.name
                series.unit = record.unit
                series.dimension = record.dimension
                series.source_url = source_url
                series.source_kind = source_kind
                series.updated_at = utcnow()
            series_cache[record.code] = series
            # One lookup per series, rather than one SELECT for every XLSX cell.
            # Old records are preloaded and new records are added to the cache.
            with db.no_autoflush:
                obs_cache[record.code] = {o.date: o for o in db.scalars(
                    select(Observation).where(Observation.series_id == series.id)).all()}
        existing = obs_cache[record.code].get(record.period)
        if existing is None:
            obs = Observation(series_id=series.id, date=record.period,
                              value=record.value, raw_period=record.raw_period)
            db.add(obs)
            obs_cache[record.code][record.period] = obs
            counters["inserted"] += 1
        elif existing.value != record.value:
            db.add(ObservationRevision(series_id=series.id, sync_run_id=sync_run_id,
                                       date=record.period, old_value=existing.value,
                                       new_value=record.value))
            existing.value = record.value
            existing.raw_period = record.raw_period
            existing.fetched_at = utcnow()
            counters["updated"] += 1
        else:
            counters["unchanged"] += 1
    return counters


def sync_cbr(db: Session, client: CBRClient, publication_id: int, dataset_id: int,
             from_year: int, to_year: int, measure_id: int = -1) -> dict:
    if from_year > to_year or from_year < 1990 or to_year > 2100:
        raise ValueError("Invalid year range")
    run = SyncRun(source="cbr", publication_id=publication_id, dataset_id=dataset_id)
    db.add(run)
    db.commit()
    try:
        datasets = client.datasets(publication_id)
        dataset = next((d for d in datasets if int(d["id"]) == dataset_id), None)
        if dataset is None:
            raise ValueError("Dataset does not belong to specified publication")
        actual_type = int(dataset.get("type") or 0)
        if actual_type == 1 and measure_id == -1:
            raise ValueError("This dataset requires measure_id; discover with `measures` first")
        labels = {}
        if actual_type == 1:
            labels = {int(m["id"]): str(m.get("name") or m["id"]) for m in client.measures(dataset_id)}
        payload = client.data(publication_id, dataset_id, from_year, to_year, measure_id)
        rows = parse_data(payload, publication_id, dataset, labels)
        url = str(dataset.get("link") or f"https://www.cbr.ru/dataservice/data?publicationId={publication_id}&datasetId={dataset_id}&measureId={measure_id}&y1={from_year}&y2={to_year}")
        # Do not redirect provenance to arbitrary client-provided URLs.
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in ('www.cbr.ru', 'cbr.ru')
                or parsed.username or parsed.password or parsed.port is not None):
            url = "https://www.cbr.ru/statistics/data-service/"
        counters = put_rows(db, rows, source_kind="cbr", source_url=url, sync_run_id=run.id)
        record_json_artifact(db, run_id=run.id, kind="cbr_rest", url=url, data=payload)
        run.status = "success"
        run.finished_at = utcnow()
        run.inserted, run.updated, run.unchanged = counters["inserted"], counters["updated"], counters["unchanged"]
        db.commit()
        return {"run_id": run.id, "status": run.status, **counters}
    except Exception as exc:
        db.rollback()  # no partial data ingestion survives an error
        persisted_run = db.get(SyncRun, run.id)
        persisted_run.status = "error"
        persisted_run.error = f"{type(exc).__name__}: {str(exc)[:500]}"
        persisted_run.finished_at = utcnow()
        db.commit()
        raise
