"""Import a transparent, human-verified CBR snapshot when direct CBR access is blocked.

The snapshot carries direct links to the corresponding official monthly releases.
This is not web scraping and MUST NOT be represented as a fresh online sync.
"""
import json
from datetime import date
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import select
from .models import Series
from .cbr_reports import month_url
from .importer import put_rows
from .provenance import record_artifact
from .models import SyncRun, utcnow
from .transform import ParsedRow

FIELDS = {
    "portfolio": ("Портфель ипотечных жилищных кредитов (на конец месяца)", "трлн руб."),
    "issuance": ("Объём выданных ипотечных кредитов за месяц", "млрд руб."),
    "avg_loan": ("Средний размер выданного ипотечного кредита", "млн руб."),
    "count": ("Количество выданных ипотечных кредитов за месяц", "тыс. кредитов"),
    "rate": ("Средневзвешенная ставка по выданным ипотечным кредитам", "% годовых"),
}


def seed_verified(db: Session, file_path="fixtures/cbr_verified_2026.json"):
    data = json.loads(Path(file_path).read_text(encoding="utf-8"))
    if data.get("kind") != "manually_verified_official_snapshot":
        raise ValueError("Not a verified CBR snapshot")
    if data.get("source_template") != "https://www.cbr.ru/statistics/bank_sector/mortgage/Indicator_mortgage/{mmyy}/":
        raise ValueError("Unexpected source template")
    # Never overwrite newer, remotely fetched statistics with a stale local snapshot.
    live_series = db.scalar(select(Series).where(Series.code.in_(
        [f"cbr-report-rf-{field}" for field in FIELDS]), Series.source_kind == "cbr_report"))
    if live_series is not None:
        raise ValueError("Live CBR report series already exist; do not downgrade them to an older snapshot")
    run = SyncRun(source="cbr_snapshot", status="running")
    db.add(run)
    db.commit()
    run_id = run.id
    try:
        total = {"inserted": 0, "updated": 0, "unchanged": 0, "series": 0}
        for entry in data["monthly"]:
            period = date.fromisoformat(entry["period"] + "-01")
            rows = []
            for field, (name, unit) in FIELDS.items():
                value = float(entry[field])
                if value < 0 or not __import__("math").isfinite(value):
                    raise ValueError(f"Invalid snapshot value for {field}")
                rows.append(ParsedRow(
                    code=f"cbr-report-rf-{field}", name=name, dimension="Российская Федерация",
                    unit=unit, publication_id=0, dataset_id=0,
                    measure_id=None, element_id=None, col_id=None, unit_id=None,
                    period=period, raw_period=entry["period"] + (" (остаток на начало следующего месяца)" if field == "portfolio" else ""),
                    value=value,
                ))
            counters = put_rows(db, rows, source_kind="cbr_snapshot", source_url=month_url(period), sync_run_id=run_id)
            for key in total:
                total[key] += counters[key]
        record_artifact(db, run_id=run_id, kind="cbr_snapshot_file", url=data["source_template"],
                        data=Path(file_path).read_bytes(), retrieval="local_verified")
        run.status = "success"
        run.finished_at = utcnow()
        run.inserted, run.updated, run.unchanged = total["inserted"], total["updated"], total["unchanged"]
        db.commit()
        return total
    except Exception as exc:
        db.rollback()
        run = db.get(SyncRun, run_id)
        run.status = "error"
        run.error = f"{type(exc).__name__}: {str(exc)[:450]}"
        run.finished_at = utcnow()
        db.commit()
        raise
