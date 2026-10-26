"""Network-free end-to-end check: report failure must be in audit log;
valid workbook must still import and produce partial status.
"""
from datetime import date
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base
from app.models import SyncRun, Observation
from app import autosync
from tests.test_regional import sample_xlsx


def test_refresh_partial_failure_is_audited(tmp_path,monkeypatch):
    eng=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(eng)
    monkeypatch.setattr(autosync,'SessionLocal',sessionmaker(bind=eng,expire_on_commit=False))
    monkeypatch.setattr(autosync,'init_db',lambda:None)
    def failed_report(*args,**kwargs):
        raise ValueError('CBR unavailable')
    monkeypatch.setattr(autosync,'sync_report_month',failed_report)
    valid=tmp_path/'valid.xlsx';sample_xlsx(valid)
    monkeypatch.setattr(autosync,'fetch_workbook',lambda kind,dest: __import__('shutil').copyfile(valid,dest))
    result=autosync.refresh_all(today=date(2026,10,10),backfill=1,workbook_path=tmp_path/'regional.xlsx')
    assert result['status']=='partial'
    assert result['inserted']==180
    assert any('CBR unavailable' in err for err in result['errors'])
    assert (tmp_path/'regional.xlsx').exists()
    with autosync.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Observation))==180
        ops=db.scalars(select(SyncRun).order_by(SyncRun.id)).all()
        assert len(ops)==2
        assert ops[-1].source=='cbr_xlsx' and ops[-1].status=='success'
    eng.dispose()
