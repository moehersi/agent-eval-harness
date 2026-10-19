"""Adapter for riyadh-sql-agent (LangGraph text-to-SQL with a Reflexion loop).

The agent lives in its own repo. `SQL_AGENT_PATH` points at that checkout
(default: `../riyadh-sql-agent`, i.e. a sibling folder). Nothing in the agent
repo is modified from here; we import it, meter its LLM calls, and reuse its
own row-matching function for the deterministic score.

Runs use `use_lessons=False` on purpose. Reflexion writes `lessons.json`, so a
run with lessons on depends on every run before it — fine for the agent's own
demo, useless for regression detection, which needs the same input to produce
the same measurement. Retries on a failed query still happen (max_attempts=3).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from harness.llm import RETRIES, usage

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
    """Wraps the agent's LLM: adds retry/backoff and totals the tokens it reports."""

    def __init__(self, llm):
        self._llm = llm.with_retry(stop_after_attempt=RETRIES)
        self.tokens_in = self.tokens_out = 0

    def reset(self):
        self.tokens_in = self.tokens_out = 0

    def invoke(self, *args, **kwargs):
        message = self._llm.invoke(*args, **kwargs)
        tin, tout = usage(message)
        self.tokens_in += tin
        self.tokens_out += tout
        return message

    def __getattr__(self, name):
        return getattr(self._llm, name)


agent.llm = _Meter(agent.llm)  # agent.generate/reflect look the global up at call time


def run(case: dict) -> dict:
    agent.llm.reset()
    t0 = time.perf_counter()
    state = agent.ask(case["question"], use_lessons=False, max_attempts=MAX_ATTEMPTS)
    latency = time.perf_counter() - t0
    return {
        # The agent's answer node fills state["answer"]; empty string until it exists.
        "answer": state.get("answer") or "",
        "raw": {
            "sql": state.get("sql", ""),
            "rows": state.get("rows", []),
            "error": state.get("error", ""),
            "attempts": state.get("attempts", 0),
        },
        "latency_s": latency,
        "tokens_in": agent.llm.tokens_in,
        "tokens_out": agent.llm.tokens_out,
    }


def score(case: dict, output: dict) -> bool:
    """Deterministic check: gold rows for a query, no rows at all for a decline."""
    rows = output["raw"].get("rows") or []
    if case["kind"] == "decline":
        # The agent has no explicit decline path, so the only deterministic signal
        # available is that it produced no data. The judge grades the wording.
        return not rows
    return bool(rows) and correct(rows, agent.run_sql(case["gold_sql"]))
