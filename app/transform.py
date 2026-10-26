"""Strict, source-preserving conversion of CBR /data JSON into time series."""
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

@dataclass(frozen=True)
class ParsedRow:
    code: str
    name: str
    dimension: str
    unit: str
    publication_id: int
    dataset_id: int
    measure_id: int | None
    element_id: int | None
    col_id: int | None
    unit_id: int | None
    period: date
    raw_period: str
    value: float


def parse_date(row: dict) -> date:
    raw = str(row.get("date") or row.get("dt") or "").strip()
    if not raw:
        raise ValueError("Observation has no period")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%m.%Y", "%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unsupported CBR period {raw!r}")


def parse_data(payload: dict, publication_id: int, dataset: dict, measures: dict[int, str] | None = None) -> list[ParsedRow]:
    if not isinstance(payload, dict) or not isinstance(payload.get("RawData"), list):
        raise ValueError("CBR response lacks RawData array; database unchanged")
    raw = payload["RawData"]
    if not raw:
        raise ValueError("CBR returned empty RawData; database unchanged")
    units = {int(u["id"]): str(u.get("val") or u.get("name") or "Не указана")
             for u in payload.get("units", []) if isinstance(u, dict) and u.get("id") is not None}
    result: list[ParsedRow] = []
    unique: dict[tuple[str, date], float] = {}
    dataset_id = int(dataset["id"])
    dataset_name = str(dataset.get("full_name") or dataset.get("name") or f"Показатель {dataset_id}")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Unexpected CBR record format")
        if item.get("obs_val") is None or str(item["obs_val"]).strip() in ("", "—", "-"):
            continue  # CBR may explicitly signal missing observation
        value = float(str(item["obs_val"]).replace(" ", "").replace(",", "."))
        if not isfinite(value):
            raise ValueError("Non-finite numeric value")
        period = parse_date(item)
        measure = item.get("measure_id")
        element = item.get("element_id")
        col = item.get("colId")
        unit_id = item.get("unit_id")
        # RawData can contain multiple values for a date. Keep each dimension separate.
        label = (measures or {}).get(int(measure), f"Разрез {measure}") if measure is not None else "Общий показатель"
        if col not in (-1, None):
            label = f"{label} · столбец {col}"
        if element is not None:
            label = f"{label} · элемент {element}"
        code = f"cbr-p{publication_id}-d{dataset_id}-m{measure if measure is not None else 'x'}-e{element if element is not None else 'x'}-c{col if col is not None else 'x'}-u{unit_id if unit_id is not None else 'x'}"
        key = (code, period)
        if key in unique:
            if unique[key] != value:
                raise ValueError(f"Conflicting duplicates in CBR data for {code} / {period}")
            continue
        unique[key] = value
        result.append(ParsedRow(code, dataset_name, label, units.get(int(unit_id), "Не указана") if unit_id is not None else "Не указана",
                                publication_id, dataset_id, int(measure) if measure is not None else None,
                                int(element) if element is not None else None, int(col) if col is not None else None,
                                int(unit_id) if unit_id is not None else None,
                                period, str(item.get("dt") or item.get("date") or "").strip(), value))
    if not result:
        raise ValueError("CBR response contains no numeric observations")
    return result
