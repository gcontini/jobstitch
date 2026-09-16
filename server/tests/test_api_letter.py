"""POST /v1/letter."""

from __future__ import annotations

import json

LETTER = " ".join(["word"] * 200)


def letter_parts(candidate, **extra):
    return {
        "candidate_profile": ("p.json", json.dumps(dict(candidate.profile)), "application/json"),
        **extra,
    }


def test_returns_the_letter_and_its_word_count(client, fake_models, candidate):
    fake_models["summary"].replies = [LETTER]
    body = client.post("/v1/letter", data={"jd_text": "a jd"},
                       files=letter_parts(candidate)).json()
    assert body["data"]["text"] == LETTER
    assert body["data"]["words"] == 200


def test_a_short_letter_is_rejected_and_retried(client, fake_models, candidate):
    fake_models["summary"].replies = ["too short", LETTER]
    body = client.post("/v1/letter", data={"jd_text": "a jd"},
                       files=letter_parts(candidate)).json()
    assert body["data"]["words"] == 200
    retry = fake_models["summary"].calls[1]["messages"][-1]["content"]
    assert "not acceptable as-is" in retry


def test_the_analysis_is_optional(client, fake_models, candidate):
    fake_models["summary"].replies = [LETTER]
    analysis = {"company_name": "Acme Corp", "posting_type": "direct", "job_title": "Head of IT"}
    files = letter_parts(candidate,
                         analysis=("analysis.json", json.dumps(analysis), "application/json"))
    client.post("/v1/letter", data={"jd_text": "a jd"}, files=files)
    assert "Acme Corp" in fake_models["summary"].last_prompt


def test_an_overridden_prompt_is_used(client, fake_models, candidate):
    fake_models["summary"].replies = [LETTER]
    files = letter_parts(candidate, sys_prompt_letter=("p.txt", "WRITE IT SHORT"))
    client.post("/v1/letter", data={"jd_text": "a jd"}, files=files)
    assert fake_models["summary"].calls[0]["messages"][0]["content"] == "WRITE IT SHORT"


def test_a_letter_that_never_validates_is_a_502(client, fake_models, candidate):
    fake_models["summary"].replies = ["too short"]
    response = client.post("/v1/letter", data={"jd_text": "a jd"}, files=letter_parts(candidate))
    assert response.status_code == 502
    assert response.json()["error"]["stage"] == "letter.generate"
