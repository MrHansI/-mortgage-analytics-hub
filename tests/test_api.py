from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.main import app
from app.database import get_db
from app.importer import put_rows
from app.transform import ParsedRow
from datetime import date

def test_apis_and_export(db):
    rows=[ParsedRow("test-series","Тестовый показатель","Тестовый регион","шт.",0,0,None,None,None,None,date(2026,1,1),"2026-01-01",2),
          ParsedRow("test-series","Тестовый показатель","Тестовый регион","шт.",0,0,None,None,None,None,date(2026,2,1),"2026-02-01",5)]
    put_rows(db,rows,source_kind="demo",source_url="synthetic-demo://test")
    db.commit()
    app.dependency_overrides[get_db]=lambda: db
    try:
        with TestClient(app) as client:
            r=client.get('/api/v1/series')
            assert r.status_code==200
            assert len(r.json())==1
            r=client.get('/api/v1/summary',params={'code':'test-series'})
            assert r.status_code==200 and r.json()['previous_change_pct']==150.0
            r=client.get('/api/v1/observations',params={'code':'test-series','start':'2026-02-01'})
            assert r.status_code==200 and r.json()['count']==1
            r=client.get('/api/v1/exports/observations.csv',params={'code':'test-series'})
            assert r.status_code==200 and 'source_url' in r.text
            assert client.get('/api/v1/summary',params={'code':'unknown'}).status_code==404
            assert client.get('/api/v1/admin/sync',params={}).status_code==405
            assert client.get('/').status_code==200
    finally:
        app.dependency_overrides.clear()
