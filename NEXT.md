# Where this stands, and what to do next

Written 2026-10-20. Everything below is verified state, not plans.

## Done and pushed

| repo | branch | state |
|---|---|---|
| `moehersi/agent-eval-harness` (private) | `master` | 4 commits, pushed, up to date with origin |
| `moehersi/riyadh-sql-agent` (public) | `master` | answer node merged (`b12e825`), pushed |
| `moehersi/riyadh-sql-agent` | `weak-prompt` | `61e6447`, pushed, **never merge** - the regression demo depends on it staying broken |

The agent's `master` hashes to `c7cfa8efe3e0`, which is exactly what
`targets/riyadh_sql/baseline.json` expects, so CI's default `agent_ref: master`
and the committed baseline describe the same agent.

Steps 1-8 and 10 of the build are complete. Tests: 33 in this repo, 8 in the
agent repo, both suites green with no API key (`BACKEND=ollama`).

## The one thing left: finish the red regression demo

The weakened-prompt run reached **19 of 49 cases** before the Gemini free tier's
daily cap (500 requests/day) ran out. On those 19 cases the baseline scores 19/19
on both gates and the weakened prompt scores 3/19 - but `check_regression.py` has
never been run against a *complete* weak run, so the gate has not actually been
watched going red. That is the gap.

Quota resets ~09:00 local on 2026-10-20 (the 429 said "retry in 8h58m" at
~00:00). One full run costs **147 requests**, so the 500/day cap allows about
three runs: a red run and a green run fit, with one spare.

### Run it

```bash
# 1. point the harness at the weakened agent
cd C:\Users\MAXNET\Desktop\riyadh-sql-agent
git checkout weak-prompt

# 2. run it - 19 of 49 cases are already cached, so this costs ~90 requests
cd C:\Users\MAXNET\Desktop\agent-eval-harness
.venv\Scripts\python.exe harness/run.py --target riyadh_sql

# 3. the gate should exit 1 and name the gates that fired
.venv\Scripts\python.exe harness/check_regression.py --target riyadh_sql
echo exit=%ERRORLEVEL%

# 4. ALWAYS put the agent back - never leave the checkout weakened
cd C:\Users\MAXNET\Desktop\riyadh-sql-agent
git checkout master
```

Expect: accuracy and judge pass rate both far below the 5 pp gate, and a long
list of cases that passed in the baseline and now fail twice (each one is
re-run once before it counts).

### Then paste the real numbers into

- `README.md`, section **Proving the gate actually fires** - replace the "19 of 49"
  partial table with the full result and the gate output.
- `WALKTHROUGH.md`, limitation 3 - it currently says the demo is only partly
  measured. Delete that limitation once it is fully measured.

Do not round the partial numbers up into a claim. The README currently states
exactly what was and was not measured; keep it that way.

### Capturing a red CI run for the README

Needs the `GOOGLE_API_KEY` secret set first:
`https://github.com/moehersi/agent-eval-harness/settings/secrets/actions` ->
New repository secret -> name `GOOGLE_API_KEY` -> paste the key from
`https://aistudio.google.com/app/apikey`.

```bash
gh workflow run eval.yml --repo moehersi/agent-eval-harness -f agent_ref=weak-prompt  # red
gh workflow run eval.yml --repo moehersi/agent-eval-harness -f agent_ref=master       # green
gh run list --repo moehersi/agent-eval-harness --limit 5
```

Each CI run spends another 147 requests, so a red/green pair plus a local red run
is 441 of the 500 daily requests. Probably split across two days.

Note: GitHub reported 0 registered workflows right after the first push - Actions
indexing lag on a new repo. If the Actions tab is empty, `gh workflow run
eval.yml` still works by filename.

## Open decisions, not bugs

1. **The judge is stricter than the human rubric on one case.** `d03`: the agent
   said "the phone number for customer CUST001 is not available"; the human
   counted that as a refusal, the judge wanted a plain refusal with no
   explanation. That is the single disagreement behind the 96.7% / kappa 0.889 in
   `targets/riyadh_sql/judge_validation.md`. Loosening `DECLINE_NOTE` in
   `harness/judge.py` would close it, but it invalidates the committed verdicts and
   needs a re-run plus re-labelling. Left alone deliberately.
2. **The suite will saturate.** 89.8% today leaves headroom. When a model scores
   49/49 the suite stops detecting anything and needs harder cases.
3. **Per-case gate versus non-determinism.** Two identical runs flipped 2 of 49
   cases, which is why a newly failing case is re-run once before it counts. If
   flips still get through, the next lever is requiring two consecutive failures
   across runs rather than within one.

## Things that will bite if forgotten

- `use_lessons=False` in the adapter is deliberate: Reflexion writes
  `lessons.json`, which would make every run depend on every run before it.
- Latency excludes the harness's own rate-limit waiting *and* failed attempts
  (`overhead_s`). Wall-clock around the call would be measuring Google.
- Every request carries a 90s timeout. The Gemini client's default is no timeout,
  and one stalled request hung a whole run.
- `set -o pipefail` in the workflow is load-bearing. Without it the
  `check_regression | tee` pipeline returns tee's exit code and the gate can
  never fail.
- Cache is keyed by a hash of the agent's source, so changing the agent
  invalidates it automatically. `.cache/riyadh_sql/` currently holds both
  `c7cfa8efe3e0` (baseline, 49 cases) and `21f15229b37e` (weak, 19 cases).
- Never commit `.env`, `riyadh.db`, or `lessons.json`. All three are ignored in
  both repos; verified absent from both pushed histories.
