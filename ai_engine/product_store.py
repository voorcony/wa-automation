"""Product store - fetches and indexes products from Feishu bitable."""

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ProductStore:
    """In-memory product store with keyword search.

    Fetches products from Feishu bitable on startup and refresh().
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.products: list[dict[str, Any]] = []
        self._last_sync = 0.0

    # ------------------------------------------------------------------
    # Feishu API helpers
    # ------------------------------------------------------------------

    def _get_tenant_token(self) -> str:
        """Get Feishu tenant access token."""
        cfg = self.config.get("feishu", {})
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": cfg.get("app_id", "cli_a9619830e2fadcd1"),
                "app_secret": cfg.get("app_secret", "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"),
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu auth failed: {data.get('msg')}")
        return data["tenant_access_token"]

    def _fetch_all_records(self, token: str) -> list[dict]:
        """Fetch all records from the Feishu bitable."""
        cfg = self.config.get("feishu", {})
        app_token = cfg.get("app_token", "G5sbb3W1qa74TJs2aqrcg03EnVg")
        table_id = cfg.get("table_id", "tblfFvFg7P1ozXFq")
        headers = {"Authorization": f"Bearer {token}"}

        records = []
        page_token = None
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            resp = httpx.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                headers=headers,
                params=params,
                timeout=30,
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu API error: {data.get('msg')}")
            items = data.get("data", {}).get("items", [])
            records.extend(items)
            if data.get("data", {}).get("has_more"):
                page_token = data["data"].get("page_token")
            else:
                break
        return records

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> int:
        """Fetch all products from Feishu and rebuild the in-memory index."""
        try:
            token = self._get_tenant_token()
            records = self._fetch_all_records(token)
        except Exception as e:
            logger.error("Failed to refresh products: %s", e)
            if not self.products:
                raise
            return len(self.products)

        products = []
        for rec in records:
            fields = rec.get("fields", {})
            name = fields.get("产品名称", "") or ""
            price = fields.get("价格", "") or ""
            img_urls = (fields.get("图片URL", "") or "").split("\n")

            if not name:
                continue

            products.append({
                "name": name.strip(),
                "price": str(price).strip(),
                "images": [u for u in img_urls if u.strip()],
                "record_id": rec.get("record_id", ""),
            })

        self.products = products
        self._last_sync = time.time()
        logger.info("Product store refreshed: %d products loaded", len(products))
        return len(products)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Search products by keyword matching in title."""
        if not self.products:
            try:
                self.refresh()
            except Exception:
                return []

        query_lower = query.lower()
        keywords = query_lower.split()

        scored = []
        for p in self.products:
            name_lower = p["name"].lower()
            score = 0
            # Full match bonus
            if query_lower in name_lower:
                score += 10
            # Individual keyword matches
            for kw in keywords:
                if kw in name_lower:
                    score += 3
            # Boost for products with images
            if p.get("images"):
                score += 1
            if score > 0:
                scored.append((score, p))

        scored.sort(key=lambda x: -x[0])
        return [p for _, p in scored[:top_k]]

    def format_for_prompt(self, products: list[dict]) -> str:
        """Format products as a text block for the AI prompt."""
        lines = []
        for i, p in enumerate(products, 1):
            name = p["name"].replace("\n", " ").strip()
            price = p.get("price", "")
            img = p["images"][0] if p.get("images") else ""
            img_info = f" - 图片: {img}" if img else ""
            price_info = f" - ¥{price}" if price else " - 已售罄"
            lines.append(f"{i}. {name}{price_info}{img_info}")
        return "\n".join(lines)

    @property
    def stats(self) -> dict:
        return {
            "product_count": len(self.products),
            "last_sync": self._last_sync,
        }
