"""AI provider clients for Greenway AI services.

Providers are selected via config/ai.yaml's top-level `provider:` key through
the factory in ai/providers/factory.py. Supported providers:
- gemini    (default) -> ai.providers.gemini.GeminiClient
- anthropic            -> ai.providers.anthropic.AnthropicClient
"""
