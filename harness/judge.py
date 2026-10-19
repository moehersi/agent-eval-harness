"""The LLM judge: grades an agent's answer text against the gold result.

Three criteria, all of which must hold for a PASS:
  1. correct facts   - the answer states what the gold result actually says
  2. no invented numbers - every number and name in the answer is in the gold result
  3. same language   - the answer is in the language the question was asked in

The prompt lives here and nowhere else. It never sees an agent's prompt, and the
agent never sees this one: a judge that shares wording with the thing it grades
rewards the agent for sounding like itself.
"""
from __future__ import annotations

import json
import re

from harness import llm

PROMPT = """You grade one answer produced by a database question-answering agent.

QUESTION (as the user asked it):
{question}

GOLD RESULT (the correct result rows from the database, the ground truth):
{gold}

AGENT ANSWER (what you must grade):
{answer}

Grade the agent answer on all three criteria:
1. CORRECT FACTS - what the answer claims matches the gold result. Wording, rounding
   to the same value, and extra context are fine. A different number, a different
   name, or a claim the gold result does not support is not fine.
2. NO INVENTED NUMBERS - every number and every name in the answer appears in the
   gold result (or in the question). An answer that adds a figure from nowhere fails,
   even if the rest is right.
3. SAME LANGUAGE - the answer is in the same language the question was asked in.
   An Arabic question needs an Arabic answer; an English question an English answer.
   Column names, category codes and place names kept in their original form are fine.

{decline_note}
Verdict PASS only if all three hold. Otherwise FAIL.

Reply with one JSON object and nothing else:
{{"verdict": "PASS" or "FAIL", "reason": "<one short sentence>"}}"""

DECLINE_NOTE = """This question CANNOT be answered from the database ({decline_reason}).
The only correct answer is a plain refusal in the question's language. An answer that
reports a value, explains the schema, or answers a nearby question instead is a FAIL.

"""


def build_prompt(case: dict, answer: str, gold_rows: list | None) -> str:
    decline = case.get("kind") == "decline"
    return PROMPT.format(
        question=case["question"],
        gold=("not answerable from this database" if decline else json.dumps(gold_rows, ensure_ascii=False, default=str)),
        answer=answer or "(the agent produced no answer)",
        decline_note=DECLINE_NOTE.format(decline_reason=case.get("decline_reason", "")) if decline else "",
    )


def parse(text: str) -> dict:
    """Pull the verdict out of the model's reply. An unparseable reply is a FAIL."""
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).strip().upper()
            if verdict in ("PASS", "FAIL"):
                return {"verdict": verdict, "reason": str(data.get("reason", ""))[:300]}
        except json.JSONDecodeError:
            pass
    return {"verdict": "FAIL", "reason": f"judge reply was not valid JSON: {text[:120]!r}"}


def grade(case: dict, answer: str, gold_rows: list | None, client=None) -> dict:
    """-> {verdict, reason, tokens_in, tokens_out}"""
    client = client or llm.client()
    llm.throttle()
    message = client.invoke(build_prompt(case, answer, gold_rows))
    tokens_in, tokens_out = llm.usage(message)
    return {**parse(llm.text(message.content)), "tokens_in": tokens_in, "tokens_out": tokens_out}
