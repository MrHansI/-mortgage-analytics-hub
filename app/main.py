"""HTTP API, OpenAPI and dashboard for Mortgage Analytics Hub."""
from contextlib import asynccontextmanager
from threading import Event, Thread
import sys
import secrets
from fastapi import Request
from datetime import date, datetime, timezone
import csv
import io
import os
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .config import ADMIN_TOKEN
from .database import get_db, init_db
from .models import Observation, Series, SyncRun, SourceArtifact, ObservationRevision
from .cbr_client import CBRClient
from .importer import sync_cbr
from .cbr_reports import month_url
from .regional import SHEETS, SOURCE_KIND, classify_region
from .forecast import forecast_monthly
from .insights import summarize_volume
from .quality import quality_report
from .security import validate_settings, authorize_admin
from .autosync import refresh_if_stale, scheduler_loop, enqueue_refresh

@asynccontextmanager
async def lifespan(app):
    validate_settings()
    init_db()
    stop = Event()
    enable_auto = os.getenv('AUTO_SYNC_ON_START', '0') == '1' and 'PYTEST_CURRENT_TEST' not in os.environ
    if enable_auto:
        try:
            refresh_if_stale()
        except Exception as exc:
            # Startup should never fail due to a data provider or scheduling issue.
            print('Auto-sync startup error:', repr(exc), flush=True)
        thread = Thread(target=scheduler_loop, args=(stop,), daemon=True, name='cbr-scheduler')
        thread.start()
    try:
        yield
    finally:
        stop.set()

app = FastAPI(title="Mortgage Analytics Hub", version="0.5.1", lifespan=lifespan,
              docs_url=None if os.getenv("APP_ENV") == "production" else "/docs",
              redoc_url=None if os.getenv("APP_ENV") == "production" else "/redoc",
              description="Система интеграции с официальными API и региональными XLSX Банка России. Данные demo синтетические.")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "static")), name="static")

@app.middleware("http")
async def response_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Cache-Control', 'no-store' if request.url.path.startswith('/api/') else 'no-cache')
    return response

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).resolve().parent.parent / "static/index.html")

@app.get("/api/v1/health", tags=["System"])
def health(db: Session = Depends(get_db)):
    return {"status": "ok", "series_count": db.scalar(select(func.count()).select_from(Series)),
            "observation_count": db.scalar(select(func.count()).select_from(Observation)),
            "public_refresh":os.getenv("APP_ENV","development") != "production"}

@app.get("/api/v1/readiness", tags=["System"])
def readiness(db: Session = Depends(get_db)):
    """Database readiness, not a claim that upstream CBR is online."""
    try:
        db.execute(select(1)).scalar_one()
        return {"status":"ready", "database":"ok"}
    except Exception:
        raise HTTPException(503, "Database is not ready")

@app.get("/api/v1/data-quality", tags=["Analytics"])
def data_quality(db: Session = Depends(get_db)):
    return quality_report(db)

@app.get("/api/v1/series", tags=["Analytics"])
def list_series(query: str | None = None, source: str | None = None, limit: int = Query(200, ge=1, le=2000),
                db: Session = Depends(get_db)):
    q = select(Series).order_by(Series.source_kind == "demo", Series.name, Series.dimension)
    if query:
        q = q.where(Series.name.ilike(f"%{query}%") | Series.dimension.ilike(f"%{query}%"))
    if source:
        q = q.where(Series.source_kind == source)
    records = db.scalars(q.limit(limit)).all()
    return [{"code": s.code, "name": s.name, "unit": s.unit, "dimension": s.dimension,
             "source": s.source_kind, "source_url": s.source_url, "updated_at": s.updated_at.isoformat()}
            for s in records]

def _observations(db, code, start, end):
    series = db.scalar(select(Series).where(Series.code == code))
    if series is None:
        raise HTTPException(404, "Series not found")
    q = select(Observation).where(Observation.series_id == series.id)
    if start: q = q.where(Observation.date >= start)
    if end: q = q.where(Observation.date <= end)
    records = db.scalars(q.order_by(Observation.date)).all()
    return series, records

@app.get("/api/v1/observations", tags=["Analytics"])
def observations(code: str, start: date | None = None, end: date | None = None,
                 db: Session = Depends(get_db)):
    if start and end and start > end:
        raise HTTPException(422, "start must not be after end")
    series, records = _observations(db, code, start, end)
    return {"series": {"code": series.code, "name": series.name, "unit": series.unit,
                       "dimension": series.dimension, "source": series.source_kind,
                       "source_url": series.source_url},
            "count": len(records), "observations": [{"date": r.date.isoformat(), "value": r.value,
                                                 "period_note": r.raw_period,
                                                 "source_url": month_url(r.date) if series.source_kind in ("cbr_report", "cbr_snapshot") else series.source_url}
                                                for r in records]}

@app.get("/api/v1/summary", tags=["Analytics"])
def summary(code: str, db: Session = Depends(get_db)):
    series, rows = _observations(db, code, None, None)
    if not rows:
        raise HTTPException(404, "No observations")
    first, last = rows[0], rows[-1]
    delta = None if len(rows) < 2 or rows[-2].value == 0 else round((last.value / rows[-2].value - 1) * 100, 2)
    return {"code": code, "name": series.name, "unit": series.unit, "last_date": last.date,
            "last_value": last.value, "previous_change_pct": delta, "first_date": first.date,
            "count": len(rows), "source_url": series.source_url, "source": series.source_kind}

@app.get("/api/v1/sync-runs", tags=["System"])
def sync_runs(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return [{"id":r.id, "status":r.status, "source":r.source, "dataset_id":r.dataset_id,
             "started_at":r.started_at.isoformat(), "finished_at":r.finished_at.isoformat() if r.finished_at else None,
             "inserted":r.inserted, "updated":r.updated, "unchanged":r.unchanged, "error":"Ошибка получения или обработки статистики; детали у оператора." if r.error else None}
            for r in db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)).all()]

@app.get("/api/v1/admin/sync-runs", tags=["Admin"])
def admin_sync_runs(request: Request, x_admin_token: str | None = Header(default=None),
                    limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    if not ADMIN_TOKEN:
        raise HTTPException(403, "Configure ADMIN_TOKEN for administrative log access")
    authorize_admin(request, x_admin_token, ADMIN_TOKEN)
    return [{"id":r.id,"source":r.source,"status":r.status,
             "started_at":r.started_at.isoformat(),
             "finished_at":r.finished_at.isoformat() if r.finished_at else None,
             "error":r.error,"inserted":r.inserted,"updated":r.updated,"unchanged":r.unchanged}
            for r in db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)).all()]

@app.get("/api/v1/source-artifacts", tags=["System"])
def source_artifacts(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)):
    """Public evidence of accepted sources, without raw file or private server paths."""
    rows = db.scalars(select(SourceArtifact).order_by(SourceArtifact.id.desc()).limit(limit)).all()
    return [{"run_id":r.sync_run_id,"kind":r.kind,"retrieval":r.retrieval,
             "source_url":r.url,"sha256":r.sha256,"bytes":r.bytes_count,
             "checksum_method":r.checksum_method,"imported_at":r.imported_at.isoformat()}
            for r in rows]


@app.get("/api/v1/admin/revisions", tags=["Admin"])
def admin_revisions(request: Request, x_admin_token: str | None = Header(default=None),
                    code: str | None = None, limit: int = Query(100, ge=1, le=500),
                    db: Session = Depends(get_db)):
    if not ADMIN_TOKEN:
        raise HTTPException(403, "Configure ADMIN_TOKEN for revision audit access")
    authorize_admin(request, x_admin_token, ADMIN_TOKEN)
    q = select(ObservationRevision, Series.code).join(Series, ObservationRevision.series_id == Series.id)
    if code:
        q = q.where(Series.code == code)
    rows = db.execute(q.order_by(ObservationRevision.id.desc()).limit(limit)).all()
    return [{"series_code":series_code, "date":r.date.isoformat(), "old_value":r.old_value,
             "new_value":r.new_value, "sync_run_id":r.sync_run_id,
             "changed_at":r.changed_at.isoformat()} for r, series_code in rows]


@app.get("/api/v1/exports/observations.csv", tags=["Analytics"])
def export_csv(code: str, db: Session = Depends(get_db)):
    series, rows = _observations(db, code, None, None)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "value", "unit", "series_code", "source_url"])
    def safe_csv_text(value: str) -> str:
        """Prevent spreadsheet formula evaluation when CSV is opened in Excel."""
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
    for r in rows:
        writer.writerow([r.date.isoformat(), r.value, safe_csv_text(series.unit),
                         safe_csv_text(series.code),
                         safe_csv_text(month_url(r.date) if series.source_kind in ("cbr_report", "cbr_snapshot")
                                       else series.source_url)])
    return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=mortgage_series.csv"})


# Regional comparison endpoints: all data come from the three verified worksheets.
REGION_PREFIX = "cbr-xlsx-volume-"


def _regional_codes(sheet: str) -> str:
    keys = {item[0] for item in SHEETS.values()}
    if sheet not in keys:
        raise HTTPException(422, "sheet must be total, rub or fx")
    return REGION_PREFIX + sheet + "-"


@app.get("/api/v1/regions", tags=["Regions"])
def regions(db: Session = Depends(get_db)):
    names = db.scalars(select(Series.dimension).where(Series.source_kind == SOURCE_KIND).distinct()).all()
    result = sorted({name for name in names if classify_region(name) == "region"})
    return {"regions": result, "count": len(result), "unit": "млн руб.",
            "source_url": "https://www.cbr.ru/vfs/statistics/BankSector/Mortgage/02_11_New_loans_mortgage.xlsx"}


@app.get("/api/v1/regions/periods", tags=["Regions"])
def region_periods(sheet: str = "total", db: Session = Depends(get_db)):
    prefix = _regional_codes(sheet)
    days = db.scalars(select(Observation.date).join(Series)
        .where(Series.source_kind == SOURCE_KIND, Series.code.like(prefix + "%"))
        .distinct().order_by(Observation.date)).all()
    return {"sheet": sheet, "periods": [d.isoformat() for d in days]}


@app.get("/api/v1/regions/ranking", tags=["Regions"])
def region_ranking(sheet: str = "total", period: date | None = None,
                   limit: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)):
    prefix = _regional_codes(sheet)
    base = (Series.source_kind == SOURCE_KIND, Series.code.like(prefix + "%"))
    if period is None:
        period = db.scalar(select(func.max(Observation.date)).join(Series).where(*base))
    if period is None:
        return {"sheet": sheet, "period": None, "unit": "млн руб.", "ranking": [], "country_total": None}
    if period.day != 1:
        raise HTTPException(422, "period must be first day of month")
    items = db.execute(select(Series.dimension, Observation.value).join(Observation)
                       .where(*base, Observation.date == period)).all()
    country = next((v for name,v in items if classify_region(name) == "country"), None)
    ranking = sorted(((name, value) for name, value in items if classify_region(name) == "region"),
                     key=lambda x: (-x[1], x[0]))[:limit]
    return {"sheet": sheet, "period": period.isoformat(), "unit": "млн руб.",
            "country_total": country,
            "ranking": [{"region": name, "value": value,
                         "share_pct": round(value / country * 100, 2) if country else None}
                        for name, value in ranking]}


@app.get("/api/v1/regions/compare", tags=["Regions"])
def region_compare(regions: list[str] = Query(default=[]), sheet: str = "total",
                   start: date | None = None, end: date | None = None,
                   db: Session = Depends(get_db)):
    prefix = _regional_codes(sheet)
    if len(regions) == 0 or len(regions) > 5 or len(set(regions)) != len(regions):
        raise HTTPException(422, "Choose 1 to 5 different regions")
    if any(len(x) > 150 or not x.strip() for x in regions):
        raise HTTPException(422, "Invalid region name")
    if start and end and start > end:
        raise HTTPException(422, "start must not exceed end")
    q = select(Series).where(Series.source_kind == SOURCE_KIND,
                            Series.code.like(prefix + "%"), Series.dimension.in_(regions))
    found = {s.dimension: s for s in db.scalars(q).all()}
    if len(found) != len(regions) or any(classify_region(s) != "region" for s in regions):
        raise HTTPException(404, "One or more regional series not found")
    result = []
    for name in regions:
        s = found[name]
        q_obs = select(Observation).where(Observation.series_id == s.id)
        if start: q_obs = q_obs.where(Observation.date >= start)
        if end: q_obs = q_obs.where(Observation.date <= end)
        points = db.scalars(q_obs.order_by(Observation.date)).all()
        result.append({"region": name,
                       "observations": [{"date": p.date.isoformat(), "value": p.value} for p in points]})
    return {"sheet": sheet, "unit": "млн руб.", "series": result}

@app.get("/api/v1/regions/insights", tags=["Regions"])
def region_insights(sheet: str = "total", region: str = "РОССИЙСКАЯ ФЕДЕРАЦИЯ",
                    through: date | None = None, db: Session = Depends(get_db)):
    prefix = _regional_codes(sheet)
    row = db.scalar(select(Series).where(Series.source_kind == SOURCE_KIND,
                    Series.code.like(prefix + "%"), Series.dimension == region))
    if row is None:
        raise HTTPException(404, "Regional data not imported or region not found")
    query = select(Observation.date, Observation.value).where(Observation.series_id == row.id)
    if through:
        if through.day != 1:
            raise HTTPException(422, "through must be the first day of a month")
        query = query.where(Observation.date <= through)
    points = db.execute(query.order_by(Observation.date)).all()
    return {"region": region, "sheet":sheet, "source_url":row.source_url,
            **summarize_volume(list(points))}


@app.get("/api/v1/forecast", tags=["Forecast"])
def get_forecast(sheet: str = "total", region: str = "РОССИЙСКАЯ ФЕДЕРАЦИЯ",
                 months: int = Query(3, ge=1, le=6),
                 scenario_pct: float = Query(0, ge=-15, le=15),
                 db: Session = Depends(get_db)):
    prefix = _regional_codes(sheet)
    series = db.scalar(select(Series).where(Series.source_kind == SOURCE_KIND,
                       Series.code.like(prefix + "%"), Series.dimension == region))
    if series is None:
        raise HTTPException(404, "Для прогноза сначала импортируйте региональный XLSX Банка России")
    points = db.execute(select(Observation.date, Observation.value)
                        .where(Observation.series_id == series.id).order_by(Observation.date)).all()
    try:
        result = forecast_monthly(list(points), horizon=months, scenario_pct=scenario_pct)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    last_observed = points[-1][0]
    today = datetime.now(timezone.utc).date()
    data_age_months = (today.year - last_observed.year)*12 + today.month - last_observed.month
    return {"region":region, "sheet":sheet, "unit":series.unit,
            "source_url":series.source_url, "is_official_forecast":False,
            "last_observed_month":last_observed.isoformat(),
            "data_age_months":data_age_months,
            "stale_data_warning": "Исторический ряд устарел для текущего прогноза; результаты требуют ручной проверки." if data_age_months > 3 else None,
            "history":[{"date":d.isoformat(),"value":v} for d,v in points[-36:]], **result}


@app.get("/api/v1/refresh/status", tags=["System"])
def refresh_status(db: Session = Depends(get_db)):
    rows = db.scalars(select(SyncRun).where(SyncRun.source.in_(['auto_refresh','manual_refresh']))
                      .order_by(SyncRun.id.desc()).limit(1)).all()
    r = rows[0] if rows else None
    return {"state":r.status if r else "not_started",
            "last_start":r.started_at.isoformat() if r else None,
            "last_finish":r.finished_at.isoformat() if r and r.finished_at else None,
            "last_error":"Ошибка синхронизации; детали у оператора." if r and r.error else None,
            "run_id":r.id if r else None}


@app.post("/api/v1/refresh", tags=["Admin"], status_code=202)
def request_refresh(request: Request, x_admin_token: str | None = Header(default=None)):
    if "application/json" not in request.headers.get("content-type", "").lower():
        raise HTTPException(415, "application/json required")
    authorize_admin(request, x_admin_token, ADMIN_TOKEN)
    if not enqueue_refresh():
        return {"status":"already_running", "message":"Синхронизация уже выполняется"}
    return {"status":"started", "message":"Фоновая синхронизация запущена"}


@app.get("/api/v1/cbr/publications", tags=["Bank of Russia"])
def cbr_publications():
    try:
        with CBRClient() as cbr: return cbr.publications()
    except Exception as exc:
        raise HTTPException(502, f"CBR currently unavailable: {type(exc).__name__}") from exc

@app.get("/api/v1/cbr/datasets/{publication_id}", tags=["Bank of Russia"])
def cbr_datasets(publication_id: int):
    try:
        with CBRClient() as cbr: return cbr.datasets(publication_id)
    except Exception as exc:
        raise HTTPException(502, f"CBR currently unavailable: {type(exc).__name__}") from exc

@app.get("/api/v1/cbr/measures/{dataset_id}", tags=["Bank of Russia"])
def cbr_measures(dataset_id: int):
    try:
        with CBRClient() as cbr: return cbr.measures(dataset_id)
    except Exception as exc:
        raise HTTPException(502, f"CBR currently unavailable: {type(exc).__name__}") from exc

@app.post("/api/v1/admin/sync", tags=["Admin"])
def admin_sync(publication_id: int, dataset_id: int, from_year: int, to_year: int,
               measure_id: int = -1, request: Request = None,
               x_admin_token: str | None = Header(default=None), db: Session = Depends(get_db)):
    if not ADMIN_TOKEN:
        raise HTTPException(403, "Configure ADMIN_TOKEN for direct imports")
    authorize_admin(request, x_admin_token, ADMIN_TOKEN)
    try:
        with CBRClient() as cbr:
            return sync_cbr(db, cbr, publication_id, dataset_id, from_year, to_year, measure_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"CBR import failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
