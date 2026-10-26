"""Digest utilities and evidence of accepted official source imports."""
import hashlib
import json
from pathlib import Path
from .models import SourceArtifact


def record_artifact(db, *, run_id, kind, url, data: bytes, retrieval='online', method='sha256_raw_bytes'):
    db.add(SourceArtifact(sync_run_id=run_id, kind=kind, url=url,
                          sha256=hashlib.sha256(data).hexdigest(), bytes_count=len(data),
                          retrieval=retrieval, checksum_method=method))


def record_json_artifact(db, *, run_id, kind, url, data):
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    record_artifact(db, run_id=run_id, kind=kind, url=url,
                    data=encoded, method='sha256_normalized_json')
