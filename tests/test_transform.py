from datetime import date
import pytest
from app.transform import parse_data, parse_date

DATASET={"id":15,"name":"Количество ипотечных кредитов", "type":0}
PAYLOAD={"RawData":[{"date":"2026-02-01T00:00:00","dt":"01.02.2026", "obs_val":"12,3", "colId":-1,"unit_id":4,
                    "measure_id":None,"element_id":None}], "units":[{"id":4,"val":"тыс. шт."}]}

def test_parse_valid_and_provenance():
    rows=parse_data(PAYLOAD, 6, DATASET)
    assert len(rows)==1
    assert rows[0].period==date(2026,2,1)
    assert rows[0].value==12.3
    assert rows[0].unit=="тыс. шт."
    assert rows[0].dataset_id==15
    assert rows[0].publication_id==6

def test_missing_payload_rejected():
    with pytest.raises(ValueError, match="RawData"):
        parse_data({"data":[]},6,DATASET)
    with pytest.raises(ValueError, match="empty"):
        parse_data({"RawData":[]},6,DATASET)

def test_conflicting_repeated_point_rejected():
    raw=PAYLOAD["RawData"][0]
    with pytest.raises(ValueError, match="Conflicting"):
        parse_data({"RawData":[raw,{**raw,"obs_val":14}],"units":[]},6,DATASET)

def test_multiple_columns_are_distinct_series():
    raw=PAYLOAD["RawData"][0]
    rows=parse_data({"RawData":[raw,{**raw,"colId":2,"obs_val":14}],"units":[]},6,DATASET)
    assert len(rows)==2
    assert rows[0].code!=rows[1].code

def test_date_formats():
    assert parse_date({"dt":"01.09.2026"})==date(2026,9,1)
    assert parse_date({"dt":"2026"})==date(2026,1,1)
    with pytest.raises(ValueError):parse_date({"dt":"unknown"})
