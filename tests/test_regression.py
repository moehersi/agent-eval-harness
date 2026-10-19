"""The regression gate: each threshold must fire, and a clean run must not. No LLM calls."""
import json
import sys

import pytest

from harness import adapter, check_regression

BASELINE = {
    "target": "fake",
    "finished_at": "2026-01-01T00:00:00+00:00",
    "agent_code_hash": "abc123",
    "models": {"agent": "m", "judge": "m"},
    "metrics": {"cases": 4, "accuracy": 1.0, "judge_pass_rate": 1.0, "latency_p50_s": 1.0,
                "latency_p95_s": 2.0, "tokens_in": 100, "tokens_out": 50, "judge_tokens_in": 80,
                "judge_tokens_out": 20, "cost_usd": 0.0, "cost_usd_per_question": 0.0},
    "cases": {f"c{i}": {"score": True, "verdict": "PASS"} for i in range(1, 5)},
}


def passing_cases(n=4):
    return [{"id": f"c{i}", "score": True, "verdict": "PASS", "question": "q", "answer": "a", "reason": "r"}
            for i in range(1, n + 1)]


def latest(accuracy=1.0, judge_rate=1.0, p95=2.0, cases=None):
    cases = passing_cases() if cases is None else cases
    return {
        "target": "fake", "finished_at": "2026-02-01T00:00:00+00:00", "agent_code_hash": "abc123",
        "models": {"agent": "m", "judge": "m"},
        "metrics": {"cases": len(cases), "accuracy": accuracy, "judge_pass_rate": judge_rate,
                    "latency_p50_s": 1.0, "latency_p95_s": p95, "tokens_in": 100, "tokens_out": 50,
                    "judge_tokens_in": 80, "judge_tokens_out": 20, "cost_usd": 0.0,
                    "cost_usd_per_question": 0.0},
        "cases_detail": cases,
    }


@pytest.fixture
def check(tmp_path, monkeypatch, capsys):
    """Runs check_regression.main() against throwaway files; -> (exit code, stdout)."""
    monkeypatch.setattr(adapter, "ROOT", tmp_path)
    monkeypatch.setattr(adapter, "TARGETS", tmp_path / "targets")
    (tmp_path / "targets" / "fake").mkdir(parents=True)

    def run(latest_doc, baseline=BASELINE, args=()):
        if baseline is not None:
            (tmp_path / "targets" / "fake" / "baseline.json").write_text(
                json.dumps(baseline), encoding="utf-8")
        if latest_doc is not None:
            (adapter.results_dir("fake") / "latest.json").write_text(
                json.dumps(latest_doc), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["check_regression", "--target", "fake", *args])
        return check_regression.main(), capsys.readouterr().out

    return run


def test_clean_run_passes(check):
    code, out = check(latest())
    assert code == 0, out
    assert "No regression" in out


def test_accuracy_drop_over_5pp_fails(check):
    code, out = check(latest(accuracy=0.90))
    assert code == 1 and "accuracy fell 10.0 pp" in out


def test_accuracy_drop_within_5pp_passes(check):
    code, out = check(latest(accuracy=0.96))
    assert code == 0, out


def test_judge_pass_rate_drop_fails(check):
    code, out = check(latest(judge_rate=0.80))
    assert code == 1 and "judge pass rate fell" in out


def test_p95_latency_rise_over_30pct_fails(check):
    code, out = check(latest(p95=2.8))
    assert code == 1 and "p95 latency rose" in out


def test_p95_latency_rise_within_30pct_passes(check):
    code, out = check(latest(p95=2.5))
    assert code == 0, out


@pytest.mark.parametrize("broken", [{"score": False, "verdict": "PASS"},
                                   {"score": True, "verdict": "FAIL"}])
def test_a_case_that_passed_before_now_failing_fails(check, broken):
    cases = passing_cases(3) + [{"id": "c4", **broken, "question": "q", "answer": "a", "reason": "r"}]
    # The rates are left unchanged on purpose: this proves the per-case gate alone
    # catches a swap that leaves the headline numbers looking fine.
    code, out = check(latest(cases=cases))
    assert code == 1 and "c4" in out


def test_missing_baseline_case_fails(check):
    code, out = check(latest(cases=passing_cases(3)))
    assert code == 1 and "missing from this run" in out


def test_no_baseline_is_not_a_pass(check):
    code, out = check(latest(), baseline=None)
    assert code == 2 and "no baseline" in out


def test_no_run_is_not_a_pass(check):
    code, out = check(None)
    assert code == 2 and "no run to check" in out


def test_update_baseline_writes_per_case_results(check, tmp_path):
    code, out = check(latest(), args=("--update-baseline",))
    assert code == 0 and "updated" in out
    written = json.loads((tmp_path / "targets" / "fake" / "baseline.json").read_text(encoding="utf-8"))
    assert written["cases"]["c1"] == {"score": True, "verdict": "PASS"}
    assert "cases_detail" not in written  # a tripwire, not an archive
