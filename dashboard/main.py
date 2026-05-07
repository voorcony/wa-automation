"""WA Automation Dashboard service.

A small FastAPI app that:
  * serves the single-page admin dashboard from ./static/
  * proxies /api/orchestrator/* -> http://localhost:8080/*
  * proxies /api/ai/*           -> http://localhost:8082/*
  * exposes /api/status which fans out to both backends and aggregates the
    result so the front-end can render its top-of-page health badges with a
    single round-trip.

Run with:
    python -m uvicorn dashboard.main:app --host 0.0.0.0 --port 8086
or directly:
    python dashboard/main.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8080").rstrip("/")
AI_ENGINE_URL = os.environ.get("AI_ENGINE_URL", "http://localhost:8082").rstrip("/")
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8086"))
WA_WORKER_URL = os.environ.get("WA_WORKER_URL", "http://localhost:8083").rstrip("/")
PROXY_TIMEOUT = float(os.environ.get("DASHBOARD_PROXY_TIMEOUT", "30"))

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Hop-by-hop headers that must NOT be forwarded when proxying.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("dashboard")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(title="WA Automation Dashboard", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    app.state.http = httpx.AsyncClient(timeout=PROXY_TIMEOUT, follow_redirects=False)
    logger.info(
        "Dashboard starting; orchestrator=%s ai_engine=%s static=%s",
        ORCHESTRATOR_URL, AI_ENGINE_URL, STATIC_DIR,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    client: httpx.AsyncClient | None = getattr(app.state, "http", None)
    if client is not None:
        await client.aclose()
    logger.info("Dashboard stopped")


# ---------------------------------------------------------------------------
# Aggregated /api/status
# ---------------------------------------------------------------------------

async def _fetch_json(client: httpx.AsyncClient, url: str) -> tuple[bool, Any]:
    try:
        r = await client.get(url, timeout=5.0)
        if r.status_code >= 400:
            return False, {"status_code": r.status_code, "detail": r.text[:500]}
        try:
            return True, r.json()
        except ValueError:
            return True, r.text
    except httpx.HTTPError as exc:
        return False, {"error": str(exc)}


@app.get("/api/status")
async def api_status() -> JSONResponse:
    """Aggregate health + summary information for the dashboard header."""
    client: httpx.AsyncClient = app.state.http

    orch_health_ok, orch_health = await _fetch_json(client, f"{ORCHESTRATOR_URL}/healthz")
    orch_status_ok, orch_status = await _fetch_json(client, f"{ORCHESTRATOR_URL}/status")
    ai_health_ok, ai_health = await _fetch_json(client, f"{AI_ENGINE_URL}/health")

    accounts: list[dict[str, Any]] = []
    if orch_status_ok and isinstance(orch_status, dict):
        raw_accounts = orch_status.get("accounts") or []
        if isinstance(raw_accounts, list):
            accounts = [a for a in raw_accounts if isinstance(a, dict)]

    products_loaded = 0
    if ai_health_ok and isinstance(ai_health, dict):
        try:
            products_loaded = int(ai_health.get("products_loaded") or 0)
        except (TypeError, ValueError):
            products_loaded = 0

    payload = {
        "orchestrator_health": {
            "ok": orch_health_ok,
            "data": orch_health if orch_health_ok else None,
            "error": None if orch_health_ok else orch_health,
        },
        "ai_engine_health": {
            "ok": ai_health_ok,
            "data": ai_health if ai_health_ok else None,
            "error": None if ai_health_ok else ai_health,
        },
        "accounts": accounts,
        "products_loaded": products_loaded,
    }
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Generic reverse proxy
# ---------------------------------------------------------------------------

async def _proxy(request: Request, upstream_base: str, upstream_path: str) -> Response:
    """Forward `request` to `<upstream_base>/<upstream_path>` and stream back."""
    client: httpx.AsyncClient = app.state.http

    url = f"{upstream_base}/{upstream_path.lstrip('/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    fwd_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        fwd_headers[k] = v

    body = await request.body()

    try:
        upstream_resp = await client.request(
            request.method,
            url,
            content=body or None,
            headers=fwd_headers,
        )
    except httpx.ConnectError as exc:
        logger.warning("Proxy connect failed for %s: %s", url, exc)
        return JSONResponse(
            {"error": "upstream_unreachable", "upstream": url, "detail": str(exc)},
            status_code=502,
        )
    except httpx.HTTPError as exc:
        logger.warning("Proxy error for %s: %s", url, exc)
        return JSONResponse(
            {"error": "upstream_error", "upstream": url, "detail": str(exc)},
            status_code=502,
        )

    out_headers: dict[str, str] = {}
    for k, v in upstream_resp.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out_headers[k] = v

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=out_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


@app.api_route(
    "/api/orchestrator/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_orchestrator(path: str, request: Request) -> Response:
    return await _proxy(request, ORCHESTRATOR_URL, path)


@app.api_route(
    "/api/ai/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_ai(path: str, request: Request) -> Response:
    return await _proxy(request, AI_ENGINE_URL, path)


@app.api_route(
    "/api/wa/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_wa(path: str, request: Request) -> Response:
    return await _proxy(request, WA_WORKER_URL, path)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "dashboard"}


# ---------------------------------------------------------------------------
# Static files (mounted last so /api/* routes take precedence)
# ---------------------------------------------------------------------------

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "dashboard.main:app",
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        access_log=True,
    )
