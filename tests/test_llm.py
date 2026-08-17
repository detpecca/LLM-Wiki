"""LLMClient retry behaviour (mocked urlopen; no network)."""

import io
import json
import urllib.error

import llm_wiki.llm as llm_mod
from llm_wiki.llm import LLMClient


class _FakeResp:
    def __init__(self, content: dict):
        self._raw = io.BytesIO(json.dumps(content).encode())

    def read(self):
        return self._raw.getvalue()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_response():
    return _FakeResp({"choices": [{"message": {"content": "hello"}}]})


def _client():
    return LLMClient(base_url="http://x", api_key="k", model="m")


def test_retry_succeeds_after_transient_errors(monkeypatch):
    calls = {"n": 0}

    def flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection reset")
        return _ok_response()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)  # no real waiting
    assert _client().chat([{"role": "user", "content": "hi"}]) == "hello"
    assert calls["n"] == 3


def test_retry_gives_up_after_max(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("down")))
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    try:
        _client().chat([{"role": "user", "content": "hi"}], max_retries=2)
        raise AssertionError("should have raised")
    except urllib.error.URLError:
        pass


def test_4xx_not_retried(monkeypatch):
    calls = {"n": 0}

    def auth_fail(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", auth_fail)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    try:
        _client().chat([{"role": "user", "content": "hi"}])
        raise AssertionError("should have raised")
    except urllib.error.HTTPError:
        pass
    assert calls["n"] == 1  # no retry on 4xx


# ---------------------------------------------------------- native tool calling

_TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]


def test_chat_tools_parses_tool_calls(monkeypatch):
    resp = _FakeResp({"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "call_0", "type": "function",
         "function": {"name": "wiki_search", "arguments": '{"query": "x"}'}}]}}]})
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: resp)
    out = _client().chat_tools([{"role": "user", "content": "hi"}], _TOOLS)
    assert out["content"] is None
    assert out["tool_calls"] == [
        {"id": "call_0", "name": "wiki_search", "arguments": {"query": "x"}}]


def test_chat_tools_malformed_arguments_default_empty(monkeypatch):
    resp = _FakeResp({"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c", "function": {"name": "wiki_read", "arguments": "not json"}}]}}]})
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: resp)
    out = _client().chat_tools([{"role": "user", "content": "hi"}], _TOOLS)
    assert out["tool_calls"][0]["arguments"] == {}


def test_chat_tools_raises_toolsunsupported_on_4xx(monkeypatch):
    def reject(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    try:
        _client().chat_tools([{"role": "user", "content": "hi"}], _TOOLS)
        raise AssertionError("should have raised")
    except llm_mod.ToolsUnsupported:
        pass
