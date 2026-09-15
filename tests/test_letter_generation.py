"""Cover letter validation and the web-research decision."""

import pytest

from jobstitch.letter_generation import MAX_WORDS, MIN_WORDS, LetterGenerator


def letter(words):
    return " ".join(["word"] * words)


def test_accepts_a_letter_of_plausible_length():
    LetterGenerator._validate_letter(letter(MIN_WORDS + 10))


@pytest.mark.parametrize("words", [MIN_WORDS - 1, MAX_WORDS + 1])
def test_rejects_letters_outside_the_word_range(words):
    with pytest.raises(ValueError, match="words"):
        LetterGenerator._validate_letter(letter(words))


@pytest.mark.parametrize("placeholder", ["[Company Name]", "<<role>>"])
def test_rejects_unfilled_placeholders(placeholder):
    text = letter(MIN_WORDS) + " " + placeholder
    with pytest.raises(ValueError, match="placeholder"):
        LetterGenerator._validate_letter(text)


def test_rejects_non_ascii():
    with pytest.raises(ValueError, match="non-ASCII"):
        LetterGenerator._validate_letter(letter(MIN_WORDS) + " — dash")


def test_strips_code_fences_the_model_adds_anyway():
    class Msg:
        content = "```\nDear Hiring Manager,\n```"

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    assert LetterGenerator._extract_letter(Resp()) == "Dear Hiring Manager,"


class FakeSelector:
    def __init__(self, supports_web_search):
        self.supports_web_search = supports_web_search


@pytest.mark.parametrize(
    "supports, analysis, expected",
    [
        (True, {"posting_type": "direct", "company_name": "Globex"}, True),
        # A headhunter listing has no employer to research; searching it risks
        # writing about the agency instead of the hiring company.
        (True, {"posting_type": "headhunter", "company_name": "Acme Recruit"}, False),
        (True, {"posting_type": "direct", "company_name": ""}, False),
        (False, {"posting_type": "direct", "company_name": "Globex"}, False),
    ],
)
def test_research_only_for_a_named_direct_employer(supports, analysis, expected):
    gen = LetterGenerator.__new__(LetterGenerator)
    gen.summary_model = FakeSelector(supports)
    assert gen._should_research(analysis) is expected
