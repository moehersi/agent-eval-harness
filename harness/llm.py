"""The harness's own LLM client, used by the judge.

Deliberately separate from whatever client an agent under test uses: the judge
must not inherit an agent's model, prompt or temperature. Backoff comes from
LangChain's `with_retry`, which already does exponential backoff with jitter —
that is what absorbs Gemini free-tier 429s.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

RETRIES = int(os.getenv("LLM_RETRIES", "5"))


def model_name() -> str:
    default = "gemini-flash-lite-latest" if os.getenv("BACKEND", "gemini") == "gemini" else "llama3.2"
    return os.getenv("JUDGE_MODEL", default)


def client():
    """A temperature-0 chat model that retries transient errors (429s included)."""
    if os.getenv("BACKEND", "gemini") == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(model=model_name(), temperature=0)
    else:
        from langchain_ollama import ChatOllama

        llm = ChatOllama(model=model_name(), temperature=0)
    return llm.with_retry(stop_after_attempt=RETRIES)


def text(content) -> str:
    """Normalize a response's .content: Gemini returns a list of parts, Ollama a plain str."""
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content or ""


def usage(message) -> tuple[int, int]:
    """(tokens_in, tokens_out) from a response, or (0, 0) if the backend reports none."""
    u = getattr(message, "usage_metadata", None) or {}
    return u.get("input_tokens", 0), u.get("output_tokens", 0)
