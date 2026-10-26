from datetime import date
import httpx
import pytest
from sqlalchemy import func, select
from app.cbr_reports import parse_report_html, sync_report_month, month_url, download_report
from app.models import Observation, SyncRun


def report_html(year, month, port, volume, avg, count, rate):
    months = {7:("Июль", "июле"),8:("Август", "августе")}
    name, prep = months[month]
    return f"""<html><body><h1>Обзор рынка ипотечного жилищного кредитования</h1>
    <h2>{name} {year}</h2><h3>ИЖК</h3>
    <table><thead><tr><th>Портфель</th><th>Объем выдач</th><th>Средний размер</th></tr></thead>
    <tbody><tr><td>{port} трлн рублей</td><td>{volume} млрд рублей</td><td>{avg} млн рублей</td></tr></tbody></table>
    <h3>ИЖК по ДДУ</h3><p>4,3 трлн рублей 161,5 млрд рублей 5,7 млн рублей</p>
    <p>Кредитные организации в {prep} {year} года предоставили {count} тыс. рублевых ИЖК на сумму {volume} млрд рублей.</p>
    <p>В {prep} {year} года средневзвешенная ставка по ИЖК снизилась до {rate}% годовых.</p>
    </body></html>"""


def test_report_2026_august_verified_numbers():
    rows = parse_report_html(report_html(2026, 8, "22,7", "380,6", "4,4", "86,4", "10,1"), date(2026,8,1))
    found={r.code:r for r in rows}
    assert len(rows)==5
    assert found['cbr-report-rf-portfolio'].value==22.7
    assert found['cbr-report-rf-issuance'].value==380.6
    assert found['cbr-report-rf-avg_loan'].value==4.4
    assert found['cbr-report-rf-count'].value==86.4
    assert found['cbr-report-rf-rate'].value==10.1
    assert found['cbr-report-rf-portfolio'].raw_period.endswith('остаток на начало следующего месяца)')


def test_report_sync_is_idempotent_and_auditable(db):
    jul=report_html(2026,7,'22,5','363,4','4,3','85,5','10,5')
    aug=report_html(2026,8,'22,7','380,6','4,4','86,4','10,1')
    def downloader(period): return {7: jul, 8: aug}[period.month]
    a=sync_report_month(db,date(2026,7,1),downloader=downloader)
    b=sync_report_month(db,date(2026,8,1),downloader=downloader)
    c=sync_report_month(db,date(2026,8,1),downloader=downloader)
    assert (a['inserted'],b['inserted'],c['unchanged'])==(5,5,5)
    assert db.scalar(select(func.count()).select_from(Observation))==10
    runs=db.scalars(select(SyncRun)).all()
    assert [r.status for r in runs]==['success','success','success']


def test_bad_report_does_not_touch_good_data(db):
    aug=report_html(2026,8,'22,7','380,6','4,4','86,4','10,1')
    sync_report_month(db,date(2026,8,1),downloader=lambda _: aug)
    # Narrative value conflicts with headline: reject whole report and audit error.
    broken=aug.replace('на сумму 380,6 млрд', 'на сумму 520,0 млрд')
    with pytest.raises(ValueError, match='conflicts'):
        sync_report_month(db,date(2026,8,1),downloader=lambda _: broken)
    assert db.scalar(select(func.count()).select_from(Observation))==5
    assert db.scalar(select(SyncRun).order_by(SyncRun.id.desc())).status == 'error'


def test_invalid_month_and_redirect_rejected():
    with pytest.raises(ValueError):
        parse_report_html(report_html(2026,7,'22,5','363,4','4,3','85,5','10,5'),date(2026,8,1))
    assert month_url(date(2026,8,1)).endswith('/0826/')
    def res(request): return httpx.Response(302,headers={'Location':'https://example.com/bad'})
    with pytest.raises(httpx.HTTPStatusError):
        download_report(date(2026,8,1),transport=httpx.MockTransport(res))


def test_seed_official_snapshot_and_source_urls(db):
    from app.verified_snapshot import seed_verified
    from app.models import Series
    a=seed_verified(db)
    b=seed_verified(db)
    assert a['inserted']==15
    assert b['unchanged']==15
    assert db.scalar(select(func.count()).select_from(Observation)) == 15
    series=db.scalar(select(Series).where(Series.code=='cbr-report-rf-rate'))
    assert series.source_kind=='cbr_snapshot'
