"""Measure the judge against human labels.

    python harness/validate_judge.py --target riyadh_sql --export 30
    # ... a human fills human_label in labels-blind.csv (PASS / FAIL) ...
    python harness/validate_judge.py --target riyadh_sql --compute

`--export` writes two files:

  labels-blind.csv   what the human labels: id, kind, lang, question, gold, answer,
                     human_label. No judge verdict, no judge reason, and the rows
                     shuffled so the judge's failures are not grouped together. A
                     labeller who can see the judge's answer is not an independent
                     rater, and the agreement number would measure anchoring.
  labels.csv         the full record for the repo, in id order, with the judge's
                     verdict and reason. `--compute` joins the two by id.

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

FULL_COLUMNS = ["id", "kind", "lang", "question", "gold", "answer", "judge_verdict", "judge_reason"]
BLIND_COLUMNS = ["id", "kind", "lang", "question", "gold", "answer", "human_label"]
SAMPLE_SEED = 0
SHUFFLE_SEED = 1


def full_path(target: str) -> Path:
    return adapter.target_dir(target) / "labels.csv"


def blind_path(target: str) -> Path:
    return adapter.target_dir(target) / "labels-blind.csv"


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export(target: str, n: int, force: bool) -> int:
    blind = blind_path(target)
    if blind.exists() and not force:
        filled = [r["id"] for r in read_csv(blind) if (r.get("human_label") or "").strip()]
        if filled:
            print(f"{blind} already has {len(filled)} label(s) filled in. Refusing to overwrite "
                  f"them.\nPass --force to discard them and export a fresh file.")
            return 2

    latest = json.loads((adapter.results_dir(target) / "latest.json").read_text(encoding="utf-8"))
    records = latest["cases_detail"]
    impl = adapter.load(target)
    by_id = {c["id"]: c for c in adapter.load_suite(target)}

    fails = [r for r in records if r["verdict"] == "FAIL"]
    passes = [r for r in records if r["verdict"] == "PASS"]
    random.Random(SAMPLE_SEED).shuffle(passes)
    sample = sorted((fails + passes)[:n], key=lambda r: r["id"])

    rows = []
    for r in sample:
        gold = impl.gold(by_id[r["id"]]) if hasattr(impl, "gold") else None
        rows.append({
            "id": r["id"], "kind": r["kind"], "lang": r["lang"], "question": r["question"],
            "gold": "not answerable from this database" if gold is None
                    else json.dumps(gold, ensure_ascii=False, default=str),
            "answer": r["answer"], "judge_verdict": r["verdict"], "judge_reason": r["reason"],
            "human_label": "",
        })

    write_csv(full_path(target), FULL_COLUMNS, rows)
    shuffled = list(rows)
    random.Random(SHUFFLE_SEED).shuffle(shuffled)
    write_csv(blind, BLIND_COLUMNS, shuffled)

    print(f"wrote {blind} with {len(rows)} rows, shuffled, judge columns withheld "
          f"({len(fails)} of them the judge failed - which row is which is not shown).\n"
          f"wrote {full_path(target)} with the judge's verdicts, for the record.\n"
          f"Fill in human_label with PASS or FAIL for every row of labels-blind.csv, then run:\n"
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
    blind, full = blind_path(target), full_path(target)
    for path in (blind, full):
        if not path.exists():
            print(f"{path} is missing. Run --export first.")
            return 2

    human_rows = read_csv(blind)
    judge_by_id = {r["id"]: r for r in read_csv(full)}
    stray = [r["id"] for r in human_rows if r["id"] not in judge_by_id]
    if stray:
        print(f'these ids in {blind.name} are not in {full.name}: {", ".join(stray)}.\n'
              f"The two files are from different exports; re-export and label again.")
        return 2

    pairs, unlabeled, bad, merged = [], [], [], []
    for r in human_rows:
        human = (r.get("human_label") or "").strip().upper()
        judged = judge_by_id[r["id"]]
        row = {**judged, "human_label": human}
        merged.append(row)
        if not human:
            unlabeled.append(r["id"])
        elif human not in ("PASS", "FAIL"):
            bad.append(f'{r["id"]}={r["human_label"]!r}')
        else:
            pairs.append((judged["judge_verdict"].strip().upper(), human))

    if bad:
        print(f'human_label must be PASS or FAIL. Bad values: {", ".join(bad)}')
        return 2
    if not pairs:
        print(f"no rows in {blind} have a human_label yet.")
        return 2

    # Fold the labels into labels.csv so the repo keeps judge and human side by side.
    write_csv(full, FULL_COLUMNS + ["human_label"], sorted(merged, key=lambda r: r["id"]))

    observed, k, cells = kappa(pairs)
    disagreements = [r for r in merged
                     if r["human_label"] in ("PASS", "FAIL")
                     and r["judge_verdict"].strip().upper() != r["human_label"]]

    latest = json.loads((adapter.results_dir(target) / "latest.json").read_text(encoding="utf-8"))
    md = [
        f"# Judge validation: {target}",
        "",
        f'Judge model `{latest["models"]["judge"]}`, temperature 0, prompt in `harness/judge.py`.',
        f'Labelled against the run of {latest["finished_at"]} (agent code `{latest["agent_code_hash"]}`).',
        "",
        "Labels were collected blind: the human labelled `labels-blind.csv`, which omits the "
        "judge's verdict and reason and is shuffled so the judge's failures are not grouped. "
        "The two sides are joined by case id afterwards.",
        "",
        "| | value |",
        "|---|---|",
        f"| labelled cases | {len(pairs)} of {len(merged)} exported |",
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
    md += ["", "## Where the judge and the human disagree", ""]
    if disagreements:
        for r in disagreements:
            md += [f'- `{r["id"]}` judge **{r["judge_verdict"]}**, human **{r["human_label"]}**',
                   f'  - question: {r["question"][:120]}',
                   f'  - answer: {r["answer"][:200]}',
                   f'  - judge reason: {r["judge_reason"][:200]}']
    else:
        md += ["None: the judge and the human agreed on every labelled case."]
    md += ["", "Regenerate with `python harness/validate_judge.py --target "
           f'{target} --compute` after editing `labels-blind.csv`.', ""]

    out = adapter.target_dir(target) / "judge_validation.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"agreement {observed:.1%}, Cohen's kappa {k:.3f} over {len(pairs)} labelled cases")
    print(f"{len(disagreements)} disagreement(s)")
    print(f"wrote {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True)
    ap.add_argument("--export", type=int, metavar="N", help="export N cases for labelling")
    ap.add_argument("--force", action="store_true", help="with --export: overwrite labels already filled in")
    ap.add_argument("--compute", action="store_true", help="join the labels, write judge_validation.md")
    args = ap.parse_args()
    if args.export:
        return export(args.target, args.export, args.force)
    if args.compute:
        return compute(args.target)
    ap.error("pass --export N or --compute")


if __name__ == "__main__":
    raise SystemExit(main())
