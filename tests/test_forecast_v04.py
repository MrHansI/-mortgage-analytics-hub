from datetime import date
from fastapi.testclient import TestClient
from app.forecast import forecast_monthly, next_month
from app.insights import summarize_volume
from app.main import app
from app.database import get_db
from app.importer import put_rows
from app.transform import ParsedRow
import pytest


def history(n=48):
    d=date(2022,1,1);out=[]
    for k in range(n):
        out.append((d, 200 + 12 * (k%12) + (k//12)*14))
        d=next_month(d)
    return out


def test_model_selection_and_positive_forecasts():
    pts=history()
    result=forecast_monthly(pts,6)
    assert result['training_count']==48
    assert len(result['forecasts'])==6
    assert result['backtest_points']==12
    assert result['forecasts'][0]['date']=='2026-01-01'
    assert all(x['high']>=x['value']>=x['low']>=0 for x in result['forecasts'])
    assert set(result['alternatives'])=={'seasonal_naive','rolling_mean','damped_trend'}
    # The forward scenario should not change which model wins backtesting.
    stress=forecast_monthly(pts,6,-3)
    assert stress['model']==result['model']
    assert stress['forecasts'][0]['value']<=result['forecasts'][0]['value']


def test_missing_month_and_insufficient_history_rejected():
    with pytest.raises(ValueError,match='не менее 24'):
        forecast_monthly(history(12))
    pts=history(25);pts.pop(10)
    with pytest.raises(ValueError,match='пропуск'):
        forecast_monthly(pts)
    with pytest.raises(ValueError,match='Горизонт'):
        forecast_monthly(history(),7)
    with pytest.raises(ValueError,match='Сценарий'):
        forecast_monthly(history(),2,40)


def test_insight_yoy_is_actual_previous_year():
    pts=history(24)
    x=summarize_volume(pts)
    assert x['count']==24
    assert x['period']=='2023-12-01'
    assert x['yoy_pct'] is not None
    assert x['sum_ytd']==sum(v for d,v in pts if d.year==2023)


def test_forecast_api_from_official_regional_rows(db):
    pts=history()
    rows=[ParsedRow('cbr-xlsx-volume-total-rf','Ипотека','РОССИЙСКАЯ ФЕДЕРАЦИЯ',
                     'млн руб.',0,0,None,None,None,None,d,d.isoformat(),v) for d,v in pts]
    put_rows(db,rows,source_kind='cbr_xlsx',source_url='https://www.cbr.ru/statistics/bank_sector/mortgage/')
    db.commit()
    app.dependency_overrides[get_db]=lambda:db
    try:
        with TestClient(app) as c:
            response=c.get('/api/v1/forecast',params={'months':3})
            assert response.status_code==200, response.text
            data=response.json()
            assert data['is_official_forecast'] is False
            assert data['forecasts'][0]['date']=='2026-01-01'
            assert data['training_count']==48
            assert c.get('/api/v1/forecast',params={'months':12}).status_code==422
            assert c.get('/api/v1/regions/insights').json()['count']==48
            assert c.get('/api/v1/refresh/status').status_code==200
            assert c.get('/').status_code==200
            assert c.get('/static/plotly.min.js').status_code==200
    finally:
        app.dependency_overrides.clear()
