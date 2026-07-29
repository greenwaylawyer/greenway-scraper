"""Tests for the Gemini provider client.

These tests do NOT require the google-genai SDK or a real API key. They inject
fakes via sys.modules so the lazy import inside GeminiClient.__init__ succeeds.
"""

import json
import sys
import types

import pytest

from ai.providers.gemini import GeminiClient


def _install_fake_genai(monkeypatch, response_text: str):
    """Inject a fake `google.genai` module into sys.modules."""
    captured: dict = {}

    class _FakeResponse:
        def __init__(self, text):
            self.text = text

    class _FakeModels:
        async def generate_content(self, *, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return _FakeResponse(response_text)

    class _FakeAio:
        def __init__(self):
            self.models = _FakeModels()

    class _FakeClient:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.aio = _FakeAio()

    class _FakeConfig:
        def __init__(self, *args, **kwargs):
            captured["config_kwargs"] = kwargs

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = _FakeClient
    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = _FakeConfig
    fake_genai.types = fake_types

    # Ensure `from google import genai` works by also registering the parent pkg.
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    return captured


def test_gemini_client_requires_api_key(monkeypatch):
    """Missing GEMINI_API_KEY should raise ValueError."""
    _install_fake_genai(monkeypatch, "ok")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiClient(model="gemini-2.5-flash")


def test_gemini_client_requires_sdk(monkeypatch):
    """If the SDK import fails, a friendly ImportError is raised."""
    # Force the lazy import to fail by removing google.* from sys.modules.
    for mod in list(sys.modules):
        if mod == "google" or mod.startswith("google."):
            monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(ImportError, match="google-genai"):
        GeminiClient(model="gemini-2.5-flash")


async def test_complete_returns_text(monkeypatch):
    """complete() should return response.text and pass system/user through."""
    captured = _install_fake_genai(monkeypatch, "hello world")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    client = GeminiClient(model="gemini-2.5-flash")
    text = await client.complete(system="be brief", user="say hi", max_tokens=64)

    assert text == "hello world"
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["contents"] == "say hi"
    assert captured["config_kwargs"]["system_instruction"] == "be brief"
    assert captured["config_kwargs"]["max_output_tokens"] == 64


async def test_complete_json_parses_plain_json(monkeypatch):
    _install_fake_genai(monkeypatch, '{"practice_area": "Immigration"}')
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    client = GeminiClient(model="gemini-2.5-flash")
    result = await client.complete_json(system="s", user="u")

    assert result == {"practice_area": "Immigration"}


async def test_complete_json_strips_markdown_fences(monkeypatch):
    """Gemini often wraps JSON in ```json ... ``` fences; these must be stripped."""
    _install_fake_genai(monkeypatch, "```json\n{\"a\": 1}\n```")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    client = GeminiClient(model="gemini-2.5-flash")
    result = await client.complete_json(system="s", user="u")

    assert result == {"a": 1}


async def test_complete_json_raises_on_non_json(monkeypatch):
    _install_fake_genai(monkeypatch, "this is not json")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    client = GeminiClient(model="gemini-2.5-flash")
    with pytest.raises(ValueError, match="non-JSON"):
        await client.complete_json(system="s", user="u")
