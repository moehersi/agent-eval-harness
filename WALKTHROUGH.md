# What we built, explained simply

This is the plain-language companion to the README. The README tells you how to
use the harness. This file tells you *why each piece exists* and *what I learned
building it*, so the next one you build you can design yourself.

---

## 1. The problem in one paragraph

You had a text-to-SQL agent that scored 20 out of 20 on its own test set. That
sounds like success, but it is actually a blind spot: a test that always passes
tells you nothing. If you change a prompt, swap a model, or upgrade a library
and the agent gets worse, a 20/20 test still says 20/20 until the damage is bad
enough to break something — and by then you cannot tell *which* change did it.

So the job was not "test the agent better". It was **build an instrument that can
notice the agent getting worse**. That instrument has to be separate from the
agent, because you want to point it at the invoice extractor and the WhatsApp
agent next.

---

## 2. The five ideas the harness is made of

### Idea 1: An adapter, so the harness does not care what the agent is

The harness never imports your agent directly. Instead each agent gets a small
translator file — an **adapter** — that speaks the harness's language:

```
run(case)  ->  {answer, raw, latency_s, tokens_in, tokens_out}
score(case, output)  ->  True / False
```

That is the entire contract. The SQL agent's adapter calls `agent.ask(...)`; a
future invoice adapter will call `extract(...)`. The harness only ever sees the
dictionary that comes back.

**Why this matters:** without it, adding the second agent means editing the
harness. With it, adding the second agent means adding a folder. That is the
difference between a tool and a one-off script.

**The thing I deliberately did *not* build:** a plugin system, a config file
format, or a base class to inherit from. A folder under `targets/` with a
`suite.jsonl` in it *is* the registration. `--target riyadh_sql` is the folder
name. Every abstraction you don't write is one you never have to maintain.

### Idea 2: Two graders, because they catch different bugs

**The deterministic check** runs your gold SQL, runs the agent's SQL, and
compares the rows. Fast, free, no opinions. It catches "the agent wrote the wrong
query".

**The LLM judge** reads the agent's *sentence* and grades it against the gold
result on three things: are the facts right, did it invent any numbers, is it in
the same language as the question.

Why both? Case `h08` from the real run is the perfect illustration. The question
asked for order counts *for each month of 2025*. The agent produced all twelve
correct rows — so the deterministic check **passed** — and then wrote: "monthly
orders ranged from 6 in October to 14 in January". Correct data, wrong answer.
Only the judge caught it.

And case `d04`, the best one in the suite: asked in Arabic for a 5-star customer
delivery rating, which does not exist in the database, the agent ran
`SELECT avg(actual_delivery_min)` and answered "the average customer rating is
34.32". It reported **delivery time in minutes as a star rating**. That is a
hallucination with a real number attached, which is the kind that fools people.

### Idea 3: A baseline, so "worse" has a meaning

A number on its own is not a test. `89.8%` is not pass or fail — it is just a
number. So the harness writes every run to `results/riyadh_sql/latest.json`, and
compares it to a **committed** file, `targets/riyadh_sql/baseline.json`, holding
the last results you accepted.

If the new run is meaningfully worse, `check_regression.py` **exits with code 1**,
which is how a command tells CI "fail the build".

"Meaningfully worse" is four specific rules:

| rule | why that number |
|---|---|
| accuracy dropped > 5 percentage points | below that is run-to-run noise |
| judge pass rate dropped > 5 points | same reason |
| p95 latency rose > 30% | a slower agent is a regression too |
| a case that used to pass now fails | catches a *swap* |

That last one is subtle and the most useful. Imagine a change breaks two cases
and fixes two others. Accuracy is unchanged — 89.8% before, 89.8% after — and
every average looks fine. The per-case check still notices, because it tracks
each case by name.

### Idea 4: Measure the judge too

An LLM grading an LLM sounds like a circular argument, and it would be if nobody
checked the grader. So: I exported 30 cases, **you** labelled them PASS or FAIL
by hand, and the harness compared your labels to the judge's.

Two numbers come out:

- **Raw agreement — 96.7%.** You and the judge gave the same verdict on 29 of 30.
- **Cohen's kappa — 0.889.** Agreement *after subtracting the agreement you would
  expect from luck alone.*

Kappa is worth understanding. Suppose the judge said PASS to everything, and 90%
of answers really are passes. Raw agreement would be 90% — which sounds great,
from a grader that is not grading. Kappa fixes that by asking: how much better
than guessing is this? 0 means "no better than chance", 1 means "perfect".
**0.889 means the judge is genuinely tracking your judgement**, not riding the
base rate.

**The detail that makes this measurement honest:** your labelling file was
**blind**. `labels-blind.csv` had no judge verdict in it, and the rows were
shuffled so the judge's six failures were not sitting in a block at the top. If
you can see the judge's answer while labelling, you unconsciously agree with it,
and the 96.7% would be measuring *your anchoring*, not the judge's accuracy. You
asked for this specifically, and you were right to.

### Idea 5: Run it automatically, or it will not get run

`.github/workflows/eval.yml` runs all of this on every pull request. It checks
out two repos — the harness and the agent — builds the database, runs both test
suites, then the eval, then the regression check, and writes the results table
into the GitHub job summary so you see it without digging through logs.

If the `GOOGLE_API_KEY` secret is missing it **skips the LLM steps with an
explanation** instead of failing, because a missing key is a setup problem, not a
code problem, and a red X that means "you forgot a secret" trains people to
ignore red Xs.

---

## 3. The five real bugs I hit, and what they teach

These are the interesting part. Each one is a mistake that would have quietly
produced wrong numbers.

### Bug 1: The latency metric was measuring Google, not the agent

**Symptom:** one question was recorded at **53.8 seconds**. By hand it takes 1.4s.

**Cause:** Gemini's free tier allows ~15 requests per minute. Go over, it returns
error 429. The retry library I was using, `with_retry`, catches that and sleeps
before trying again — *inside* the function call I was timing. So my stopwatch
was measuring Google's rate limiter and labelling it "agent latency".

**Why it mattered:** one of the four regression rules is "p95 latency rose more
than 30%". With garbage latency numbers, that rule would fire at random. A gate
that cries wolf gets switched off, and then it is not a gate.

**Fix, in two parts.** First, a *throttle*: the harness now spaces its own
requests out (10/min) so it does not provoke the 429s in the first place. Second,
*attempt-level timing*: the adapter does its own retrying so it can time each
attempt separately, and every second not spent on the attempt that actually
succeeded is subtracted. Result: p95 went from 22.4s to **4.16s**, with zero
retries in the baseline run.

**The lesson:** when you measure something, ask what *else* is inside your
measurement. Wall-clock time around a network call includes the network, the
queue, and the retry policy. Those are not the thing you meant to measure.

### Bug 2: The CI gate could never fail

I wrote this in the workflow:

```bash
python harness/check_regression.py --target "$target" | tee -a "$GITHUB_STEP_SUMMARY" || status=1
```

Looks reasonable: run the check, copy the output to the summary, and if it fails,
set `status=1`.

**It can never fail.** In a shell pipeline `a | b`, the exit code is **`b`'s**.
`tee` always succeeds, so the pipeline always succeeded, so `|| status=1` never
ran. The entire regression gate was decorative. The fix is one line —
`set -o pipefail` — which makes the pipeline report the first failure instead.

**The lesson:** a safety mechanism you never watched fail is not known to work.
Which is exactly why step 9 exists: I broke the agent on purpose to prove the
gate goes red.

### Bug 3: Four of my test questions were untestable

I wrote 25 new questions with gold SQL, then ran every one against the database
before trusting it. Four were broken:

- "districts with more than 60 customers" → **no rows**: every district has
  exactly 60.
- "customers with 4 or more orders" → **no rows**: the busiest customer has 3.
- "highest-value order per category" → **tied** in five of six categories, so
  there is no single right answer.
- "top-spending customer per district" → **tied** in Al Malaz.

A question with no answer, or with two equally-correct answers, does not test the
agent — it just fails it at random. All four were rewritten and re-verified.

**The lesson:** validate your test data before you trust the test. A test suite
is code, and unverified gold answers are bugs that look like agent failures.

### Bug 4: A hung request has no timeout, and waits forever

**Symptom:** a run stopped dead at case 12 and sat there. No error, no log line,
nothing to look at.

**Cause:** the Gemini client's default is `timeout=None` — *no* timeout. My retry
loop can only retry something that fails, and a request that never returns never
fails. So it waited.

**Why it mattered:** in CI that is a job holding a runner for its full six-hour
limit and then dying with an empty log. The worst kind of failure: expensive and
uninformative.

**Fix:** every request now carries a 90-second timeout. The judge's client sets it
at construction; the agent builds its own client in its own repo, so the adapter
sets the field on it when it wraps it. Also `max_retries=0` on the client, because
retrying is the harness's job and two retry layers stacked on top of each other
turn one slow call into minutes of invisible waiting.

**The lesson:** every network call needs a timeout, and "the library probably has
a sensible default" is not a timeout. Also: I first diagnosed this stall wrong. I
had been watching for new cache files to appear, and concluded a *second* run was
hung when it was merely working through cached cases that write no files. Watching
the wrong signal produces a confident wrong diagnosis — I checked the API directly
(it answered in 1.7s) before I believed anything.

### Bug 5: Temperature 0 is not determinism

I ran the exact same agent, the exact same suite, twice in a row. **Two of 49
cases gave different results.** Temperature 0 makes a model *more*
deterministic, not deterministic — floating-point and batching on the provider's
side still move things around.

This is a real problem for the "a case that used to pass now fails" rule: it will
sometimes fire on noise rather than a regression. So that rule now **re-runs the
failing case once**, and only counts it if it fails twice. The re-run is
deliberately not cached (a cached re-run replays the same answer and proves
nothing), and it *fails closed* — if the agent cannot be reached, the failure
stands rather than being quietly excused.

**The lesson:** measure your instrument's noise floor before you set a threshold.
I only knew 5 percentage points was a sane threshold because I knew two identical
runs differ by about one case in 25.

---

## 4. Staying inside the free tier

Three mechanisms, each doing a different job:

- **Throttle** — space requests out so you never hit the limit. Prevention.
- **Retry with backoff** — if you hit it anyway, wait and try again. Recovery.
- **Cache** — never pay for the same answer twice. Avoidance.
- **Timeout** — give up on a stalled request instead of waiting forever.

The numbers matter here. The free tier allows **15 requests a minute and 500 a
day**. One full 49-case run costs **147 requests** (51 attempts at writing SQL, 47
answer-node calls, 49 judge calls), so you get about **three full runs a day**.
I found this out the way you always find it out: by running out mid-demo, nine
hours from the reset.

The cache is the interesting one. Agent outputs are stored under
`.cache/<target>/<hash of the agent's source code>/`. That hash in the path is
the whole trick: **change the agent and every cache entry is automatically a
miss**, because the path changed. No cache-invalidation logic to get wrong. You
can see it worked — after I weakened the agent's prompt, a second folder appeared
next to the first, and the harness re-ran everything.

---

## 5. What I would tell you to watch

Three honest limitations, so you are not surprised later:

1. **The judge is one model's opinion, validated on 30 cases.** 96.7% agreement
   is good, and 30 labels is a small sample. If you change the judge prompt or
   its model, the old agreement number no longer applies — re-label.
2. **The agreement number over-weights hard cases.** The 30 labelled cases
   include all six the judge failed, on purpose, because a random sample would
   have had too few failures to compute a meaningful kappa. It is agreement on
   *that* sample, not on the suite. The file says so next to the number.
3. **The regression demo is only partly measured.** The weakened-prompt branch
   exists and the harness was run against it, but the daily quota ran out at case
   19 of 49. On those 19 cases the weakened agent scored 3/19 where the baseline
   scored 19/19 — the gate would fire many times over — but the full red run and
   the red CI screenshot are still outstanding. The README says so rather than
   rounding it up to "demonstrated".
4. **The suite will saturate eventually.** Right now the agent scores 89.8% and
   there is headroom. When a better model scores 49/49, the suite stops being
   able to detect anything and needs harder cases — the same problem the original
   20 questions had. Saturation is not a victory, it is a maintenance task.

---

## 6. The vocabulary, in one table

| term | plain meaning |
|---|---|
| **harness** | the test rig that runs an agent and scores it |
| **adapter** | the small translator file that teaches the harness to talk to one agent |
| **target** | one agent plugged into the harness (a folder under `targets/`) |
| **suite** | the list of test cases, one JSON object per line |
| **gold / ground truth** | the known-correct answer a case is graded against |
| **deterministic check** | grading by exact comparison — no model involved |
| **LLM-as-judge** | using a model to grade another model's answer |
| **baseline** | the last accepted set of results, committed to git as a tripwire |
| **regression** | getting worse than the baseline by more than the allowed margin |
| **percentage point (pp)** | the gap between two percentages — 89.8% to 84.8% is 5 pp |
| **p50 / p95 latency** | the middle case, and the slow-but-not-freak case (95 of 100 are faster) |
| **Cohen's kappa** | agreement after subtracting the agreement luck would give you |
| **exit code** | the number a command returns; 0 means success, anything else fails CI |
| **throttle** | deliberately slowing down to stay under a rate limit |
| **backoff** | waiting longer after each failed retry |
| **fail closed** | when a check cannot run, treat it as a failure rather than a pass |

---

## 7. If you want to add the invoice extractor tomorrow

1. `mkdir targets/invoice_extractor`
2. Write `adapter.py` with `run()` and `score()` — about 20 lines, example in the
   README.
3. Write `suite.jsonl`, one case per line, and **verify every gold answer before
   trusting it** (bug 3 above).
4. `python harness/run.py --target invoice_extractor` and read the output.
5. Happy with it? `python harness/check_regression.py --target invoice_extractor
   --update-baseline`, then commit the baseline.
6. Add one `actions/checkout` step to the workflow for that agent's repo. The
   loop over `targets/*/` picks the rest up on its own.

No harness file needs to change. That was the point.
