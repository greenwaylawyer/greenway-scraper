"""AI provider factory.

Resolves which provider client to instantiate based on config/ai.yaml's
top-level `provider:` key. This is the single switch point — adding a new
provider means editing only this file.

Supported providers:
- "gemini"     -> GeminiClient    (default)
- "anthropic"  -> AnthropicClient
"""

from __future__ import annotations

from ai import config as ai_config

DEFAULT_PROVIDER = "gemini"


def get_client(feature: str):
    """
    Return a provider client configured for the given feature.

    The model name is resolved per-feature from config (falling back to the
    top-level `model:`). Imports are lazy so an unused provider's SDK is never
    required to be installed.

    Raises ImportError/ValueError from the underlying client if its SDK or
    API key is missing — callers are expected to catch and degrade gracefully.
    """
    provider = ai_config.get().get("provider", DEFAULT_PROVIDER)
    model = ai_config.feature_model(feature)

    if provider == "anthropic":
        from ai.providers.anthropic import AnthropicClient
        return AnthropicClient(model=model)

    # Default and any unknown value -> Gemini
    from ai.providers.gemini import GeminiClient
    return GeminiClient(model=model)
