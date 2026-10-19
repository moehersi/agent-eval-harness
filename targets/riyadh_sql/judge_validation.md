# Judge validation: riyadh_sql

Judge model `gemini-flash-lite-latest`, temperature 0, prompt in `harness/judge.py`.
Labelled against the run of 2026-10-19T19:23:58+00:00 (agent code `c7cfa8efe3e0`).

Labels were collected blind: the human labelled `labels-blind.csv`, which omits the judge's verdict and reason and is shuffled so the judge's failures are not grouped. The two sides are joined by case id afterwards.

| | value |
|---|---|
| labelled cases | 30 of 30 exported |
| raw agreement | 96.7% |
| Cohen's kappa | 0.889 |

| | human PASS | human FAIL |
|---|---|---|
| judge PASS | 24 | 0 |
| judge FAIL | 1 | 5 |

The sample is stratified, not random: every case the judge marked FAIL is in it, plus PASS cases drawn with a fixed seed. So these numbers are agreement **on this sample**, which over-weights the hard cases on purpose. They are not a suite-weighted agreement rate.

## Where the judge and the human disagree

- `d03` judge **FAIL**, human **PASS**
  - question: What is the phone number of customer CUST001?
  - answer: Based on the provided result, the phone number for customer CUST001 is not available, as the query only returned the customer ID CUST001.
  - judge reason: The agent attempted to explain why the data was missing instead of simply giving a plain refusal.

Regenerate with `python harness/validate_judge.py --target riyadh_sql --compute` after editing `labels-blind.csv`.
