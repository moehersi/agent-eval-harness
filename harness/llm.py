"""The harness's own LLM client, used by the judge.

Deliberately separate from whatever client an agent under test uses: the judge
must not inherit an agent's model, prompt or temperature. Backoff comes from
LangChain's `with_retry`, which already does exponential backoff with jitter —
that is what absorbs Gemini free-tier 429s.
"""
from __future__ import annotations

import os
import time

from dotenv import load_dotenv

load_dotenv()

RETRIES = int(os.getenv("LLM_RETRIES", "5"))
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "90"))  # no timeout means a hung
                                                    # request blocks CI until the runner dies
RPM = float(os.getenv("LLM_RPM", "10"))  # under the free tier flash-lite limit of 15/min
_MIN_INTERVAL = 60.0 / RPM if RPM > 0 else 0.0
_last_request = 0.0
waited_s = 0.0  # total time spent waiting on the throttle, so callers can subtract it


def model_name() -> str:
    default = "gemini-flash-lite-latest" if os.getenv("BACKEND", "gemini") == "gemini" else "llama3.2"
    return os.getenv("JUDGE_MODEL", default)


def client():
    """A temperature-0 chat model that retries transient errors (429s included)."""
    if os.getenv("BACKEND", "gemini") == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        # max_retries=0: retrying is this harness's job, and two retry layers
        # stacked turn one slow call into minutes of invisible waiting.
        llm = ChatGoogleGenerativeAI(model=model_name(), temperature=0,
                                     timeout=TIMEOUT_S, max_retries=0)
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


def throttle() -> float:
    """Space requests out to stay under the provider's per-minute limit.

    Without this, the free tier answers with 429s and `with_retry` sleeps out the
    backoff *inside* the measured call - which would make p95 latency a reading of
    Google's rate limiter rather than of the agent. Returns the seconds slept, so a
    caller timing an agent can subtract its own waiting from the measurement.
    """
    global _last_request, waited_s
    wait = _last_request + _MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
        waited_s += wait
    _last_request = time.monotonic()
    return max(wait, 0.0)
