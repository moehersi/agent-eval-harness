"""Adapter for riyadh-sql-agent (LangGraph text-to-SQL with a Reflexion loop).

The agent lives in its own repo. `SQL_AGENT_PATH` points at that checkout
(default: `../riyadh-sql-agent`, i.e. a sibling folder). Nothing in the agent
repo is modified from here; we import it, meter its LLM calls, and reuse its
own row-matching function for the deterministic score.

Runs use `use_lessons=False` on purpose. Reflexion writes `lessons.json`, so a
run with lessons on depends on every run before it - fine for the agent's own
demo, useless for regression detection, which needs the same input to produce
the same measurement. Retries on a failed query still happen (max_attempts=3).
"""
from __future__ import annotations

import hashlib
import os
import random
import sys
import time
from pathlib import Path

from harness.llm import RETRIES, TIMEOUT_S, throttle, usage

HARNESS_ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = Path(os.getenv("SQL_AGENT_PATH") or HARNESS_ROOT.parent / "riyadh-sql-agent").resolve()
if str(AGENT_PATH) not in sys.path:
    sys.path.insert(0, str(AGENT_PATH))

import agent  # noqa: E402  the agent under test
from eval import correct  # noqa: E402  its own lenient row-matching logic, reused verbatim

if not agent.DB.exists():
    raise SystemExit(f"{agent.DB} is missing. Run `python load_db.py` in {AGENT_PATH} first.")

MAX_ATTEMPTS = int(os.getenv("SQL_AGENT_MAX_ATTEMPTS", "3"))


class _Meter:
    """Wraps the agent's LLM: throttles, retries, meters tokens, and keeps the
    harness's own waiting out of the reported latency.

    This does its own retrying instead of LangChain's `with_retry` for one reason:
    `with_retry` hides its attempts, so a 429 and the backoff that follows it land
    *inside* the timed call and get reported as agent latency. An early run read
    53.8s for a question that takes 1.4s. Here, every second not spent on the
    attempt that actually succeeded is charged to `overhead_s` and subtracted.
    """

    def __init__(self, llm):
        self._llm = llm
        # The agent builds its own client with no request timeout. Left alone, one
        # hung call stalls the whole run - which is exactly what happened once.
        for field, value in (("timeout", TIMEOUT_S), ("max_retries", 0)):
            try:
                setattr(llm, field, value)
            except Exception:  # a backend that has no such field is fine as-is
                pass
        self.tokens_in = self.tokens_out = self.retries = 0
        self.overhead_s = 0.0

    def reset(self):
        self.tokens_in = self.tokens_out = self.retries = 0
        self.overhead_s = 0.0

    def invoke(self, *args, **kwargs):
        self.overhead_s += throttle()
        for attempt in range(RETRIES):
            started = time.perf_counter()
            try:
                message = self._llm.invoke(*args, **kwargs)
            except Exception:
                self.overhead_s += time.perf_counter() - started  # a failed round-trip is not agent work
                self.retries += 1
                if attempt == RETRIES - 1:
                    raise
                backoff = min(2 ** attempt, 32) * (1 + random.random() * 0.25)  # jittered, like with_retry
                time.sleep(backoff)
                self.overhead_s += backoff
                continue
            tokens_in, tokens_out = usage(message)
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            return message

    def __getattr__(self, name):
        return getattr(self._llm, name)


agent.llm = _Meter(agent.llm)  # agent.generate/answer look the global up at call time


def run(case: dict) -> dict:
    agent.llm.reset()
    started = time.perf_counter()
    state = agent.ask(case["question"], use_lessons=False, max_attempts=MAX_ATTEMPTS)
    # Report what the agent took, not what the free tier made us wait.
    latency = time.perf_counter() - started - agent.llm.overhead_s
    return {
        "answer": state.get("answer") or "",
        "raw": {
            "sql": state.get("sql", ""),
            "rows": state.get("rows", []),
            "error": state.get("error", ""),
            "attempts": state.get("attempts", 0),
        },
        "latency_s": max(latency, 0.0),
        "overhead_s": round(agent.llm.overhead_s, 3),
        "llm_retries": agent.llm.retries,
        "tokens_in": agent.llm.tokens_in,
        "tokens_out": agent.llm.tokens_out,
    }


def score(case: dict, output: dict) -> bool:
    """Deterministic check: gold rows for a query, the exact refusal for a decline."""
    if case["kind"] == "decline":
        # Anything other than the agent's fixed refusal string is a fail, including a
        # fluent LLM sentence that happens to admit the data is missing: a decline has
        # to be a decline, not an answer that talks its way around the gap.
        return output["answer"].strip() == agent.REFUSAL[agent.lang(case["question"])]
    rows = output["raw"].get("rows") or []
    return bool(rows) and correct(rows, agent.run_sql(case["gold_sql"]))


def gold(case: dict) -> list | None:
    """The correct result rows, for the judge to grade against."""
    return None if case["kind"] == "decline" else agent.run_sql(case["gold_sql"])


def code_hash() -> str:
    """Short hash of the agent's own source, so changing the agent busts the cache."""
    h = hashlib.sha256()
    for path in sorted(AGENT_PATH.glob("*.py")):
        h.update(path.read_bytes())
    return h.hexdigest()[:12]
