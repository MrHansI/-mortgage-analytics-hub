"""Representative XLSX based on the real header/labels shown in user's CBR inspection.

Figures in generated files are TEST FIXTURES, not a claim that the file is
an official source. Real-file import must be checked on downloaded CBR XLSX.
"""
import pytest
from datetime import date
from openpyxl import Workbook, load_workbook
from fastapi.testclient import TestClient
from app.regional import parse_volume_workbook, sync_volume_workbook
from app.models import Observation, Series, SyncRun
from app.main import app
from app.database import get_db
from sqlalchemy import select, func

M = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def sample_xlsx(filename):
    wb = Workbook()
    wb.remove(wb.active)
    titles = {
        "итого": "Объем ипотечных жилищных кредитов, предоставленных физическим лицам-резидентам, млн руб.",
        "в рублях": "Объем ипотечных жилищных кредитов в рублях, предоставленных физическим лицам-резидентам, млн руб.",
        "в инвалюте": "Объем ипотечных жилищных кредитов в иностранной валюте, предоставленных физическим лицам-резидентам, млн руб.",
    }
    for sheet, title in titles.items():
        ws=wb.create_sheet(sheet)
        ws.append([])
        ws.append([title])
        ws.append([None]+[m+" 2019" for m in M])
        vals = [10] * 12 if sheet!='в инвалюте' else [0] * 12
        ws.append(["РОССИЙСКАЯ ФЕДЕРАЦИЯ"] + [v*10 for v in vals])
        ws.append(["ЦЕНТРАЛЬНЫЙ ФЕДЕРАЛЬНЫЙ ОКРУГ"] + [v*4 for v in vals])
        ws.append(["Белгородская область"] + vals)
        ws.append(["Московская область"] + [v*2 for v in vals])
        ws.append(["г. Москва"] + [v*3 for v in vals])
    wb.save(filename)


def test_parse_official_layout_and_idempotency(db, tmp_path):
    path=tmp_path/'test.xlsx';sample_xlsx(path)
    rows,diags=parse_volume_workbook(path)
    assert len(rows)==3*5*12
    assert set(diags['sheets'])=={'total','rub','fx'}
    assert diags['sheets']['total']['regions']==3
    assert any(r.dimension=='Белгородская область' and r.period==date(2019,1,1) and r.value==10 for r in rows)
    first=sync_volume_workbook(db,path)
    assert first['inserted']==180
    assert first['series']==15
    again=sync_volume_workbook(db,path)
    assert again['inserted']==again['updated']==0
    assert again['unchanged']==180
    assert db.scalar(select(func.count()).select_from(Observation))==180
    assert db.scalar(select(func.count()).select_from(SyncRun))==2


def test_bad_schema_rollback(db, tmp_path):
    file=tmp_path/'source.xlsx';sample_xlsx(file)
    sync_volume_workbook(db,file)
    wb=load_workbook(file)
    wb['итого']['B3']='Недостоверный период'
    wb.save(file)
    with pytest.raises(ValueError,match='Unknown period|Insufficient'):
        sync_volume_workbook(db,file)
    assert db.scalar(select(func.count()).select_from(Observation))==180
    assert db.scalars(select(SyncRun.status).order_by(SyncRun.id)).all()==['success','error']


def test_regions_endpoints(db, tmp_path):
    file=tmp_path/'source.xlsx';sample_xlsx(file)
    sync_volume_workbook(db,file)
    app.dependency_overrides[get_db]=lambda:db
    try:
        with TestClient(app) as client:
            resp=client.get('/api/v1/regions')
            assert resp.status_code==200 and resp.json()['count']==3
            assert 'РОССИЙСКАЯ ФЕДЕРАЦИЯ' not in resp.json()['regions']
            months=client.get('/api/v1/regions/periods?sheet=total').json()
            assert len(months['periods'])==12
            ranking=client.get('/api/v1/regions/ranking?sheet=total&period=2019-01-01').json()
            assert [r['region'] for r in ranking['ranking']]==['г. Москва','Московская область','Белгородская область']
            assert ranking['country_total']==100
            assert ranking['ranking'][0]['share_pct']==30
            compared=client.get('/api/v1/regions/compare',params=[('sheet','rub'),('regions','г. Москва'),('regions','Московская область')])
            assert compared.status_code==200
            assert len(compared.json()['series'])==2
            assert len(compared.json()['series'][0]['observations'])==12
            assert client.get('/api/v1/regions/compare',params={'regions':'РОССИЙСКАЯ ФЕДЕРАЦИЯ'}).status_code==404
            assert client.get('/api/v1/regions/ranking?sheet=bogus').status_code==422
            assert client.get('/api/v1/regions/compare',params={'regions':'г. Москва', 'start':'2020-01-01', 'end':'2019-01-01'}).status_code==422
    finally:
        app.dependency_overrides.clear()
