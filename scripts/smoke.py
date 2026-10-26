"""Safe, read-only smoke checks against a *running* Mortgage Analytics Hub.

Usage: python scripts/smoke.py --base-url http://127.0.0.1:8000
"""
import argparse
import sys
import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    args=parser.parse_args()
    paths=['/', '/api/v1/health','/api/v1/readiness', '/api/v1/data-quality',
           '/api/v1/series','/api/v1/regions','/api/v1/sync-runs','/api/v1/source-artifacts','/static/app.js']
    with httpx.Client(base_url=args.base_url,timeout=12,follow_redirects=False) as c:
        for path in paths:
            r=c.get(path)
            if not r.is_success:
                print('FAIL', path, r.status_code, r.text[:180])
                return 1
            if path.startswith('/api/'):
                r.json()
            print('OK',path,r.status_code)
    print('Smoke passed (read-only local HTTP checks; no external CBR connectivity verified)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
