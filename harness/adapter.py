"""The one interface every target implements.

A target is a folder under `targets/<name>/` holding:
    adapter.py     a module defining `run()` and, optionally, `score()`
    suite.jsonl    one test case per line, each with at least "id" and "question"
    baseline.json  metrics of the last accepted run (written by check_regression.py)

    run(case: dict) -> dict
        answer     str    natural-language answer; this is what the LLM judge grades
        raw        any    whatever the agent returned (for debugging and score())
        latency_s  float  wall-clock seconds for the whole agent call
        tokens_in  int    0 if the backend does not report usage
        tokens_out int

    score(case: dict, output: dict) -> bool
        The deterministic check. Optional: a target with no ground truth omits it.

Nothing else is required of a target. There is no base class to inherit and no
registry to edit: `--target <name>` is the folder name.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "targets"


def load(target: str):
    """Import `targets/<target>/adapter.py`."""
    return importlib.import_module(f"targets.{target}.adapter")


def load_suite(target: str) -> list[dict]:
    text = (TARGETS / target / "suite.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def target_dir(target: str) -> Path:
    return TARGETS / target


def results_dir(target: str) -> Path:
    d = ROOT / "results" / target
    d.mkdir(parents=True, exist_ok=True)
    return d
