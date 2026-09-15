"""JDAnalysis parsing -- the shape every JD source has to produce."""

import json

import pytest
from pydantic import ValidationError

from jobstitch.jd_validator import JDAnalysis, JDValidator

VALID = {
    "match_percentage": 82,
    "match_rationale": "Strong overlap.",
    "job_title": "Staff Platform Engineer",
    "work_location": "Lisbon, Portugal",
    "work_mode": "hybrid",
    "expected_salary": "EUR 70k-90k",
    "max_salary": 90000,
    "experience_level": "professional",
    "hard_skills": ["Kubernetes", "Go"],
    "soft_skills": ["Communication"],
    "company_name": "Globex",
    "posting_type": "direct",
    "posting_url": "https://example.com/job",
    "gaps": "No formal people management.",
    "pers_preferences": "Hybrid is acceptable.",
    "pers_preference_score": 1.5,
}


def test_parses_plain_json():
    assert JDValidator._parse_jd_analysis(json.dumps(VALID)).company_name == "Globex"


def test_strips_markdown_fences():
    fenced = "```json\n" + json.dumps(VALID) + "\n```"
    assert JDValidator._parse_jd_analysis(fenced).match_percentage == 82


def test_unwraps_a_nested_payload():
    """Some fast models wrap the whole answer in one string field."""
    wrapped = json.dumps({"description": json.dumps(VALID)})
    assert JDValidator._parse_jd_analysis(wrapped).job_title == "Staff Platform Engineer"


def test_rejects_an_invalid_experience_level():
    bad = dict(VALID, experience_level="wizard")
    with pytest.raises(ValidationError):
        JDValidator._parse_jd_analysis(json.dumps(bad))


def test_optional_fields_may_be_absent():
    minimal = {k: v for k, v in VALID.items()
               if k not in ("match_rationale", "work_location", "expected_salary",
                            "max_salary", "posting_url")}
    analysis = JDAnalysis.model_validate(minimal)
    assert analysis.posting_url is None


def test_clamp_list_caps_and_coerces():
    assert JDValidator._clamp_list([1, 2, 3, 4, 5, 6], 4) == ["1", "2", "3", "4"]
    assert JDValidator._clamp_list(None, 4) == []
