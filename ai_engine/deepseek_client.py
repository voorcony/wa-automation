"""DeepSeek API client wrapper."""

import logging
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)


class DeepSeekClient:
    """OpenAI-compatible client for DeepSeek API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, str]] | None = None,
        **kwargs,
    ) -> str:
        """Generate a response from DeepSeek.

        Args:
            system_prompt: The system message content.
            messages: List of previous messages [{"role": "user"|"assistant", "content": "..."}]
            **kwargs: Override default model parameters.

        Returns:
            The generated text content.
        """
        msgs = [{"role": "system", "content": system_prompt}]
        if messages:
            msgs.extend(messages)

        try:
            resp = self.client.chat.completions.create(
                model=kwargs.get("model", self.model),
                messages=msgs,
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
                timeout=kwargs.get("timeout", 30),
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            logger.error("DeepSeek API error: %s", e)
            raise

    def generate_with_context(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
        **kwargs,
    ) -> str:
        """Convenience method: add a user message and optional history."""
        msgs = list(conversation_history or [])
        msgs.append({"role": "user", "content": user_message})
        return self.generate(system_prompt, messages=msgs, **kwargs)
