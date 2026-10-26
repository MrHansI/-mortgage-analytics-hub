from datetime import date
from sqlalchemy import select
from app.cbr_reports import sync_report_month
from app.models import SourceArtifact, ObservationRevision
from app.transform import ParsedRow
from app.importer import put_rows


def test_records_report_checksum_and_revision(db):
    from tests.test_cbr_reports import report_html
    period=date(2026,8,1)
    first=report_html(2026,8,'22,7','380,6','4,4','86,4','10,1')
    second=report_html(2026,8,'22,7','383,6','4,4','86,4','10,1')
    sync_report_month(db,period,downloader=lambda _: first)
    sync_report_month(db,period,downloader=lambda _: second)
    files=db.scalars(select(SourceArtifact).order_by(SourceArtifact.id)).all()
    assert len(files)==2
    assert files[0].sha256!=files[1].sha256
    assert len(files[0].sha256)==64
    assert files[0].kind=='cbr_report_html'
    rev=db.scalars(select(ObservationRevision)).all()
    assert len(rev)==1
    assert rev[0].date==period
    assert rev[0].old_value==380.6 and rev[0].new_value==383.6
    assert rev[0].sync_run_id==files[1].sync_run_id


def test_no_revision_on_unchanged_observation(db):
    row=ParsedRow('unique','stat','RF','шт.',1,2,None,None,None,None,date(2026,1,1),'2026-01',2)
    put_rows(db,[row],source_kind='demo',source_url='synthetic-demo://test')
    put_rows(db,[row],source_kind='demo',source_url='synthetic-demo://test')
    db.commit()
    assert db.scalars(select(ObservationRevision)).all()==[]
