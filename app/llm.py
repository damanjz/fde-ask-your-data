"""LLM client (Ollama by default, Anthropic optional). Copied from the support agent, trimmed to chat.

One system message per request: the local chat template rejects a second one with HTTP 500.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.config import settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class LLMError(RuntimeError):
    """Raised when the provider cannot be reached or returns something unusable."""


@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    provider: str = ""
    model: str = ""


def _post(url: str, payload: dict, headers: dict | None = None) -> dict:
    """POST JSON with a timeout and one retry on transport errors and 5xx."""
    last: Exception | None = None
    for _ in range(2):
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=settings.llm_timeout_s)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            last = exc
    raise LLMError(f"request to {url} failed: {last!r}") from last


def check_messages(messages: list[dict]) -> None:
    """The chat template needs exactly one system message and it must come first."""
    systems = [i for i, m in enumerate(messages) if m["role"] == "system"]
    if len(systems) > 1 or (systems and systems[0] != 0):
        raise LLMError("exactly one system message is allowed, and it must be first")


def chat(messages: list[dict]) -> LLMResponse:
    """Send a chat turn. Messages use the Ollama shape; the Anthropic provider converts them."""
    check_messages(messages)
    start = time.monotonic()
    resp = _chat_anthropic(messages) if settings.llm_provider == "anthropic" else _chat_ollama(messages)
    resp.latency_ms = int((time.monotonic() - start) * 1000)
    return resp


def _chat_ollama(messages: list[dict]) -> LLMResponse:
    payload = {
        "model": settings.ollama_model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "seed": 7},
    }
    return parse_ollama(_post(f"{settings.ollama_url}/api/chat", payload))


def parse_ollama(data: dict) -> LLMResponse:
    msg = data.get("message") or {}
    return LLMResponse(
        content=(msg.get("content") or "").strip(),
        prompt_tokens=data.get("prompt_eval_count", 0),
        completion_tokens=data.get("eval_count", 0),
        provider="ollama",
        model=data.get("model", settings.ollama_model),
    )


def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split out the system prompt; the rest passes through as user/assistant turns."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    return system, rest


def parse_anthropic(data: dict) -> LLMResponse:
    text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage", {})
    return LLMResponse(
        content=text.strip(),
        prompt_tokens=usage.get("input_tokens", 0),
        completion_tokens=usage.get("output_tokens", 0),
        provider="anthropic",
        model=data.get("model", settings.anthropic_model),
    )


def _chat_anthropic(messages: list[dict]) -> LLMResponse:
    if not settings.anthropic_api_key:
        raise LLMError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
    system, converted = to_anthropic_messages(messages)
    payload = {"model": settings.anthropic_model, "max_tokens": 1024, "system": system, "messages": converted}
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    return parse_anthropic(_post(ANTHROPIC_URL, payload, headers))
