"""Run one target's suite and write results/<target>/latest.json.

    python harness/run.py --target riyadh_sql

Agent outputs are cached under .cache/<target>/<agent code hash>/, so a re-run
after an unrelated change does not spend the agent's quota again. Change the
agent's source or a case's text and its cache entry is a miss. The judge is
asked once per case per run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import adapter, judge, llm  # noqa: E402

# USD per 1M tokens, (input, output). Gemini's free tier bills nothing, so the
# cost columns are honest zeros - the token counts are the real number to watch.
PRICES = {
    "gemini-flash-lite-latest": (0.0, 0.0),  # free tier
    "gemini-2.5-flash-lite": (0.10, 0.40),   # paid tier, for when the free quota is gone
    "gemini-2.5-flash": (0.30, 2.50),
    "llama3.2": (0.0, 0.0),                  # local Ollama
}

CACHE = Path(__file__).resolve().parent.parent / ".cache"


def cache_path(target: str, case: dict, code_hash: str) -> Path:
    case_hash = hashlib.sha256(json.dumps(case, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    return CACHE / target / code_hash / f"{case['id']}-{case_hash}.json"


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. n is small (tens of cases); interpolation would be noise."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))], 3)


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return round((tokens_in * price_in + tokens_out * price_out) / 1e6, 6)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True)
    ap.add_argument("--limit", type=int, help="run only the first N cases (for smoke tests)")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached agent outputs")
    args = ap.parse_args()

    target = args.target
    impl = adapter.load(target)
    cases = adapter.load_suite(target)[: args.limit]
    code_hash = impl.code_hash() if hasattr(impl, "code_hash") else "nohash"
    agent_model = os.getenv("MODEL", "gemini-flash-lite-latest")
    judge_client = llm.client()

    records, hits = [], 0
    for i, case in enumerate(cases, 1):
        path = cache_path(target, case, code_hash)
        cached = path.exists() and not args.no_cache
        if cached:
            output = json.loads(path.read_text(encoding="utf-8"))
            hits += 1
        else:
            output = impl.run(case)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(output, ensure_ascii=False, default=str), encoding="utf-8")

        passed = impl.score(case, output) if hasattr(impl, "score") else None
        gold = impl.gold(case) if hasattr(impl, "gold") else None
        verdict = judge.grade(case, output["answer"], gold, client=judge_client)

        records.append({
            "id": case["id"],
            "kind": case["kind"],
            "lang": case["lang"],
            "tags": case.get("tags", []),
            "question": case["question"],
            "answer": output["answer"],
            "score": passed,
            "verdict": verdict["verdict"],
            "reason": verdict["reason"],
            "latency_s": round(output["latency_s"], 3),
            "tokens_in": output["tokens_in"],
            "tokens_out": output["tokens_out"],
            "overhead_s": output.get("overhead_s", 0.0),
            "llm_retries": output.get("llm_retries", 0),
            "judge_tokens_in": verdict["tokens_in"],
            "judge_tokens_out": verdict["tokens_out"],
            "raw": output["raw"],
            "cached": cached,
        })
        print(f'[{i}/{len(cases)}] {case["id"]:>4} score={str(passed):>5} judge={verdict["verdict"]}'
              f' {output["latency_s"]:.1f}s  {case["question"][:46]}')

    scored = [r for r in records if r["score"] is not None]
    latencies = [r["latency_s"] for r in records]
    tokens_in = sum(r["tokens_in"] for r in records)
    tokens_out = sum(r["tokens_out"] for r in records)
    judge_in = sum(r["judge_tokens_in"] for r in records)
    judge_out = sum(r["judge_tokens_out"] for r in records)
    total_cost = cost_usd(agent_model, tokens_in, tokens_out) + cost_usd(llm.model_name(), judge_in, judge_out)
    n = len(records)

    result = {
        "target": target,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent_code_hash": code_hash,
        "models": {"agent": agent_model, "judge": llm.model_name()},
        "unpriced_models": sorted({m for m in (agent_model, llm.model_name()) if m not in PRICES}),
        "metrics": {
            "cases": n,
            "accuracy": round(sum(bool(r["score"]) for r in scored) / len(scored), 4) if scored else None,
            "judge_pass_rate": round(sum(r["verdict"] == "PASS" for r in records) / n, 4) if n else None,
            "latency_p50_s": percentile(latencies, 50),
            "latency_p95_s": percentile(latencies, 95),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "judge_tokens_in": judge_in,
            "judge_tokens_out": judge_out,
            "cost_usd": round(total_cost, 6),
            "cost_usd_per_question": round(total_cost / n, 8) if n else 0.0,
        },
        "cases_detail": records,
    }

    out = adapter.results_dir(target) / "latest.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    m = result["metrics"]
    rate = lambda v: "n/a" if v is None else f"{v:.1%}"  # a target without score() has no accuracy
    print(f'\n{target}: accuracy {rate(m["accuracy"])}  judge pass {rate(m["judge_pass_rate"])}  '
          f'p50 {m["latency_p50_s"]}s  p95 {m["latency_p95_s"]}s  '
          f'tokens {tokens_in}/{tokens_out} (+judge {judge_in}/{judge_out})  '
          f'cost ${m["cost_usd"]:.6f}  cache hits {hits}/{n}')
    retries = sum(r["llm_retries"] for r in records)
    if retries:
        # Retry time is excluded from latency_s, but a high count means the run was
        # fighting the rate limit - worth knowing before trusting the latency numbers.
        print(f"note: {retries} LLM call(s) were retried (rate limit); their wait is "
              f"excluded from the latency figures.")
    if result["unpriced_models"]:
        print(f'WARNING: no price listed for {result["unpriced_models"]}; cost reported as $0.')
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
