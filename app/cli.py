"""Local commands: discover, sync, demo, schedule."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path
from sqlalchemy import select
from .database import SessionLocal, init_db
from .models import SyncRun, utcnow
from .cbr_client import CBRClient
from .importer import put_rows, sync_cbr
from .transform import ParsedRow
from .cbr_reports import months_between, sync_report_month
from .cbr_workbooks import fetch_workbook, inspect_workbook, CBR_BOOKS
from .verified_snapshot import seed_verified
from .regional import parse_volume_workbook, sync_volume_workbook


def discover(query: str):
    query = query.casefold()
    matched = 0
    with CBRClient() as cbr:
        for pub in cbr.publications():
            name = str(pub.get("category_name") or "")
            if pub.get("NoActive") and "кредит" not in name.casefold():
                continue
            try:
                datasets = cbr.datasets(int(pub["id"]))
            except Exception as exc:
                print(f"WARN: catalog {pub.get('id')}: {exc}", file=sys.stderr)
                continue
            for d in datasets:
                title = str(d.get("full_name") or d.get("name") or "")
                if query in title.casefold():
                    matched += 1
                    print(f"publication_id={pub['id']} dataset_id={d['id']} type={d.get('type')} | {title[:170]}")
    print(f"Found: {matched}")


def demo(file_path: str):
    p = Path(file_path)
    fixture = json.loads(p.read_text(encoding="utf-8"))
    if fixture.get("data_type") != "synthetic_demo":
        raise ValueError("Demo must be explicitly labeled synthetic_demo")
    rows = []
    from datetime import date
    for s in fixture["series"]:
        for entry in s["points"]:
            rows.append(ParsedRow(s["code"], s["name"], s["dimension"], s["unit"], 0, 0,
                                  None, None, None, None, date.fromisoformat(entry[0]), entry[0], float(entry[1])))
    init_db()
    with SessionLocal() as db:
        counters = put_rows(db, rows, source_kind="demo", source_url="synthetic-demo://fixtures/demo.json")
        db.add(SyncRun(status="success", source="demo", finished_at=utcnow(), inserted=counters["inserted"],
                       updated=counters["updated"], unchanged=counters["unchanged"]))
        db.commit()
    print("DEMO ONLY — fictitious values, not Bank of Russia data", counters)


def sync_one(p, d, y1, y2, m=-1):
    init_db()
    with SessionLocal() as db, CBRClient() as cbr:
        print(sync_cbr(db, cbr, p, d, y1, y2, m))


def _month_arg(value: str) -> date:
    try:
        return date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM") from exc


def sync_reports(start: date, end: date):
    init_db()
    failures = 0
    with SessionLocal() as db:
        for period in months_between(start, end):
            try:
                print("CBR overview:", sync_report_month(db, period), flush=True)
            except Exception as exc:
                failures += 1
                print(f"ERROR {period:%Y-%m}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError(f"Failed to import {failures} monthly report(s); check sync-runs")


def sync_regions_online(out="downloads/cbr_volume.xlsx"):
    """Get the newest official workbook, atomically ingest it, then replace cache."""
    destination = Path(out)
    pending = destination.with_name("." + destination.name + ".pending.xlsx")
    try:
        fetch_workbook("volume", pending)
        init_db()
        with SessionLocal() as db:
            result = sync_volume_workbook(db, pending, retrieval='online')
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending, destination)
        return result
    finally:
        pending.unlink(missing_ok=True)


def load_targets(filename):
    config = json.loads(Path(filename).read_text(encoding="utf-8"))
    targets = config.get("targets", [])
    for t in targets:
        for k in ("publication_id", "dataset_id"):
            if k not in t: raise ValueError(f"Missing {k} in configured target")
    return targets


def sync_targets(filename):
    targets = load_targets(filename)
    if not targets:
        print("No configured targets. Discover official IDs and edit config/targets.json")
        return
    current_year = datetime.now(timezone.utc).year
    failures = 0
    for t in targets:
        try:
            sync_one(t["publication_id"], t["dataset_id"], t.get("from_year", current_year-2),
                     current_year, t.get("measure_id", -1))
        except Exception as exc:
            failures += 1
            print(f"ERROR target {t}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError(f"{failures} configured targets could not be synchronized")


def schedule(filename):
    from .schedule_service import serve_forever
    serve_forever(filename)


def main():
    parser = argparse.ArgumentParser(description="Mortgage Analytics Hub ETL")
    sub = parser.add_subparsers(dest="command", required=True)
    discover_cmd = sub.add_parser("discover", help="Search CBR catalogs for matching indicators")
    discover_cmd.add_argument("--query", default="ипотеч")
    sync_cmd = sub.add_parser("sync", help="Sync selected, verified CBR dataset")
    sync_cmd.add_argument("--publication-id", type=int, required=True)
    sync_cmd.add_argument("--dataset-id", type=int, required=True)
    sync_cmd.add_argument("--from-year", type=int, default=datetime.now(timezone.utc).year-2)
    sync_cmd.add_argument("--to-year", type=int, default=datetime.now(timezone.utc).year)
    sync_cmd.add_argument("--measure-id", type=int, default=-1)
    demo_cmd = sub.add_parser("demo", help="Load clearly marked synthetic dashboard examples")
    demo_cmd.add_argument("--file", default="fixtures/demo.json")
    seed_cmd = sub.add_parser("seed-official", help="Load locally verified June-August 2026 CBR figures; no network")
    seed_cmd.add_argument("--file", default="fixtures/cbr_verified_2026.json")
    reports_cmd = sub.add_parser("sync-reports", help="Import official CBR monthly mortgage overview HTML")
    reports_cmd.add_argument("--from-month", type=_month_arg, required=True, help="YYYY-MM")
    reports_cmd.add_argument("--to-month", type=_month_arg, required=True, help="YYYY-MM")
    download_cmd = sub.add_parser("download-xlsx", help="Get an official regional CBR XLSX file")
    download_cmd.add_argument("--kind", choices=sorted(CBR_BOOKS), required=True)
    download_cmd.add_argument("--out", default=None)
    online_cmd = sub.add_parser("sync-regions-online", help="Download then atomically import latest official XLSX")
    online_cmd.add_argument("--out", default="downloads/cbr_volume.xlsx")
    import_cmd = sub.add_parser("import-regions", help="Atomic import of the reviewed CBR volume XLSX schema")
    import_cmd.add_argument("--file", default="downloads/cbr_volume.xlsx")
    verify_cmd = sub.add_parser("validate-regions", help="Validate regional XLSX without changing the database")
    verify_cmd.add_argument("--file", default="downloads/cbr_volume.xlsx")
    preview_cmd = sub.add_parser("inspect-xlsx", help="Inspect worksheets and headers without importing guesses")
    preview_cmd.add_argument("--file", required=True)
    preview_cmd.add_argument("--rows", type=int, default=18)
    preview_cmd.add_argument("--cols", type=int, default=12)
    for cmd in ("sync-config", "schedule", "schedule-once"):
        p = sub.add_parser(cmd)
        p.add_argument("--config", default="config/targets.json")
    args = parser.parse_args()
    if args.command == "discover": discover(args.query)
    elif args.command == "sync": sync_one(args.publication_id, args.dataset_id, args.from_year, args.to_year, args.measure_id)
    elif args.command == "demo": demo(args.file)
    elif args.command == "seed-official":
        init_db()
        with SessionLocal() as db: print("VERIFIED CBR SNAPSHOT (not a live download):", seed_verified(db, args.file))
    elif args.command == "sync-reports": sync_reports(args.from_month, args.to_month)
    elif args.command == "download-xlsx":
        out = args.out or f"downloads/cbr_{args.kind}.xlsx"
        print("CBR file:", fetch_workbook(args.kind, out))
        print(json.dumps(inspect_workbook(out, 8, 8), ensure_ascii=False, indent=2))
    elif args.command == "sync-regions-online":
        print(json.dumps(sync_regions_online(args.out), ensure_ascii=False, indent=2))
    elif args.command == "validate-regions":
        rows, diag = parse_volume_workbook(args.file)
        print(json.dumps({"validated": True, "observations": len(rows), **diag}, ensure_ascii=False, indent=2))
    elif args.command == "import-regions":
        init_db()
        with SessionLocal() as db:
            result = sync_volume_workbook(db, args.file)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "inspect-xlsx":
        print(json.dumps(inspect_workbook(args.file, args.rows, args.cols), ensure_ascii=False, indent=2))
    elif args.command == "sync-config": sync_targets(args.config)
    elif args.command == "schedule": schedule(args.config)
    elif args.command == "schedule-once":
        from .schedule_service import run_due
        init_db()
        print(json.dumps(run_due(targets=args.config), ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
