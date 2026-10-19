"""Judge plumbing: verdict parsing, prompt assembly, kappa. No LLM calls."""
import pytest

from harness import judge
from harness.validate_judge import kappa

CASE = {"id": "x", "kind": "query", "lang": "en", "question": "How many customers are there?"}
DECLINE = {"id": "d", "kind": "decline", "lang": "en", "question": "What is their phone number?",
           "decline_reason": "phone numbers are not in the database"}


@pytest.mark.parametrize("reply, expected", [
    ('{"verdict": "PASS", "reason": "matches"}', "PASS"),
    ('{"verdict": "FAIL", "reason": "wrong number"}', "FAIL"),
    ('```json\n{"verdict":"pass","reason":"ok"}\n```', "PASS"),
    ('Sure! {"verdict": "FAIL", "reason": "no"} hope that helps', "FAIL"),
    ("I think it is fine", "FAIL"),          # unparseable is a fail, never a silent pass
    ('{"verdict": "MAYBE", "reason": "?"}', "FAIL"),
    ("", "FAIL"),
])
def test_parse(reply, expected):
    assert judge.parse(reply)["verdict"] == expected


def test_unparseable_reply_says_so():
    assert "not valid JSON" in judge.parse("nonsense")["reason"]


def test_prompt_carries_question_gold_and_answer():
    prompt = judge.build_prompt(CASE, "There are 300 customers.", [[300]])
    assert CASE["question"] in prompt
    assert "300" in prompt
    assert "There are 300 customers." in prompt
    assert "SAME LANGUAGE" in prompt


def test_decline_prompt_demands_a_refusal():
    prompt = judge.build_prompt(DECLINE, "I can't answer that from this data.", None)
    assert "CANNOT be answered" in prompt
    assert DECLINE["decline_reason"] in prompt


def test_missing_answer_is_still_gradeable():
    assert "no answer" in judge.build_prompt(CASE, "", [[300]])


def test_kappa_perfect_and_chance():
    perfect = [("PASS", "PASS")] * 6 + [("FAIL", "FAIL")] * 4
    observed, k, _ = kappa(perfect)
    assert observed == 1.0 and k == pytest.approx(1.0)

    # Judge always says PASS, human splits evenly: 50% raw agreement, no skill.
    chance = [("PASS", "PASS")] * 5 + [("PASS", "FAIL")] * 5
    observed, k, _ = kappa(chance)
    assert observed == 0.5 and k == pytest.approx(0.0)


def test_kappa_counts_every_cell():
    _, _, cells = kappa([("PASS", "PASS"), ("PASS", "FAIL"), ("FAIL", "PASS"), ("FAIL", "FAIL")])
    assert all(cells[c] == 1 for c in cells)
