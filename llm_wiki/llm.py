"""LLM client: OpenAI-compatible chat completion.

Configured via environment variables:
  LLM_WIKI_BASE_URL  (default: https://api.moonshot.cn/v1)
  LLM_WIKI_API_KEY   (required for real calls)
  LLM_WIKI_MODEL     (default: kimi-k2-0711-preview)

Any object with a ``chat(messages, **kwargs) -> str`` method satisfies the
interface, which is how tests plug in FakeLLM.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k2-0711-preview"


class ToolsUnsupported(Exception):
    """The endpoint rejected a request carrying a ``tools`` parameter.

    Raised by ``chat_tools`` when a tools-bearing request fails with a 4xx,
    signalling the caller to fall back to prompt-driven (JSON-action) tool use.
    Only raised when ``tools`` was actually sent, so genuine auth/quota 4xx on
    plain ``chat`` calls are never misread as "tools unsupported".
    """


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
    ):
        self.base_url = (base_url or os.environ.get("LLM_WIKI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_WIKI_API_KEY") or ""
        self.model = model or os.environ.get("LLM_WIKI_MODEL") or DEFAULT_MODEL
        self.temperature = temperature

    def _post(self, payload: dict, max_retries: int = 3) -> dict:
        """POST a chat-completion payload; return the parsed JSON response.

        Retries transient failures (network errors, timeouts, HTTP 5xx) with
        exponential backoff; 4xx errors (auth/quota) fail immediately.
        """
        if not self.api_key:
            raise RuntimeError(
                "LLM_WIKI_API_KEY is not set; cannot make LLM calls. "
                "Set it, or inject a fake client for testing."
            )
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        for attempt in range(max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 or attempt == max_retries:
                    raise
                time.sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError):
                if attempt == max_retries:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    def chat(self, messages: list[dict], max_retries: int = 3, **kwargs) -> str:
        """Send a chat completion request, return the assistant content."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.pop("temperature", self.temperature),
        }
        payload.update(kwargs)
        data = self._post(payload, max_retries)
        return data["choices"][0]["message"]["content"]

    def chat_tools(self, messages: list[dict], tools: list[dict],
                   tool_choice: str = "auto", max_retries: int = 3,
                   **kwargs) -> dict:
        """Native function-calling request.

        Returns a normalized dict ``{"content": str|None, "tool_calls": [...]}``
        where each tool call is ``{"id", "name", "arguments"}`` and arguments
        are already ``json.loads``-decoded (``{}`` if the model sent malformed
        JSON). Raises ``ToolsUnsupported`` if the endpoint rejects the ``tools``
        parameter with a 4xx, so callers can fall back to JSON-action mode.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.pop("temperature", self.temperature),
            "tools": tools,
            "tool_choice": tool_choice,
        }
        payload.update(kwargs)
        try:
            data = self._post(payload, max_retries)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise ToolsUnsupported(
                    f"endpoint rejected tools parameter (HTTP {e.code})") from e
            raise
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""),
                          "arguments": args})
        return {"content": msg.get("content"), "tool_calls": calls}
