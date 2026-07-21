"""Offline HTTP cassette replay helpers for integration tests.

JSON cassettes under ``tests/cassettes/`` emulate VCR-style record/replay
so network-marked tests never depend on live third-party APIs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from academic_intelligence.utils.http import HTTPClient

CASSETTES_DIR = Path(__file__).parent / "cassettes"


def load_cassette(name: str) -> Dict[str, Any]:
    """Load a JSON cassette fixture by stem name (without extension)."""
    path = CASSETTES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Cassette not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def match_cassette(
    url: str,
    params: Optional[Dict[str, Any]],
    catalog: Dict[str, Any],
) -> httpx.Response:
    """Match a request against cassette catalog entries (first match wins)."""
    parsed = urlparse(url)
    query: Dict[str, Any] = {
        k: (v[0] if isinstance(v, list) and len(v) == 1 else v)
        for k, v in parse_qs(parsed.query).items()
    }
    if params:
        for k, v in params.items():
            query[str(k)] = v

    for entry in catalog.get("interactions", []):
        req = entry.get("request", {}) or {}

        host = req.get("host")
        if host and host not in parsed.netloc:
            continue

        path_contains = req.get("path_contains")
        if path_contains:
            # Match against full URL so DOI paths with encoding still hit
            if path_contains not in parsed.path and path_contains not in url:
                continue

        required_params = req.get("params_contains") or {}
        ok = True
        for pk, pv in required_params.items():
            actual = query.get(pk)
            if actual is None:
                # also try without type coercion
                actual = query.get(str(pk))
            if actual is None:
                ok = False
                break
            if pv is not None and str(pv).lower() not in str(actual).lower():
                ok = False
                break
        if not ok:
            continue

        resp = entry.get("response", {}) or {}
        status = int(resp.get("status_code", 200))
        body = resp.get("json")
        text = resp.get("text")
        headers = resp.get("headers") or {"Content-Type": "application/json"}
        request = httpx.Request("GET", url, params=params)
        if body is not None:
            return httpx.Response(status, json=body, headers=headers, request=request)
        return httpx.Response(status, text=text or "", headers=headers, request=request)

    request = httpx.Request("GET", url, params=params)
    return httpx.Response(
        404,
        json={"error": "cassette miss", "url": url, "params": query},
        request=request,
    )


def install_cassette(monkeypatch: pytest.MonkeyPatch, name: str) -> Dict[str, Any]:
    """Monkeypatch ``HTTPClient.get`` to replay a single cassette."""
    catalog = load_cassette(name)

    async def _fake_get(
        self: HTTPClient,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        return match_cassette(url, params, catalog)

    monkeypatch.setattr(HTTPClient, "get", _fake_get)
    return catalog


def install_merged_cassettes(
    monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> Dict[str, Any]:
    """Monkeypatch ``HTTPClient.get`` to replay several cassettes merged."""
    merged: Dict[str, Any] = {"interactions": []}
    for name in names:
        data = load_cassette(name)
        merged["interactions"].extend(data.get("interactions", []))

    async def _fake_get(
        self: HTTPClient,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        return match_cassette(url, params, merged)

    monkeypatch.setattr(HTTPClient, "get", _fake_get)
    return merged
