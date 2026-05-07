"""
AdsPower Local API manager.

Wraps the AdsPower Local API (default: http://127.0.0.1:50325) to create,
start, stop, list, and update browser profiles, and to retrieve the CDP
WebSocket endpoint needed to drive the browser with Playwright / Puppeteer.

The AdsPower Local API responses share a common envelope:

    {"code": 0, "msg": "success", "data": {...}}

`code == 0` indicates success; any other value indicates failure and the
human-readable reason is in `msg`. This module normalizes that into Python
exceptions and returns plain dicts containing only the useful payload.

Rate limiting note: the AdsPower Local API permits roughly one request per
second. This client serializes calls through an internal lock and enforces
a configurable minimum interval between requests.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AdsPowerError(Exception):
    """Base exception for all AdsPower client errors."""


class AdsPowerAPIError(AdsPowerError):
    """Raised when the AdsPower API returns a non-success ``code``."""

    def __init__(self, code: int, message: str, endpoint: str) -> None:
        super().__init__(f"AdsPower API error {code} at {endpoint}: {message}")
        self.code = code
        self.message = message
        self.endpoint = endpoint


class AdsPowerConnectionError(AdsPowerError):
    """Raised when the local AdsPower service is unreachable."""


class AdsPowerTimeoutError(AdsPowerError):
    """Raised when an operation (e.g. browser start) exceeds its deadline."""


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class BrowserSession:
    """Information returned by ``start_profile``.

    ``ws_endpoint`` is the Chrome DevTools Protocol WebSocket URL that
    automation libraries (Playwright, Puppeteer, pyppeteer) connect to.
    """

    user_id: str
    ws_endpoint: str            # CDP ws://... URL (puppeteer flavor)
    selenium_endpoint: str       # host:port for Selenium remote-debugging
    webdriver_path: str          # path to bundled chromedriver, if any
    debug_port: str              # raw debug port string
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "ws_endpoint": self.ws_endpoint,
            "selenium_endpoint": self.selenium_endpoint,
            "webdriver_path": self.webdriver_path,
            "debug_port": self.debug_port,
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AdsPowerManager:
    """Synchronous client for the AdsPower Local API.

    Parameters
    ----------
    api_key:
        API key from AdsPower -> Settings -> API.
    base_url:
        Local API base URL. Defaults to ``http://127.0.0.1:50325``.
    group_name:
        Default group new profiles are placed in.
    open_urls:
        Default list of URLs newly created profiles open on first launch.
    request_timeout:
        Per-request HTTP timeout, in seconds.
    start_timeout:
        Maximum time to wait for ``start_profile`` to return a CDP endpoint.
    max_retries:
        Retry attempts for transient network / 5xx failures.
    retry_backoff:
        Exponential backoff base, in seconds.
    min_request_interval:
        Minimum gap between successive requests (AdsPower rate limit).
    """

    DEFAULT_BASE_URL = "http://127.0.0.1:50325"
    DEFAULT_GROUP = "wa-auto"
    DEFAULT_OPEN_URLS: tuple[str, ...] = ("https://web.whatsapp.com",)

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        group_name: str = DEFAULT_GROUP,
        open_urls: Iterable[str] | None = None,
        request_timeout: float = 30.0,
        start_timeout: float = 90.0,
        max_retries: int = 3,
        retry_backoff: float = 1.5,
        min_request_interval: float = 1.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.group_name = group_name
        self.open_urls: list[str] = list(open_urls) if open_urls else list(self.DEFAULT_OPEN_URLS)
        self.request_timeout = request_timeout
        self.start_timeout = start_timeout
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff = retry_backoff
        self.min_request_interval = min_request_interval

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(request_timeout),
            headers={"User-Agent": "wa-automation/ads-manager"},
        )
        self._rate_lock = threading.Lock()
        self._last_request_at: float = 0.0

    # -- context manager / lifecycle -----------------------------------------

    def __enter__(self) -> "AdsPowerManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Error closing httpx client", exc_info=True)

    # -- low-level request plumbing ------------------------------------------

    def _throttle(self) -> None:
        """Block until at least ``min_request_interval`` has elapsed."""
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait = self.min_request_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a request, retry transient failures, unwrap the envelope."""
        params = dict(params or {})
        params.setdefault("api_key", self.api_key)

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                logger.debug(
                    "AdsPower %s %s (attempt %d/%d) params=%s body=%s",
                    method, endpoint, attempt, self.max_retries,
                    {k: v for k, v in params.items() if k != "api_key"},
                    json_body,
                )
                response = self._client.request(
                    method, endpoint, params=params, json=json_body,
                )
            except httpx.ConnectError as exc:
                last_exc = exc
                logger.warning(
                    "AdsPower unreachable at %s (%s). Is the desktop app running?",
                    self.base_url, exc,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("AdsPower request timed out: %s %s", method, endpoint)
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("AdsPower HTTP error: %s", exc)
            else:
                # Retry on 5xx; surface other HTTP errors immediately.
                if 500 <= response.status_code < 600:
                    last_exc = httpx.HTTPStatusError(
                        f"server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    logger.warning(
                        "AdsPower 5xx %s on %s (attempt %d)",
                        response.status_code, endpoint, attempt,
                    )
                else:
                    return self._parse_envelope(response, endpoint)

            if attempt < self.max_retries:
                sleep_for = self.retry_backoff ** attempt
                logger.debug("Retrying %s in %.2fs", endpoint, sleep_for)
                time.sleep(sleep_for)

        # Exhausted retries
        if isinstance(last_exc, httpx.ConnectError):
            raise AdsPowerConnectionError(
                f"Cannot reach AdsPower Local API at {self.base_url}. "
                f"Ensure the AdsPower desktop app is running."
            ) from last_exc
        if isinstance(last_exc, httpx.TimeoutException):
            raise AdsPowerTimeoutError(
                f"AdsPower request {method} {endpoint} timed out after "
                f"{self.max_retries} attempts"
            ) from last_exc
        raise AdsPowerError(f"AdsPower request {method} {endpoint} failed") from last_exc

    @staticmethod
    def _parse_envelope(response: httpx.Response, endpoint: str) -> dict[str, Any]:
        """Validate the standard ``{code, msg, data}`` envelope."""
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AdsPowerError(
                f"HTTP {response.status_code} from {endpoint}: {response.text[:200]}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise AdsPowerError(
                f"Non-JSON response from {endpoint}: {response.text[:200]}"
            ) from exc

        code = payload.get("code")
        if code != 0:
            raise AdsPowerAPIError(
                code=int(code) if isinstance(code, int) else -1,
                message=str(payload.get("msg", "unknown error")),
                endpoint=endpoint,
            )
        return payload.get("data") or {}

    # -- public API: profiles ------------------------------------------------

    def create_profile(
        self,
        user_name: str,
        *,
        group_name: str | None = None,
        open_urls: Iterable[str] | None = None,
        proxy_config: dict[str, Any] | None = None,
        fingerprint_config: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new browser profile.

        Returns a dict with at least ``user_id`` and ``user_name``.
        """
        if not user_name:
            raise ValueError("user_name is required")

        body: dict[str, Any] = {
            "name": user_name,
            "user_name": user_name,
            "group_name": group_name or self.group_name,
            "open_urls": list(open_urls) if open_urls is not None else list(self.open_urls),
            # AdsPower requires a fingerprint config; "1" = random fingerprint.
            "fingerprint_config": fingerprint_config or {"automatic_timezone": "1"},
            # No proxy by default; pass an explicit config to use one.
            "user_proxy_config": proxy_config or {"proxy_soft": "no_proxy"},
        }
        if extra:
            body.update(extra)

        data = self._request("POST", "/api/v1/user/create", json_body=body)
        user_id = data.get("id") or data.get("user_id")
        if not user_id:
            raise AdsPowerError(f"create_profile returned no user id: {data}")

        logger.info("Created AdsPower profile user_id=%s name=%s", user_id, user_name)
        return {
            "user_id": str(user_id),
            "user_name": user_name,
            "group_name": body["group_name"],
            "raw": data,
        }

    def update_profile_proxy(
        self,
        user_id: str,
        proxy_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Update the proxy configuration of an existing profile.

        ``proxy_config`` follows AdsPower's schema, e.g.::

            {
                "proxy_soft": "luminati",     # or other_https, no_proxy, etc.
                "proxy_type": "http",          # http | https | socks5
                "proxy_host": "1.2.3.4",
                "proxy_port": "8080",
                "proxy_user": "user",
                "proxy_password": "pass",
            }
        """
        if not user_id:
            raise ValueError("user_id is required")
        if not proxy_config:
            raise ValueError("proxy_config is required")

        body = {
            "user_id": user_id,
            "user_proxy_config": proxy_config,
        }
        data = self._request("POST", "/api/v1/user/update", json_body=body)
        logger.info("Updated proxy for AdsPower profile user_id=%s", user_id)
        return {"user_id": user_id, "raw": data}

    def list_profiles(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a list of all profiles (single page)."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if group_id:
            params["group_id"] = group_id

        data = self._request("GET", "/api/v1/user/list", params=params)
        items = data.get("list") or []
        results = [
            {
                "user_id": str(item.get("user_id") or item.get("id") or ""),
                "user_name": item.get("name") or item.get("user_name") or "",
                "group_name": item.get("group_name") or "",
                "last_open_time": item.get("last_open_time"),
                "raw": item,
            }
            for item in items
        ]
        logger.debug("Listed %d AdsPower profiles", len(results))
        return results

    # -- public API: browser lifecycle ---------------------------------------

    def start_profile(
        self,
        user_id: str,
        *,
        open_tabs: bool = True,
        ip_tab: bool = False,
        headless: bool = False,
    ) -> BrowserSession:
        """Launch the browser for ``user_id`` and return its CDP endpoint.

        Honors ``self.start_timeout`` by overriding the per-request timeout
        for this call only -- cold browser starts can take a minute.
        """
        if not user_id:
            raise ValueError("user_id is required")

        params: dict[str, Any] = {
            "user_id": user_id,
            "open_tabs": "1" if open_tabs else "0",
            "ip_tab": "1" if ip_tab else "0",
            "headless": "1" if headless else "0",
        }

        original_timeout = self._client.timeout
        self._client.timeout = httpx.Timeout(self.start_timeout)
        try:
            data = self._request("GET", "/api/v1/browser/start", params=params)
        finally:
            self._client.timeout = original_timeout

        ws = data.get("ws") or {}
        ws_endpoint = ws.get("puppeteer") or ws.get("selenium") or ""
        if not ws_endpoint:
            raise AdsPowerError(
                f"start_profile for {user_id} returned no CDP endpoint: {data}"
            )

        session = BrowserSession(
            user_id=user_id,
            ws_endpoint=ws_endpoint,
            selenium_endpoint=str(ws.get("selenium", "")),
            webdriver_path=str(data.get("webdriver", "")),
            debug_port=str(data.get("debug_port", "")),
            raw=data,
        )
        logger.info(
            "Started AdsPower profile user_id=%s ws=%s",
            user_id, session.ws_endpoint,
        )
        return session

    def stop_profile(self, user_id: str) -> dict[str, Any]:
        """Close the browser for ``user_id``. Idempotent."""
        if not user_id:
            raise ValueError("user_id is required")

        data = self._request(
            "GET", "/api/v1/browser/stop", params={"user_id": user_id},
        )
        logger.info("Stopped AdsPower profile user_id=%s", user_id)
        return {"user_id": user_id, "raw": data}

    def get_browser_status(self, user_id: str) -> dict[str, Any]:
        """Return the current open/close status for a profile."""
        if not user_id:
            raise ValueError("user_id is required")
        data = self._request(
            "GET", "/api/v1/browser/active", params={"user_id": user_id},
        )
        return {
            "user_id": user_id,
            "status": data.get("status", "unknown"),
            "raw": data,
        }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def build_from_config(cfg: dict[str, Any]) -> AdsPowerManager:
    """Build an ``AdsPowerManager`` from a parsed ``config.yaml`` dict."""
    section = cfg.get("adspower") or {}
    api_key = section.get("api_key")
    if not api_key:
        raise ValueError(
            "adspower.api_key is missing. Set ADSPOWER_API_KEY in the environment."
        )
    return AdsPowerManager(
        api_key=api_key,
        base_url=section.get("base_url", AdsPowerManager.DEFAULT_BASE_URL),
        group_name=section.get("group_name", AdsPowerManager.DEFAULT_GROUP),
        open_urls=section.get("open_urls"),
        request_timeout=float(section.get("request_timeout", 30.0)),
        start_timeout=float(section.get("start_timeout", 90.0)),
        max_retries=int(section.get("max_retries", 3)),
        retry_backoff=float(section.get("retry_backoff", 1.5)),
        min_request_interval=float(section.get("min_request_interval", 1.0)),
    )


__all__ = [
    "AdsPowerManager",
    "BrowserSession",
    "AdsPowerError",
    "AdsPowerAPIError",
    "AdsPowerConnectionError",
    "AdsPowerTimeoutError",
    "build_from_config",
]
