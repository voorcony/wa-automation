"""
WA Automation orchestrator.

Responsibilities
----------------
1. Read ../config.yaml (with ${VAR:-default} interpolation).
2. For each account in ``wa_accounts`` (config and/or persisted state):
     - Create an AdsPower profile if ``user_id`` is empty.
     - Start the AdsPower profile to obtain a CDP ws_endpoint.
     - Spawn ``node ../wa-worker/worker.js --user-id=... --ws-endpoint=...``
       as a child process, one per account.
3. Monitor every worker (health check every 30s, auto-restart on crash with
   exponential backoff).
4. Expose a small HTTP control plane on ``orchestrator.port`` (default 8080):
       GET    /status
       GET    /healthz
       POST   /accounts                 {"name": "...", "proxy": {...}}
       DELETE /accounts/{user_id}
5. Graceful shutdown on SIGTERM/SIGINT: stop workers, stop AdsPower profiles,
   close the HTTP server.

The orchestrator imports the existing ``ads.ads_manager`` package and runs all
of its synchronous calls in a thread pool to keep the asyncio loop responsive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# --- Make the sibling ``ads`` package importable when run as a script -------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ads.ads_manager import (  # noqa: E402  (import after sys.path mutation)
    AdsPowerError,
    AdsPowerManager,
    BrowserSession,
    build_from_config,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("orchestrator")


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Config loader (with ${VAR} / ${VAR:-default} interpolation)
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            return os.environ.get(var, default if default is not None else "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return _interpolate(raw)


# ---------------------------------------------------------------------------
# Persistent account store
# ---------------------------------------------------------------------------

@dataclass
class AccountSpec:
    """User-supplied description of an account."""
    name: str
    proxy: dict[str, Any] | None = None
    user_id: str = ""           # AdsPower profile id; empty -> created on demand

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "proxy": self.proxy, "user_id": self.user_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AccountSpec":
        return cls(
            name=str(d.get("name", "")).strip(),
            proxy=d.get("proxy") or None,
            user_id=str(d.get("user_id") or "").strip(),
        )


class AccountStore:
    """Tiny JSON-backed store for account specs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def load(self) -> list[AccountSpec]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read account store %s: %s", self.path, exc)
            return []
        return [AccountSpec.from_dict(d) for d in data if isinstance(d, dict)]

    async def save(self, specs: list[AccountSpec]) -> None:
        async with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([s.to_dict() for s in specs], indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Per-account runtime state
# ---------------------------------------------------------------------------

@dataclass
class ManagedAccount:
    spec: AccountSpec
    session: BrowserSession | None = None
    process: asyncio.subprocess.Process | None = None
    state: str = "PENDING"          # PENDING|STARTING|RUNNING|CRASHED|STOPPING|STOPPED
    last_state_change: float = field(default_factory=time.time)
    restart_count: int = 0
    last_error: str | None = None
    started_at: float | None = None

    @property
    def user_id(self) -> str:
        return self.spec.user_id

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process and self.process.returncode is None else None

    def set_state(self, state: str, error: str | None = None) -> None:
        self.state = state
        self.last_state_change = time.time()
        if error is not None:
            self.last_error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.spec.name,
            "state": self.state,
            "pid": self.pid,
            "ws_endpoint": self.session.ws_endpoint if self.session else None,
            "started_at": self.started_at,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "last_state_change": self.last_state_change,
        }


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class Supervisor:
    """Owns AdsPower sessions + worker subprocesses for every account."""

    HEALTH_CHECK_INTERVAL = 30.0
    SHUTDOWN_GRACE_SECONDS = 10.0
    MAX_RESTART_BACKOFF = 60.0

    def __init__(
        self,
        config: dict[str, Any],
        config_path: Path,
        ads: AdsPowerManager,
        store: AccountStore,
    ) -> None:
        self.config = config
        self.config_path = config_path.resolve()
        self.ads = ads
        self.store = store

        orch_cfg = config.get("orchestrator") or {}
        ai_cfg = config.get("ai_engine") or {}
        ai_host = ai_cfg.get("host", "localhost")
        if ai_host in ("0.0.0.0", ""):
            ai_host = "localhost"
        self.ai_url: str = orch_cfg.get(
            "ai_engine_url", f"http://{ai_host}:{ai_cfg.get('port', 8082)}"
        )

        # Allow override; otherwise default to ../wa-worker/worker.js
        worker_script = orch_cfg.get("worker_script") or str(_REPO_ROOT / "wa-worker" / "worker.js")
        self.worker_script = Path(worker_script).resolve()
        self.node_bin: str = orch_cfg.get("node_bin", "node")

        self.health_check_interval = float(
            orch_cfg.get("health_check_interval", self.HEALTH_CHECK_INTERVAL)
        )

        self.accounts: dict[str, ManagedAccount] = {}    # keyed by user_id
        self._pending: list[ManagedAccount] = []          # specs without user_id yet
        self._mutate_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()

    # -------------------- helpers around blocking AdsPower calls ----------

    async def _ads(self, fn, *args, **kwargs):
        """Run a synchronous AdsPower call in a thread to avoid blocking."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -------------------- bootstrap ---------------------------------------

    async def start(self) -> None:
        specs = self._gather_initial_specs()
        logger.info("Bootstrapping %d account(s)", len(specs))
        for spec in specs:
            try:
                await self._add_account_internal(spec, persist=False)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to bring up account %r: %s", spec.name, exc)

        await self._persist()

        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="orchestrator-monitor"
        )

    def _gather_initial_specs(self) -> list[AccountSpec]:
        """Merge specs from config.yaml (wa_accounts) with the persisted store.

        Persisted state wins on user_id (so we can reattach to existing
        AdsPower profiles after a restart).
        """
        from_cfg = [
            AccountSpec.from_dict(d)
            for d in (self.config.get("wa_accounts") or [])
            if isinstance(d, dict) and d.get("name")
        ]
        from_store = {s.name: s for s in self.store.load()}

        merged: dict[str, AccountSpec] = {}
        for s in from_cfg:
            existing = from_store.get(s.name)
            if existing and existing.user_id and not s.user_id:
                s.user_id = existing.user_id
            merged[s.name] = s
        for name, s in from_store.items():
            merged.setdefault(name, s)
        return list(merged.values())

    # -------------------- per-account lifecycle ---------------------------

    async def _add_account_internal(self, spec: AccountSpec, *, persist: bool) -> ManagedAccount:
        if not spec.name:
            raise ValueError("account name is required")

        if not spec.user_id:
            logger.info("Creating AdsPower profile for %r", spec.name)
            try:
                created = await self._ads(
                    self.ads.create_profile,
                    spec.name,
                    proxy_config=spec.proxy or None,
                )
            except AdsPowerError as exc:
                raise RuntimeError(f"AdsPower create failed: {exc}") from exc
            spec.user_id = str(created["user_id"])
            logger.info("Created AdsPower profile user_id=%s name=%s", spec.user_id, spec.name)
        elif spec.proxy:
            with contextlib.suppress(AdsPowerError):
                await self._ads(self.ads.update_profile_proxy, spec.user_id, spec.proxy)

        if spec.user_id in self.accounts:
            raise ValueError(f"account {spec.user_id} already managed")

        managed = ManagedAccount(spec=spec)
        self.accounts[spec.user_id] = managed

        try:
            await self._launch(managed)
        except Exception as exc:  # noqa: BLE001
            managed.set_state("CRASHED", error=str(exc))
            logger.exception("Failed to launch worker for %s: %s", spec.user_id, exc)

        if persist:
            await self._persist()
        return managed

    async def _launch(self, managed: ManagedAccount) -> None:
        """Start AdsPower browser + spawn the wa-worker subprocess."""
        managed.set_state("STARTING")
        spec = managed.spec

        logger.info("Starting AdsPower browser for %s (%s)", spec.user_id, spec.name)
        session: BrowserSession = await self._ads(self.ads.start_profile, spec.user_id)
        managed.session = session
        logger.info("AdsPower started %s ws=%s", spec.user_id, session.ws_endpoint)

        if not self.worker_script.exists():
            raise FileNotFoundError(f"worker script not found: {self.worker_script}")

        cmd = [
            self.node_bin,
            str(self.worker_script),
            f"--user-id={spec.user_id}",
            f"--ws-endpoint={session.ws_endpoint}",
            f"--ai-url={self.ai_url}",
            f"--config={self.config_path}",
        ]
        logger.info("Spawning worker: %s", " ".join(cmd))

        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.worker_script.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        managed.process = proc
        managed.started_at = time.time()
        managed.set_state("RUNNING")

        # Pipe child stdout/stderr to our logger so it is not lost.
        asyncio.create_task(
            _stream_to_logger(proc.stdout, logger, logging.INFO, spec.user_id),
            name=f"worker-stdout-{spec.user_id}",
        )
        asyncio.create_task(
            _stream_to_logger(proc.stderr, logger, logging.WARNING, spec.user_id),
            name=f"worker-stderr-{spec.user_id}",
        )

    async def _stop_worker(self, managed: ManagedAccount, *, kill: bool = False) -> None:
        proc = managed.process
        if not proc or proc.returncode is not None:
            return
        sig = signal.SIGKILL if kill else signal.SIGTERM
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.SHUTDOWN_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Worker %s did not exit in %.1fs; killing", managed.user_id,
                           self.SHUTDOWN_GRACE_SECONDS)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)

    async def remove_account(self, user_id: str) -> None:
        async with self._mutate_lock:
            managed = self.accounts.get(user_id)
            if not managed:
                raise KeyError(user_id)
            managed.set_state("STOPPING")
            await self._stop_worker(managed)
            with contextlib.suppress(AdsPowerError):
                await self._ads(self.ads.stop_profile, user_id)
            managed.set_state("STOPPED")
            self.accounts.pop(user_id, None)
            await self._persist()

    async def add_account(self, name: str, proxy: dict[str, Any] | None) -> ManagedAccount:
        async with self._mutate_lock:
            if any(a.spec.name == name for a in self.accounts.values()):
                raise ValueError(f"account with name {name!r} already exists")
            spec = AccountSpec(name=name, proxy=proxy)
            return await self._add_account_internal(spec, persist=True)

    # -------------------- monitoring & restart ----------------------------

    async def _monitor_loop(self) -> None:
        logger.info("Monitor loop started (interval=%.1fs)", self.health_check_interval)
        try:
            while not self._shutdown_event.is_set():
                try:
                    await self._health_check_once()
                except Exception:  # noqa: BLE001
                    logger.exception("Health check iteration failed")
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.health_check_interval,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.info("Monitor loop exited")

    async def _health_check_once(self) -> None:
        for managed in list(self.accounts.values()):
            if managed.state in ("STOPPING", "STOPPED"):
                continue

            proc = managed.process
            if proc is None:
                # Never started successfully; try again.
                await self._restart(managed, reason="never-started")
                continue

            if proc.returncode is None:
                # Still running. Optional: ping /healthz, but a live PID is good enough.
                continue

            rc = proc.returncode
            logger.warning(
                "Worker for %s exited with code %s; scheduling restart",
                managed.user_id, rc,
            )
            managed.set_state("CRASHED", error=f"exit code {rc}")
            await self._restart(managed, reason=f"exit-{rc}")

    async def _restart(self, managed: ManagedAccount, *, reason: str) -> None:
        managed.restart_count += 1
        backoff = min(2 ** managed.restart_count, self.MAX_RESTART_BACKOFF)
        logger.info(
            "Restarting %s in %.1fs (attempt #%d, reason=%s)",
            managed.user_id, backoff, managed.restart_count, reason,
        )
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=backoff)
            return  # shutting down — abort restart
        except asyncio.TimeoutError:
            pass

        # Best-effort: stop the AdsPower browser before re-launching, so
        # start_profile gives us a fresh CDP endpoint.
        with contextlib.suppress(AdsPowerError):
            await self._ads(self.ads.stop_profile, managed.user_id)

        try:
            await self._launch(managed)
        except Exception as exc:  # noqa: BLE001
            managed.set_state("CRASHED", error=str(exc))
            logger.exception("Restart for %s failed: %s", managed.user_id, exc)

    # -------------------- persistence -------------------------------------

    async def _persist(self) -> None:
        await self.store.save([m.spec for m in self.accounts.values()])

    # -------------------- shutdown ----------------------------------------

    async def shutdown(self) -> None:
        if self._shutdown_event.is_set():
            return
        logger.info("Supervisor shutting down")
        self._shutdown_event.set()

        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task

        # Stop all workers in parallel.
        await asyncio.gather(
            *[self._stop_worker(m) for m in self.accounts.values()],
            return_exceptions=True,
        )

        # Stop all AdsPower profiles in parallel.
        await asyncio.gather(
            *[self._ads(self.ads.stop_profile, uid) for uid in list(self.accounts.keys())],
            return_exceptions=True,
        )

        for m in self.accounts.values():
            m.set_state("STOPPED")

        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.ads.close)

        logger.info("Supervisor shutdown complete")

    # -------------------- views -------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "ai_url": self.ai_url,
            "worker_script": str(self.worker_script),
            "accounts": [m.to_dict() for m in self.accounts.values()],
        }


# ---------------------------------------------------------------------------
# Subprocess stdout/stderr piping
# ---------------------------------------------------------------------------

async def _stream_to_logger(
    stream: asyncio.StreamReader | None,
    log: logging.Logger,
    level: int,
    tag: str,
) -> None:
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            log.log(level, "[worker:%s] %s", tag, line.decode(errors="replace").rstrip())
    except Exception:  # noqa: BLE001
        log.debug("stream pipe ended for %s", tag, exc_info=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

class AddAccountRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    proxy: dict[str, Any] | None = None


def build_app(supervisor: Supervisor) -> FastAPI:
    app = FastAPI(title="wa-automation orchestrator")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "accounts": len(supervisor.accounts)}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return supervisor.status()

    @app.post("/accounts", status_code=201)
    async def add_account(req: AddAccountRequest) -> dict[str, Any]:
        try:
            managed = await supervisor.add_account(req.name, req.proxy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return managed.to_dict()

    @app.delete("/accounts/{user_id}")
    async def delete_account(user_id: str) -> dict[str, Any]:
        try:
            await supervisor.remove_account(user_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown account: {user_id}")
        return {"ok": True, "user_id": user_id}

    return app


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def _amain() -> int:
    config_path = Path(os.environ.get("WA_CONFIG", _REPO_ROOT / "config.yaml"))
    config = load_config(config_path)

    _configure_logging(config.get("app", {}).get("log_level", "INFO"))
    logger.info("Loaded config from %s", config_path)

    orch_cfg = config.get("orchestrator") or {}
    host = str(orch_cfg.get("host", "0.0.0.0"))
    port = int(orch_cfg.get("port", 8080))
    state_dir = Path(orch_cfg.get("state_dir") or (_REPO_ROOT / "data"))
    store = AccountStore(state_dir / "orchestrator-accounts.json")

    try:
        ads = build_from_config(config)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to build AdsPower client: %s", exc)
        return 2

    supervisor = Supervisor(config=config, config_path=config_path, ads=ads, store=store)
    app = build_app(supervisor)

    # Configure uvicorn but disable its own signal handling so we can manage
    # graceful shutdown ourselves.
    uv_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=str(config.get("app", {}).get("log_level", "info")).lower(),
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(uv_config)
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()

    def _signal_handler(signum: int) -> None:
        logger.info("Received signal %s; initiating shutdown", signal.Signals(signum).name)
        shutdown_requested.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler, sig)

    # Bring up everything.
    try:
        await supervisor.start()
    except Exception:  # noqa: BLE001
        logger.exception("Supervisor failed to start")
        await supervisor.shutdown()
        return 1

    server_task = asyncio.create_task(server.serve(), name="uvicorn-serve")
    shutdown_task = asyncio.create_task(shutdown_requested.wait(), name="shutdown-wait")

    logger.info("Orchestrator HTTP API on http://%s:%d", host, port)

    done, _ = await asyncio.wait(
        {server_task, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Initiate shutdown.
    server.should_exit = True
    if not shutdown_task.done():
        shutdown_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_task

    if server_task in done and server_task.exception():
        logger.error("HTTP server crashed: %s", server_task.exception())

    await supervisor.shutdown()

    if not server_task.done():
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server_task, timeout=5.0)
        if not server_task.done():
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await server_task

    logger.info("Orchestrator stopped")
    return 0


def main() -> None:
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
