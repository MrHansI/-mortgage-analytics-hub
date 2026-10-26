"""Read-only data-quality diagnostics; no data corrections or invented observations.

Flags are monitoring signals, not claims that official data are incorrect.
Missing-value and staleness assessment uses source-specific time semantics.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .models import Series, Observation, SyncRun

OFFICIAL = ('cbr', 'cbr_report', 'cbr_xlsx', 'cbr_snapshot')


def _months_gap(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def quality_report(db: Session, *, today: date | None = None, series_limit: int = 5000) -> dict:
    """Compute coverage by actual periods and distinguish live vs locally seeded sources.

    Exposes only aggregate/public statistical metadata; intentionally does not
    expose server logs, tokens, configuration, environment, or user information.
    """
    today = today or datetime.now(timezone.utc).date()
    today_month = date(today.year, today.month, 1)
    data = db.execute(
        select(Series.id, Series.code, Series.name, Series.dimension, Series.source_kind,
               Series.unit, Series.source_url,
               func.min(Observation.date), func.max(Observation.date),
               func.count(Observation.id))
        .join(Observation, Series.id == Observation.series_id, isouter=True)
        .where(Series.source_kind.in_(OFFICIAL))
        .group_by(Series.id)
        .order_by(Series.code)
        .limit(series_limit + 1)
    ).all()
    truncated = len(data) > series_limit
    data = data[:series_limit]
    groups: dict[str, dict] = {}
    quality_rows = []
    # For series with missing months inspect actual monthly date sequence;
    # only calendar-monthly source types are evaluated for gaps.
    for sid, code, name, dimension, kind, unit, url, first, latest, count in data:
        monthly = kind in ('cbr_report', 'cbr_xlsx', 'cbr_snapshot')
        gap_count = None
        if monthly and first is not None:
            gap_count = max(0, _months_gap(first, latest) + 1 - count)
        age_months = _months_gap(latest, today_month) if latest else None
        # 3 calendar months is a monitoring warning, not evidence of late release.
        stale = (age_months is None or age_months > 3) if monthly else False
        flags = []
        if count == 0: flags.append('empty')
        if gap_count: flags.append('gaps')
        if stale: flags.append('stale')
        if kind == 'cbr_snapshot': flags.append('offline_snapshot')
        entry = {'code':code,'name':name,'dimension':dimension,'source':kind,
                 'unit':unit,'first_period':first.isoformat() if first else None,
                 'latest_period':latest.isoformat() if latest else None,
                 'observations':count,'missing_months':gap_count,
                 'age_months':age_months,'flags':flags,'source_url':url}
        quality_rows.append(entry)
        grp = groups.setdefault(kind, {'series':0,'observations':0,'latest_period':None,
                                       'stale_series':0,'gapped_series':0})
        grp['series'] += 1; grp['observations'] += count
        if latest and (grp['latest_period'] is None or latest.isoformat() > grp['latest_period']):
            grp['latest_period'] = latest.isoformat()
        grp['stale_series'] += int(stale)
        grp['gapped_series'] += int(bool(gap_count))
    # Cross-source comparison: mortgage report in billions and regional workbook in millions.
    report = db.execute(select(Observation.date, Observation.value).join(Series)
                        .where(Series.code == 'cbr-report-rf-issuance')).all()
    from .regional import SOURCE_KIND
    region = db.execute(select(Observation.date, Observation.value).join(Series)
                        .where(Series.source_kind == SOURCE_KIND,
                               Series.code.like('cbr-xlsx-volume-total-%'),
                               Series.dimension == 'РОССИЙСКАЯ ФЕДЕРАЦИЯ')).all()
    amounts = dict(region)
    checks = []
    for d, billion in report:
        if d not in amounts: continue
        xlsx_billion = amounts[d] / 1000.0
        relative = abs(billion - xlsx_billion) / max(abs(billion), 0.000001) * 100
        checks.append({'period':d.isoformat(),'overview_bln_rub':billion,
                       'regional_bln_rub':round(xlsx_billion, 4),
                       'difference_pct':round(relative, 2),
                       'warning':relative > 2.0})
    checks.sort(key=lambda x: x['period'], reverse=True)
    last_run = db.scalar(select(SyncRun).where(SyncRun.source.in_([
        'auto_refresh','manual_refresh']) | SyncRun.source.like('schedule:%'))
        .order_by(SyncRun.started_at.desc()).limit(1))
    issues = sum(bool(x['flags']) for x in quality_rows)
    inconsistencies = sum(x['warning'] for x in checks)
    return {'as_of':today.isoformat(), 'status':
            'no_official_data' if not quality_rows else 'attention' if issues or inconsistencies or truncated else 'ok',
            'official_series':len(quality_rows), 'groups':groups, 'flagged_series':issues,
            'cross_source_discrepancies':inconsistencies,
            'comparison_note':'Различия могут отражать методологию, охват и округление; ручная сверка обязательна.',
            'cross_source_checks':checks[:24], 'series':quality_rows,
            'truncated':truncated,
            'last_refresh':{'status':last_run.status,'started_at':last_run.started_at.isoformat(),
                            'finished_at':last_run.finished_at.isoformat() if last_run.finished_at else None}
                            if last_run else None}
