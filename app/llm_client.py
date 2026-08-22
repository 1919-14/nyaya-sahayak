"""
Single choke point for all LLM calls in this project.

Every other module (generator.py, query_rewriter.py, documents.py) calls
`complete()` or `stream()` from here instead of importing a provider SDK
directly. That means switching providers — Groq, OpenAI, OpenRouter,
Together, a local vLLM/Ollama server, whatever — is a `.env` edit only:

    LLM_API_KEY=...
    LLM_BASE_URL=https://api.groq.com/openai/v1
    LLM_MODEL=llama-3.3-70b-versatile

No code in this repo needs to change when the provider changes, as long as
it exposes an OpenAI-compatible /chat/completions endpoint (Groq, OpenAI,
OpenRouter, Together, Fireworks, and most self-hosted gateways all do).
"""
from __future__ import annotations

from typing import Iterator

from openai import OpenAI

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

_client: OpenAI | None = (
    OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None) if LLM_API_KEY else None
)

NOT_CONFIGURED_MSG = (
    "The LLM provider isn't configured yet. Set LLM_API_KEY (and LLM_BASE_URL / "
    "LLM_MODEL if you're not using the Groq default) in your .env file, then "
    "restart the backend."
)


def available() -> bool:
    return _client is not None


def complete(system: str, user: str, max_tokens: int = 800, temperature: float = 0.4) -> str:
    """Single non-streaming completion. Raises if no provider is configured —
    callers that need a graceful fallback should check `available()` first."""
    if not _client:
        raise RuntimeError(NOT_CONFIGURED_MSG)

    response = _client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def stream(system: str, user: str, max_tokens: int = 900, temperature: float = 0.4) -> Iterator[str]:
    """Yields text deltas as they arrive. If no provider is configured, yields
    a single explanatory message instead of raising, so streaming endpoints
    degrade gracefully in the UI rather than erroring out silently."""
    if not _client:
        yield NOT_CONFIGURED_MSG
        return

    response = _client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    for chunk in response:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            yield piece
