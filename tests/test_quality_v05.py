from datetime import date
from sqlalchemy import select
from app.quality import quality_report
from app.transform import ParsedRow
from app.importer import put_rows


def add(db, code, source, values, *, dimension='РОССИЙСКАЯ ФЕДЕРАЦИЯ', unit='млн руб.'):
    rows=[ParsedRow(code,'Тестовая серия',dimension,unit,0,0,None,None,None,None,date.fromisoformat(d),d,float(v)) for d,v in values]
    put_rows(db, rows, source_kind=source, source_url='https://www.cbr.ru/statistics/bank_sector/mortgage/')
    db.commit()


def test_gaps_stale_and_offline_snapshot(db):
    add(db,'cbr-report-rf-portfolio','cbr_snapshot', [('2025-01-01',10),('2025-03-01',20)])
    quality=quality_report(db,today=date(2026,10,11))
    s=quality['series'][0]
    assert s['missing_months']==1
    assert s['age_months']==19
    assert set(s['flags'])=={'stale','gaps','offline_snapshot'}
    assert quality['status']=='attention'
    assert quality['flagged_series']==1


def test_cross_source_units_and_rounding(db):
    add(db,'cbr-report-rf-issuance','cbr_report',[('2026-08-01',380.6)],unit='млрд руб.')
    add(db,'cbr-xlsx-volume-total-country','cbr_xlsx',[('2026-08-01',380600)], unit='млн руб.')
    q=quality_report(db,today=date(2026,10,11))
    assert q['cross_source_checks'][0]['difference_pct']==0
    assert q['cross_source_discrepancies']==0
    add(db,'cbr-xlsx-volume-total-country','cbr_xlsx',[('2026-08-01',400000)])
    q=quality_report(db,today=date(2026,10,11))
    assert q['cross_source_discrepancies']==1
    assert q['cross_source_checks'][0]['warning']


def test_dont_treat_generic_api_series_as_monthly(db):
    add(db,'cbr-p1-d1','cbr',[('2020-01-01',1),('2024-01-01',2)])
    r=quality_report(db,today=date(2026,10,11))['series'][0]
    assert r['missing_months'] is None
    assert not r['flags']
