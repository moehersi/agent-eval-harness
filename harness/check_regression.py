"""Compare results/<target>/latest.json against targets/<target>/baseline.json.

    python harness/check_regression.py --target riyadh_sql
    python harness/check_regression.py --target riyadh_sql --update-baseline

Exits non-zero when any of these is true:
  * deterministic accuracy dropped by more than 5 percentage points
  * judge pass rate dropped by more than 5 percentage points
  * p95 latency rose by more than 30%
  * any case that passed in the baseline fails twice in a row (see below)

A case that passed in the baseline and fails now is run once more before it
counts. Two identical back-to-back runs of this agent flipped 2 of 49 cases, so
a single failure is not evidence of a regression - temperature 0 does not make
an LLM deterministic. A case that fails twice does count. Re-runs are reported
either way, and `--no-rerun` turns them off (for offline use, or when no API key
is available).

Prints a markdown diff table either way, so CI can paste it into a job summary.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import adapter  # noqa: E402

ACCURACY_DROP = 0.05       # percentage points, as a fraction
JUDGE_DROP = 0.05
LATENCY_RISE = 0.30        # relative


def fmt(value, kind="pct") -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}" if kind == "pct" else f"{value:.2f}s"


def delta(new, old, kind="pct") -> str:
    if new is None or old is None:
        return "-"
    d = new - old
    sign = "+" if d >= 0 else ""
    return f"{sign}{d * 100:.1f} pp" if kind == "pct" else f"{sign}{d:.2f}s"


def case_status(record: dict) -> tuple[bool, bool]:
    """(deterministic pass, judge pass) for one case."""
    return bool(record.get("score")), record.get("verdict") == "PASS"


def rerun(target: str, case_ids: list[str]) -> dict[str, tuple[bool, bool]]:
    """Run these cases once more and return their fresh (score, judge pass).

    Not cached on purpose: a cached re-run would replay the same answer and prove
    nothing. Raises if the agent or the judge cannot be reached - the caller treats
    that as "could not clear the case", never as a pass.
    """
    from harness import judge, llm  # imported here so --no-rerun needs no LLM client

    impl = adapter.load(target)
    by_id = {c["id"]: c for c in adapter.load_suite(target)}
    client = llm.client()
    fresh = {}
    for case_id in case_ids:
        case = by_id[case_id]
        output = impl.run(case)
        score = bool(impl.score(case, output)) if hasattr(impl, "score") else False
        gold = impl.gold(case) if hasattr(impl, "gold") else None
        verdict = judge.grade(case, output["answer"], gold, client=client)
        fresh[case_id] = (score, verdict["verdict"] == "PASS")
    return fresh


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True)
    ap.add_argument("--update-baseline", action="store_true",
                    help="copy the latest run over the committed baseline")
    ap.add_argument("--no-rerun", action="store_true",
                    help="do not re-run newly failing cases; fail on the first failure")
    args = ap.parse_args()

    latest_path = adapter.results_dir(args.target) / "latest.json"
    baseline_path = adapter.target_dir(args.target) / "baseline.json"
    if not latest_path.exists():
        print(f"no run to check: {latest_path} is missing. Run harness/run.py first.")
        return 2
    latest = json.loads(latest_path.read_text(encoding="utf-8"))

    if args.update_baseline:
        # The baseline keeps the metrics plus each case's pass/fail, not the full
        # answers: it is a tripwire, not an archive.
        baseline = {
            "target": latest["target"],
            "finished_at": latest["finished_at"],
            "agent_code_hash": latest["agent_code_hash"],
            "models": latest["models"],
            "metrics": latest["metrics"],
            "cases": {r["id"]: {"score": bool(r["score"]), "verdict": r["verdict"]}
                      for r in latest["cases_detail"]},
        }
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f'updated {baseline_path} from the run of {latest["finished_at"]}')
        return 0

    if not baseline_path.exists():
        print(f"no baseline at {baseline_path}.\n"
              f"Review the run, then accept it with:\n"
              f"  python harness/check_regression.py --target {args.target} --update-baseline")
        return 2
    base = json.loads(baseline_path.read_text(encoding="utf-8"))

    new, old = latest["metrics"], base["metrics"]
    failures = []

    if new["accuracy"] is not None and old.get("accuracy") is not None:
        if old["accuracy"] - new["accuracy"] > ACCURACY_DROP:
            failures.append(f'accuracy fell {(old["accuracy"] - new["accuracy"]) * 100:.1f} pp '
                            f'(limit {ACCURACY_DROP * 100:.0f} pp)')
    if new["judge_pass_rate"] is not None and old.get("judge_pass_rate") is not None:
        if old["judge_pass_rate"] - new["judge_pass_rate"] > JUDGE_DROP:
            failures.append(f'judge pass rate fell {(old["judge_pass_rate"] - new["judge_pass_rate"]) * 100:.1f} pp '
                            f'(limit {JUDGE_DROP * 100:.0f} pp)')
    if old.get("latency_p95_s"):
        rise = (new["latency_p95_s"] - old["latency_p95_s"]) / old["latency_p95_s"]
        if rise > LATENCY_RISE:
            failures.append(f'p95 latency rose {rise:.0%} (limit {LATENCY_RISE:.0%})')

    now = {r["id"]: case_status(r) for r in latest["cases_detail"]}
    suspects, missing = {}, []
    for case_id, was in base.get("cases", {}).items():
        if case_id not in now:
            missing.append(case_id)
            continue
        score_now, judge_now = now[case_id]
        broke = []
        if was.get("score") and not score_now:
            broke.append("deterministic")
        if was.get("verdict") == "PASS" and not judge_now:
            broke.append("judge")
        if broke:
            suspects[case_id] = broke

    # A single failure is not a regression on a non-deterministic agent: give each
    # suspect exactly one more go, and only count the ones that fail twice.
    confirmed, flipped, rerun_error = dict(suspects), {}, ""
    if suspects and not args.no_rerun:
        try:
            fresh = rerun(args.target, sorted(suspects))
        except Exception as e:  # fail closed: an unreachable agent clears nothing
            rerun_error = f"{type(e).__name__}: {e}"
        else:
            confirmed = {}
            for case_id, broke in suspects.items():
                score_ok, judge_ok = fresh[case_id]
                still = [d for d in broke if (d == "deterministic" and not score_ok)
                         or (d == "judge" and not judge_ok)]
                (confirmed if still else flipped)[case_id] = still or broke

    if confirmed:
        detail = ", ".join(f'{cid} ({"+".join(dims)})' for cid, dims in sorted(confirmed.items()))
        twice = "" if args.no_rerun or rerun_error else " twice"
        failures.append(f"{len(confirmed)} case(s) that passed in the baseline now fail{twice}: {detail}")
    if missing:
        failures.append(f'{len(missing)} baseline case(s) missing from this run: {", ".join(missing)}')

    newly_passing = [cid for cid, (s, j) in now.items()
                     if cid in base.get("cases", {})
                     and ((s and not base["cases"][cid].get("score"))
                          or (j and base["cases"][cid].get("verdict") != "PASS"))]

    print(f'## {args.target} vs baseline ({base["finished_at"]})\n')
    print("| metric | baseline | latest | change |")
    print("|---|---|---|---|")
    print(f'| deterministic accuracy | {fmt(old.get("accuracy"))} | {fmt(new["accuracy"])} '
          f'| {delta(new["accuracy"], old.get("accuracy"))} |')
    print(f'| judge pass rate | {fmt(old.get("judge_pass_rate"))} | {fmt(new["judge_pass_rate"])} '
          f'| {delta(new["judge_pass_rate"], old.get("judge_pass_rate"))} |')
    print(f'| p50 latency | {fmt(old.get("latency_p50_s"), "s")} | {fmt(new["latency_p50_s"], "s")} '
          f'| {delta(new["latency_p50_s"], old.get("latency_p50_s"), "s")} |')
    print(f'| p95 latency | {fmt(old.get("latency_p95_s"), "s")} | {fmt(new["latency_p95_s"], "s")} '
          f'| {delta(new["latency_p95_s"], old.get("latency_p95_s"), "s")} |')
    print(f'| agent tokens in/out | {old.get("tokens_in", "-")}/{old.get("tokens_out", "-")} '
          f'| {new["tokens_in"]}/{new["tokens_out"]} | |')
    print(f'| judge tokens in/out | {old.get("judge_tokens_in", "-")}/{old.get("judge_tokens_out", "-")} '
          f'| {new["judge_tokens_in"]}/{new["judge_tokens_out"]} | |')
    print(f'| cost per question | ${old.get("cost_usd_per_question", 0):.8f} '
          f'| ${new["cost_usd_per_question"]:.8f} | |')
    print(f'| cases | {len(base.get("cases", {}))} | {new["cases"]} | |')

    if latest["agent_code_hash"] != base.get("agent_code_hash"):
        print(f'\nAgent source changed: `{base.get("agent_code_hash")}` -> `{latest["agent_code_hash"]}`.')
    if newly_passing:
        print(f'\nNewly passing: {", ".join(sorted(newly_passing))}')
    if flipped:
        print(f'\nPassed on the re-run, so not counted as a regression: '
              f'{", ".join(f"{cid} ({chr(43).join(dims)})" for cid, dims in sorted(flipped.items()))}. '
              f'This agent flips a case or two between identical runs.')
    if rerun_error:
        print(f"\nCould not re-run the failing cases ({rerun_error}), so each one counts on its "
              f"first failure.")
    if suspects and args.no_rerun:
        print("\nRe-runs disabled (--no-rerun): each failure counts on its first occurrence.")

    if failures:
        print("\n**REGRESSION**")
        for f in failures:
            print(f"- {f}")
        for case_id in sorted(confirmed):
            record = next(r for r in latest["cases_detail"] if r["id"] == case_id)
            print(f'\n  - `{case_id}` {record["question"][:70]}\n'
                  f'    answer: {record["answer"][:160]}\n'
                  f'    judge: {record["verdict"]} - {record["reason"][:120]}')
        return 1

    print("\nNo regression.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
