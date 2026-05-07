"""RAG module - connects product store with AI prompt generation."""

import logging

from .product_store import ProductStore
from .prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class RAGEngine:
    """Retrieval-Augmented Generation engine for product recommendations."""

    def __init__(self, store: ProductStore):
        self.store = store

    def build_prompt(self, user_message: str) -> tuple[str, list[dict]]:
        """Build a system prompt with relevant product context.

        Returns:
            Tuple of (filled system prompt, list of matched products).
        """
        products = self.store.search(user_message, top_k=5)
        if products:
            context = self.store.format_for_prompt(products)
        else:
            context = "（当前暂无匹配产品，可引导客户浏览其他款式）"

        prompt = SYSTEM_PROMPT.format(product_context=context)
        return prompt, products

    def build_chat_messages(
        self, user_message: str, conversation_history: list[dict] | None = None
    ) -> tuple[str, list[dict], list[dict]]:
        """Build full chat messages for the AI call.

        Returns:
            Tuple of (system_prompt, messages, matched_products).
        """
        system_prompt, products = self.build_prompt(user_message)
        history = list(conversation_history or [])

        # Add the current user message
        messages = history + [{"role": "user", "content": user_message}]

        return system_prompt, messages, products
