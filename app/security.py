"""Operational guardrails for public-read / administrative-write API."""
import os
import secrets
from urllib.parse import urlsplit
from fastapi import HTTPException, Request


def validate_settings():
    if os.getenv('APP_ENV', 'development').lower() == 'production':
        token = os.getenv('ADMIN_TOKEN', '')
        if len(token) < 32 or token == 'change-this-to-a-long-random-value':
            raise RuntimeError('Production ADMIN_TOKEN must contain at least 32 characters')
        if os.getenv('AUTO_SYNC_ON_START', '0') == '1':
            raise RuntimeError('Disable in-process AUTO_SYNC_ON_START in production; run dedicated scheduler')


def authorize_admin(request: Request, token: str | None, configured: str) -> None:
    # Avoid browser forms from the public internet targeting local developer ports.
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc.casefold() != request.headers.get('host', '').casefold():
        raise HTTPException(403, 'Cross-origin admin action forbidden')
    if configured:
        if not token or not secrets.compare_digest(token, configured):
            raise HTTPException(403, 'Valid X-Admin-Token required')
    else:
        local = request.client is not None and request.client.host in ('127.0.0.1', '::1', 'testclient')
        if os.getenv('APP_ENV', 'development') == 'production' or not local:
            raise HTTPException(403, 'Admin token is required for remote access')
