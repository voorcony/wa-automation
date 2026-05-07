"""AI Engine FastAPI service.

Run with:
    python -m uvicorn ai_engine.main:app --host 0.0.0.0 --port 8082
or directly:
    python -m ai_engine.main
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from product_store import ProductStore
from deepseek_client import DeepSeekClient
from rag import RAGEngine
import prompts  # noqa: F401  (re-exported for downstream consumers)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _interpolate_env(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} placeholders in a config tree."""
    if isinstance(value, str):
        import re

        pattern = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

        def repl(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(var_name, default)

        return pattern.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def load_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    """Load ../config.yaml and interpolate environment variables."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _interpolate_env(raw)


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = load_config()

ai_cfg = config.get("ai_engine", {}) or {}
deepseek_cfg = ai_cfg.get("deepseek", {}) or {}

api_key = deepseek_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or "sk-placeholder"
base_url = deepseek_cfg.get("api_base") or "https://api.deepseek.com/v1"
model = deepseek_cfg.get("model") or "deepseek-chat"
temperature = float(deepseek_cfg.get("temperature", 0.7))
max_tokens = int(deepseek_cfg.get("max_tokens", 1024))

product_store = ProductStore(config=config)
rag_engine = RAGEngine(product_store)
deepseek_client = DeepSeekClient(
    api_key=api_key,
    base_url=base_url,
    model=model,
    temperature=temperature,
    max_tokens=max_tokens,
)

app = FastAPI(title="WA Automation AI Engine", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    try:
        count = product_store.refresh()
        logger.info("Startup: loaded %d products", count)
    except Exception as e:
        logger.error("Startup: product refresh failed: %s", e)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    account_id: str
    from_: str = Field(..., alias="from")
    body: str
    conversation_history: list[ChatMessage] | None = None

    class Config:
        populate_by_name = True


class ChatResponse(BaseModel):
    reply: str
    products: list[dict[str, Any]]
    account_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    history = (
        [m.model_dump() for m in req.conversation_history]
        if req.conversation_history
        else None
    )

    try:
        system_prompt, messages, products = rag_engine.build_chat_messages(
            req.body, history
        )
    except Exception as e:
        logger.exception("RAG build failed")
        raise HTTPException(status_code=500, detail=f"RAG error: {e}") from e

    try:
        reply = deepseek_client.generate(system_prompt, messages)
    except Exception as e:
        logger.exception("DeepSeek generation failed")
        raise HTTPException(status_code=502, detail=f"LLM error: {e}") from e

    return ChatResponse(reply=reply, products=products, account_id=req.account_id)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "products_loaded": len(product_store.products)}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    host = ai_cfg.get("host", "0.0.0.0")
    port = int(ai_cfg.get("port", 8082))
    uvicorn.run(app, host=host, port=port)
