"""Google Gemini provider.

Wraps the google-genai SDK with the same public surface as AnthropicClient:
- complete(system, user, max_tokens) -> str
- complete_json(system, user, max_tokens) -> parsed JSON

Includes retry logic, rate limiting (simple token-bucket), and JSON response
extraction. This is a drop-in alternative to ai/providers/anthropic.py.
"""

import asyncio
import json
import os
import re
import time
from typing import Any, Optional

from ai.config import rate_limit_config
from utils.logger import get_logger

logger = get_logger(__name__)


class GeminiClient:
    """Thin async wrapper around the Google GenAI (Gemini) API."""

    def __init__(self, model: str):
        try:
            from google import genai
            from google.genai import types  # noqa: F401  (kept for config construction)
            self._genai = genai
            self._types = types
        except ImportError:
            raise ImportError(
                "google-genai package is not installed. Run: pip install google-genai"
            )

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable is not set")

        self._client = self._genai.Client(api_key=api_key)
        self.model = model

        cfg = rate_limit_config()
        self._max_retries: int = int(cfg.get("max_retries", 3))
        self._retry_delay: float = float(cfg.get("retry_delay_seconds", 5))

        # Simple rate-limiter state
        self._rpm: int = int(cfg.get("requests_per_minute", 50))
        self._tokens: float = float(self._rpm)
        self._last_refill: float = time.monotonic()

    def _consume_token(self) -> None:
        """Block (async sleep) if rate limit is exhausted."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        refill = elapsed * (self._rpm / 60.0)
        self._tokens = min(self._rpm, self._tokens + refill)
        self._last_refill = now

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
    ) -> str:
        """
        Send a generate_content request and return the text content.

        Retries on rate-limit and transient errors with exponential back-off.
        """
        # Rate limiter — wait if needed
        while self._tokens < 1:
            await asyncio.sleep(1)
            self._consume_token()
        self._tokens -= 1

        config = self._types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )

        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=user,
                    config=config,
                )
                text = response.text
                logger.debug("Gemini call complete", model=self.model)
                return text
            except Exception as e:
                last_error = e
                wait = self._retry_delay * (2 ** attempt)
                logger.warning(
                    "Gemini API error, retrying",
                    attempt=attempt + 1,
                    error=str(e),
                    wait=wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(f"Gemini API failed after {self._max_retries} retries: {last_error}")

    async def complete_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
    ) -> Any:
        """
        Like complete() but parses and returns JSON.

        Strips markdown fences if the model wraps its response in ```json ... ```.
        Raises ValueError if response cannot be parsed as JSON.
        """
        text = await self.complete(system=system, user=user, max_tokens=max_tokens)

        # Strip optional markdown code fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse AI JSON response", raw=text[:200], error=str(exc))
            raise ValueError(f"AI returned non-JSON response: {text[:200]}") from exc
