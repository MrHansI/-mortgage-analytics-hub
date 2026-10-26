"""Official Bank of Russia monthly mortgage overview importer.

These published HTML pages are distinct from CBR Data Service REST JSON API.
Fail closed on unknown page layout: NEVER silently fabricate or infer a value.
"""
import re
from datetime import date
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from .importer import put_rows
from .provenance import record_artifact
from .models import SyncRun, utcnow
from .transform import ParsedRow

REPORT_ROOT = "https://www.cbr.ru/statistics/bank_sector/mortgage/Indicator_mortgage"
RU_MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)
RU_IN_MONTH = (
    "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
)


def month_url(period: date) -> str:
    if period.day != 1 or not 2000 <= period.year <= 2099:
        raise ValueError("period must be first day of a month between 2000 and 2099")
    return f"{REPORT_ROOT}/{period:%m%y}/"


def months_between(first: date, last: date):
    if first > last:
        raise ValueError("From-period must not be later than To-period")
    if first.day != 1 or last.day != 1:
        raise ValueError("Specify periods as YYYY-MM-01")
    month = first
    while month <= last:
        yield month
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)


def _decimal(value: str) -> float:
    result = float(value.replace(" ", "").replace(",", "."))
    if result < 0 or not __import__("math").isfinite(result):
        raise ValueError("Unreasonable negative/non-finite mortgage value")
    return result


def parse_report_html(html: str, period: date) -> list[ParsedRow]:
    """Extract explicitly labeled figures, cross-check issuance against prose.

    Portfolio is an end-of-month stock reported as of 1st of next month;
    its series uses the covered month so it can be compared with flow metrics.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ").replace("\u202f", " "))
    if not re.search(r"Обзор рынка ипотечного жилищного кредитования", text, flags=re.I):
        raise ValueError("Not a Bank of Russia mortgage overview page")
    month_name = RU_MONTHS[period.month - 1]
    if not re.search(rf"\b{month_name}\s+{period.year}\b", text, flags=re.I):
        raise ValueError(f"Wrong reporting period: expected {month_name} {period.year}")
    heading = re.search(r"Портфель\s+Объем\s+выдач\s+Средний\s+размер", text, re.I)
    if heading is None:
        raise ValueError("Overview headline layout unrecognized; no rows imported")
    # Cut before DDU figures, which must not be silently mixed with total IHL figures.
    section = text[heading.end():]
    ddu = re.search(r"ИЖК\s+по\s+ДДУ", section, re.I)
    if ddu is None:
        raise ValueError("Cannot locate DDU section boundary")
    section = section[:ddu.start()]
    values = {}
    for key, unit, label in (
        ("portfolio", "трлн", "Портфель ипотечных жилищных кредитов (на конец месяца)"),
        ("issuance", "млрд", "Объём выданных ипотечных кредитов за месяц"),
        ("avg_loan", "млн", "Средний размер выданного ипотечного кредита"),
    ):
        m = re.search(rf"(?<!\d)(\d+(?:[.,]\d+)?)\s*{unit}\s+рубл", section, re.I)
        if m is None:
            raise ValueError(f"Headline metric {key} missing in CBR report")
        values[key] = (_decimal(m.group(1)), f"{unit} руб.", label)
    # More detail from narrative, only when clear and non-contradictory.
    narrative = text[heading.end() + ddu.start():]
    in_month = RU_IN_MONTH[period.month - 1]
    count = re.search(
        rf"(?:в\s+{in_month}\s+{period.year}\s+года\s+)?предоставили\s+"
        r"(\d+(?:[.,]\d+)?)\s*тыс\.\s*рублевых\s*ИЖК\s+на\s+сумму\s+"
        r"(\d+(?:[.,]\d+)?)\s*млрд\s+рубл",
        narrative,
        re.I,
    )
    if count:
        if abs(_decimal(count.group(2)) - values["issuance"][0]) > 0.15:
            raise ValueError("CBR report: headline issuance conflicts with narrative issuance")
        values["count"] = (_decimal(count.group(1)), "тыс. кредитов", "Количество выданных ипотечных кредитов за месяц")
    rate = re.search(
        r"средневзвешенн\w*\s+ставк\w*\s+по\s+ИЖК\s*(?:\d+\s*)?"
        r"(?:[^.]{0,130}?)\b(?:до|составила|равнялась|на уровне)\s*"
        r"(\d+(?:[.,]\d+)?)\s*%",
        narrative,
        re.I,
    )
    if rate:
        value = _decimal(rate.group(1))
        if not 0 <= value <= 100:
            raise ValueError("Invalid rate reported by CBR")
        values["rate"] = (value, "% годовых", "Средневзвешенная ставка по выданным ипотечным кредитам")
    # Code stable across months, report URL varies by month.
    result = []
    for key, (value, unit, name) in values.items():
        result.append(ParsedRow(
            code=f"cbr-report-rf-{key}", name=name, dimension="Российская Федерация",
            unit=unit, publication_id=0, dataset_id=0,
            measure_id=None, element_id=None, col_id=None, unit_id=None,
            period=period,
            raw_period=(f"{period:%Y-%m} (остаток на начало следующего месяца)" if key == "portfolio"
                        else period.strftime("%Y-%m")),
            value=value,
        ))
    return result


def download_report(period: date, *, transport=None) -> str:
    url = month_url(period)
    with httpx.Client(
        transport=transport, timeout=25, follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0 MortgageAnalyticsHub/0.2", "Accept": "text/html"},
    ) as client:
        with client.stream('GET', url) as response:
            response.raise_for_status()
            if urlsplit(str(response.url)).hostname not in ("www.cbr.ru", "cbr.ru"):
                raise ValueError("Unexpected non-CBR response")
            if "html" not in response.headers.get("content-type", "").lower():
                raise ValueError("CBR did not return HTML")
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > 2_000_000:
                    raise ValueError("CBR report HTML exceeds 2 MB safety limit")
                content.extend(chunk)
            charset = response.encoding or 'utf-8'
            return content.decode(charset, errors='replace')


def sync_report_month(db: Session, period: date, *, downloader=download_report) -> dict:
    url = month_url(period)
    run = SyncRun(source="cbr_report", status="running")
    db.add(run)
    db.commit()
    run_id = run.id
    try:
        html = downloader(period)
        rows = parse_report_html(html, period)
        result = put_rows(db, rows, source_kind="cbr_report", source_url=url, sync_run_id=run_id)
        record_artifact(db, run_id=run_id, kind="cbr_report_html", url=url,
                        data=html.encode("utf-8"), method="sha256_utf8_html")
        run.status = "success"
        run.finished_at = utcnow()
        run.inserted = result["inserted"]
        run.updated = result["updated"]
        run.unchanged = result["unchanged"]
        db.commit()
        return {"period": period.isoformat(), "url": url, "run_id": run.id, **result}
    except Exception as exc:
        db.rollback()
        run = db.get(SyncRun, run_id)
        run.status = "error"
        run.error = f"{type(exc).__name__}: {str(exc)[:450]}"
        run.finished_at = utcnow()
        db.commit()
        raise
