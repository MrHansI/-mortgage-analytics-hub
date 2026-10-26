from sqlalchemy import select, func
import pytest
from app.models import Observation, Series, SyncRun
from app.importer import sync_cbr

class Client:
    def __init__(self, payload):
        self.payload=payload
    def datasets(self, p):
        return [{"id":9,"name":"Ипотечные кредиты", "type":0,
                 "link":"https://www.cbr.ru/statistics/bank_sector/mortgage/"}]
    def data(self,*_):
        return self.payload

payload={"RawData":[{"date":"2026-02-01","obs_val":18,"colId":-1,"unit_id":1},
                     {"date":"2026-03-01","obs_val":20,"colId":-1,"unit_id":1}],
        "units":[{"id":1,"val":"млн руб."}]}

def test_sync_idempotent_and_updates(db):
    client=Client(payload)
    a=sync_cbr(db,client,7,9,2026,2026)
    assert a["inserted"]==2
    assert a["series"]==1
    b=sync_cbr(db,client,7,9,2026,2026)
    assert b["unchanged"]==2 and b["inserted"]==0
    client.payload={**payload,"RawData":[payload["RawData"][0],{**payload["RawData"][1],"obs_val":21}]}
    c=sync_cbr(db,client,7,9,2026,2026)
    assert c["updated"]==1
    assert db.scalar(select(func.count()).select_from(Observation))==2

def test_bad_payload_rolls_back_and_audits(db):
    client=Client(payload)
    sync_cbr(db,client,7,9,2026,2026)
    client.payload={"RawData":[{**payload["RawData"][0],"obs_val":99},{"date":"bad","obs_val":10}]}
    with pytest.raises(ValueError):
        sync_cbr(db,client,7,9,2026,2026)
    assert db.scalar(select(Observation.value).where(Observation.date==__import__('datetime').date(2026,2,1)))==18
    run=db.scalars(select(SyncRun).order_by(SyncRun.id.desc())).first()
    assert run.status=="error"

def test_unrecognized_dataset_rejected(db):
    with pytest.raises(ValueError, match="does not belong"):
        sync_cbr(db,Client(payload),7,777,2026,2026)
