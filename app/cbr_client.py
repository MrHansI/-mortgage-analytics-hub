"""Client for Bank of Russia documented Data Service REST endpoints.

Requests are restricted to the official configured CBR host. No scrape or IDs
are guessed. Dataset selection happens through official catalogs.
"""
import time
from urllib.parse import urlsplit
import httpx
from .config import CBR_API_BASE, CBR_TIMEOUT

class CBRClient:
    MAX_RESPONSE_BYTES = 15_000_000
    def __init__(self, base_url: str = CBR_API_BASE, transport=None):
        parsed = urlsplit(base_url)
        if parsed.hostname not in ("www.cbr.ru", "cbr.ru") or parsed.path.rstrip("/") != "/dataservice":
            raise ValueError("CBR_API_BASE must point to official CBR /dataservice host")
        if parsed.scheme != "https" and not (transport is not None and parsed.scheme == "http"):
            raise ValueError("CBR_API_BASE must be HTTPS")
        if parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment:
            raise ValueError("Invalid CBR_API_BASE")
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=CBR_TIMEOUT, follow_redirects=False,
            headers={"Accept":"application/json", "User-Agent":"MortgageAnalyticsHub/0.5 (+public statistics ETL)"},
            transport=transport)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, endpoint: str, **params):
        if endpoint not in {"publications", "datasets", "measures", "years", "data"}:
            raise ValueError("Unsupported endpoint")
        for attempt in range(3):
            try:
                with self.client.stream('GET', f"{self.base_url}/{endpoint}", params=params) as res:
                    if res.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        time.sleep(0.3 * (2 ** attempt))
                        continue
                    res.raise_for_status()
                    if "json" not in res.headers.get('content-type', 'application/json').lower():
                        raise ValueError("CBR API did not return JSON")
                    payload_bytes = bytearray()
                    for chunk in res.iter_bytes():
                        if len(payload_bytes) + len(chunk) > self.MAX_RESPONSE_BYTES:
                            raise ValueError("CBR API payload exceeds 15 MB safety limit")
                        payload_bytes.extend(chunk)
                    return __import__('json').loads(payload_bytes)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt >= 2:
                    raise
                time.sleep(0.3 * (2 ** attempt))
        raise RuntimeError("CBR API retries exhausted")

    def publications(self):
        result = self.get("publications")
        if not isinstance(result, list):
            raise ValueError("Unexpected publications payload")
        return result

    def datasets(self, publication_id: int):
        result = self.get("datasets", publicationId=publication_id)
        if not isinstance(result, list):
            raise ValueError("Unexpected datasets payload")
        return result

    def measures(self, dataset_id: int):
        result = self.get("measures", datasetId=dataset_id)
        if not isinstance(result, dict) or not isinstance(result.get("measure"), list):
            raise ValueError("Unexpected measures payload")
        return result["measure"]

    def years(self, dataset_id: int, measure_id: int = -1):
        return self.get("years", datasetId=dataset_id, measureId=measure_id)

    def data(self, publication_id: int, dataset_id: int, start_year: int, end_year: int,
             measure_id: int = -1):
        return self.get("data", y1=start_year, y2=end_year, publicationId=publication_id,
                        datasetId=dataset_id, measureId=measure_id)
