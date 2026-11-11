import asyncio
import json

import aiohttp
import pytest

from utils.apify_client import ApifyClient, ApifyError


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, steps, recorded):
        self._steps = steps
        self._recorded = recorded

    def request(self, method, url, params=None, json=None, headers=None):
        self._recorded.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
            }
        )
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return _FakeResponse(step[0], step[1])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_get_run_status_retries_on_502_html_and_uses_bearer_header(monkeypatch):
    steps = [
        (502, "<html><body>Bad Gateway</body></html>"),
        (503, "<html><body>Service Unavailable</body></html>"),
        (200, json.dumps({"data": {"status": "SUCCEEDED"}})),
    ]
    recorded = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kw: _FakeSession(steps, recorded),
    )

    client = ApifyClient(token="apify_api_secret_token", retry_delays=(0.0, 0.0))
    status = asyncio.run(client.get_run_status("run123"))

    assert status == "SUCCEEDED"
    assert len(recorded) == 3
    for call in recorded:
        assert call["headers"]["Authorization"] == "Bearer apify_api_secret_token"
        assert "token=" not in call["url"]
        assert call["params"] is None


def test_retries_network_errors_and_raises_apify_error_when_exhausted(monkeypatch):
    steps = [
        aiohttp.ClientConnectionError("reset by peer"),
        (502, "<html>502 Bad Gateway</html>"),
        (502, "<html>502 Bad Gateway</html>"),
    ]
    recorded = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kw: _FakeSession(steps, recorded),
    )

    client = ApifyClient(token="apify_api_secret_token", retry_delays=(0.0, 0.0))
    with pytest.raises(ApifyError, match="Apify get_run_status failed \\(502\\)"):
        asyncio.run(client.get_run_status("run123"))
