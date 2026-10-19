"""Suite integrity: every gold SQL must still run and still return rows.

This is what catches the data drifting out from under the suite. It needs the
agent checkout (for riyadh.db) but makes no LLM calls.
"""
import json

import pytest

from harness import adapter

TARGETS = [p.name for p in adapter.TARGETS.iterdir() if (p / "suite.jsonl").exists()]


@pytest.mark.parametrize("target", TARGETS)
def test_suite_wellformed(target):
    cases = adapter.load_suite(target)
    assert cases, f"{target}: empty suite"
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), f"{target}: duplicate case ids"
    for c in cases:
        assert c["kind"] in ("query", "decline"), c
        assert c["question"].strip(), c
        assert bool(c.get("gold_sql")) == (c["kind"] == "query"), c


def test_riyadh_sql_gold_queries_return_rows():
    adapter.load("riyadh_sql")  # puts the agent checkout on sys.path
    from agent import run_sql

    empty = [c["id"] for c in adapter.load_suite("riyadh_sql")
             if c["kind"] == "query" and not run_sql(c["gold_sql"])]
    assert not empty, f"gold SQL returned no rows for: {empty}"
