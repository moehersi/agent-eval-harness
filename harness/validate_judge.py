"""Measure the judge against human labels.

    python harness/validate_judge.py --target riyadh_sql --export 30
    # ... a human fills in the human_label column (PASS / FAIL) ...
    python harness/validate_judge.py --target riyadh_sql --compute

`--export` writes targets/<target>/labels.csv from the latest run, with an empty
human_label column. `--compute` reads it back and writes judge_validation.md with
raw agreement and Cohen's kappa.

The sample is stratified, not random: every case the judge marked FAIL is
included, then PASS cases are drawn with a fixed seed. On a suite the judge
mostly passes, a random 30 would contain two or three FAILs and the kappa would
be noise. The published agreement is therefore agreement *on this sample*, not a
suite-weighted rate - judge_validation.md says so.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import adapter  # noqa: E402

COLUMNS = ["id", "kind", "lang", "question", "gold", "answer", "judge_verdict", "judge_reason", "human_label"]
SEED = 0


def labels_path(target: str) -> Path:
    return adapter.target_dir(target) / "labels.csv"


def export(target: str, n: int) -> int:
    latest = json.loads((adapter.results_dir(target) / "latest.json").read_text(encoding="utf-8"))
    records = latest["cases_detail"]
    impl = adapter.load(target)
    by_id = {c["id"]: c for c in adapter.load_suite(target)}

    fails = [r for r in records if r["verdict"] == "FAIL"]
    passes = [r for r in records if r["verdict"] == "PASS"]
    random.Random(SEED).shuffle(passes)
    sample = sorted((fails + passes)[:n], key=lambda r: r["id"])

    path = labels_path(target)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in sample:
            gold = impl.gold(by_id[r["id"]]) if hasattr(impl, "gold") else None
            writer.writerow({
                "id": r["id"], "kind": r["kind"], "lang": r["lang"], "question": r["question"],
                "gold": "not answerable from this database" if gold is None else json.dumps(gold, ensure_ascii=False, default=str),
                "answer": r["answer"], "judge_verdict": r["verdict"], "judge_reason": r["reason"],
                "human_label": "",
            })
    print(f"wrote {path} with {len(sample)} rows "
          f'({len(fails)} the judge failed, {len(sample) - len(fails)} it passed).\n'
          f'Fill in human_label with PASS or FAIL for every row, then run:\n'
          f"  python harness/validate_judge.py --target {target} --compute")
    return 0


def kappa(pairs: list[tuple[str, str]]) -> tuple[float, float, dict]:
    """(raw agreement, Cohen's kappa, confusion counts) over (judge, human) label pairs."""
    n = len(pairs)
    cells = {(j, h): sum(1 for pj, ph in pairs if (pj, ph) == (j, h))
             for j in ("PASS", "FAIL") for h in ("PASS", "FAIL")}
    observed = (cells[("PASS", "PASS")] + cells[("FAIL", "FAIL")]) / n
    expected = sum(
        (sum(1 for pj, _ in pairs if pj == label) / n) * (sum(1 for _, ph in pairs if ph == label) / n)
        for label in ("PASS", "FAIL")
    )
    k = 1.0 if expected == 1 else (observed - expected) / (1 - expected)
    return observed, k, cells


def compute(target: str) -> int:
    path = labels_path(target)
    if not path.exists():
        print(f"{path} is missing. Run --export first.")
        return 2
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    pairs, unlabeled, bad = [], [], []
    for r in rows:
        human = (r.get("human_label") or "").strip().upper()
        if not human:
            unlabeled.append(r["id"])
        elif human not in ("PASS", "FAIL"):
            bad.append(f'{r["id"]}={r["human_label"]!r}')
        else:
            pairs.append((r["judge_verdict"].strip().upper(), human))

    if bad:
        print(f'human_label must be PASS or FAIL. Bad values: {", ".join(bad)}')
        return 2
    if not pairs:
        print(f"no rows in {path} have a human_label yet.")
        return 2

    observed, k, cells = kappa(pairs)
    disagreements = [r for r in rows
                     if (r.get("human_label") or "").strip().upper() in ("PASS", "FAIL")
                     and r["judge_verdict"].strip().upper() != r["human_label"].strip().upper()]

    latest = json.loads((adapter.results_dir(target) / "latest.json").read_text(encoding="utf-8"))
    md = [
        f"# Judge validation: {target}",
        "",
        f'Judge model `{latest["models"]["judge"]}`, temperature 0, prompt in `harness/judge.py`.',
        f'Labelled against the run of {latest["finished_at"]} (agent code `{latest["agent_code_hash"]}`).',
        "",
        f"| | value |",
        "|---|---|",
        f"| labelled cases | {len(pairs)} of {len(rows)} exported |",
        f"| raw agreement | {observed:.1%} |",
        f"| Cohen's kappa | {k:.3f} |",
        "",
        "| | human PASS | human FAIL |",
        "|---|---|---|",
        f'| judge PASS | {cells[("PASS", "PASS")]} | {cells[("PASS", "FAIL")]} |',
        f'| judge FAIL | {cells[("FAIL", "PASS")]} | {cells[("FAIL", "FAIL")]} |',
        "",
        "The sample is stratified, not random: every case the judge marked FAIL is in it, "
        "plus PASS cases drawn with a fixed seed. So these numbers are agreement **on this "
        "sample**, which over-weights the hard cases on purpose. They are not a "
        "suite-weighted agreement rate.",
    ]
    if unlabeled:
        md += ["", f'Unlabelled rows, excluded: {", ".join(unlabeled)}.']
    if disagreements:
        md += ["", "## Where the judge and the human disagree", ""]
        for r in disagreements:
            md += [f'- `{r["id"]}` judge **{r["judge_verdict"]}**, human **{r["human_label"].strip().upper()}**',
                   f'  - question: {r["question"][:120]}',
                   f'  - answer: {r["answer"][:200]}',
                   f'  - judge reason: {r["judge_reason"][:200]}']
    md += ["", "Regenerate with `python harness/validate_judge.py --target "
           f'{target} --compute` after editing `labels.csv`.', ""]

    out = adapter.target_dir(target) / "judge_validation.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"agreement {observed:.1%}, Cohen's kappa {k:.3f} over {len(pairs)} labelled cases")
    print(f"wrote {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True)
    ap.add_argument("--export", type=int, metavar="N", help="export N cases to labels.csv")
    ap.add_argument("--compute", action="store_true", help="read labels.csv, write judge_validation.md")
    args = ap.parse_args()
    if args.export:
        return export(args.target, args.export)
    if args.compute:
        return compute(args.target)
    ap.error("pass --export N or --compute")


if __name__ == "__main__":
    raise SystemExit(main())
