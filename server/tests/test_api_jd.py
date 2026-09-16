"""POST /v1/jd/detect and /v1/jd/analysis."""

from __future__ import annotations

import json

from jobstitch_contracts import JDAnalysis, static_jd_guess

ANALYSIS = {
    "match_percentage": 82, "match_rationale": "Strong overlap.",
    "job_title": "Head of IT", "work_location": "Milan, Italy", "work_mode": "hybrid",
    "expected_salary": "EUR 60k-80k", "max_salary": 80000, "experience_level": "manager",
    "hard_skills": ["AWS", "Kubernetes", "ITIL", "Budgets", "Extra one"],
    "soft_skills": ["Communication"], "company_name": "Acme Corp", "posting_type": "direct",
    "posting_url": None, "gaps": "No SAP.", "pers_preferences": "Hybrid is fine.",
    "pers_preference_score": 1.5,
}


def jd_parts(candidate, **extra):
    return {
        "candidate_profile": ("p.json", json.dumps(dict(candidate.profile)), "application/json"),
        "pers_preferences": ("prefs.md", candidate.preferences, "text/markdown"),
        **extra,
    }


def test_detect_rejects_short_text_without_calling_the_model(client, fake_models):
    body = client.post("/v1/jd/detect", data={"jd_text": "too short"}).json()

    assert body["data"] == {"is_job_description": False}
    assert fake_models["summary"].calls == [], "the free check must come first"


def test_detect_asks_the_model_when_the_text_is_plausible(client, fake_models):
    fake_models["summary"].replies = ["YES"]
    text = "Job posting. " * 120
    assert static_jd_guess(text)

    body = client.post("/v1/jd/detect", data={"jd_text": text}).json()

    assert body["data"]["is_job_description"] is True
    assert len(fake_models["summary"].calls) == 1


def test_detect_takes_the_model_at_its_word_when_it_says_no(client, fake_models):
    fake_models["summary"].replies = ["NO, this is a privacy policy"]
    body = client.post("/v1/jd/detect", data={"jd_text": "Job posting. " * 120}).json()
    assert body["data"]["is_job_description"] is False


def test_analysis_returns_a_validated_analysis(client, fake_models, candidate):
    fake_models["summary"].replies = [json.dumps(ANALYSIS)]

    body = client.post("/v1/jd/analysis", data={"jd_text": "a jd"},
                       files=jd_parts(candidate)).json()

    analysis = JDAnalysis.model_validate(body["data"])
    assert analysis.company_name == "Acme Corp"
    assert len(analysis.hard_skills) == 4, "the list is clamped to four"


def test_analysis_prompt_carries_the_profile_and_the_preferences(client, fake_models, candidate):
    fake_models["summary"].replies = [json.dumps(ANALYSIS)]
    client.post("/v1/jd/analysis", data={"jd_text": "SENTINEL"}, files=jd_parts(candidate))

    prompt = fake_models["summary"].last_prompt
    assert "SENTINEL" in prompt
    assert candidate.profile["name"] in prompt
    assert candidate.preferences[:40] in prompt


def test_analysis_requires_the_profile(client, candidate):
    response = client.post(
        "/v1/jd/analysis", data={"jd_text": "a jd"},
        files={"pers_preferences": ("prefs.md", candidate.preferences, "text/markdown")},
    )
    assert response.status_code == 400
    assert "candidate_profile" in response.json()["error"]["message"]


def test_analysis_requires_the_preferences(client, candidate):
    response = client.post(
        "/v1/jd/analysis", data={"jd_text": "a jd"},
        files={"candidate_profile": ("p.json", json.dumps(dict(candidate.profile)))},
    )
    assert response.status_code == 400
    assert "pers_preferences" in response.json()["error"]["message"]


def test_a_bad_analysis_is_retried_then_reported(client, fake_models, candidate):
    fake_models["summary"].replies = ["{}"]
    response = client.post("/v1/jd/analysis", data={"jd_text": "a jd"},
                           files=jd_parts(candidate))
    body = response.json()
    assert response.status_code == 502
    assert body["error"]["stage"] == "jd.analysis"
    assert len(fake_models["summary"].calls) == 2, "it retried once before giving up"
