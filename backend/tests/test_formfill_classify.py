import asyncio

import pytest

from app.agent.formfill import _classify, _fill_field, _pick_years_option, _planned_value, _radio_option, _values


def field(label, type_="text", tag="input", name="", placeholder="", autocomplete=""):
    return {"label": label, "type": type_, "tag": tag, "name": name, "idAttr": "", "placeholder": placeholder,
            "autocomplete": autocomplete, "options": [], "combobox": False}


@pytest.mark.parametrize(
    "f, kind",
    [
        (field("Name"), "full_name"),
        (field("Legal Name"), "full_name"),
        (field("Name Pronunciation"), None),
        (field("First Name"), "first_name"),
        (field("Preferred First Name"), "first_name"),
        (field("Last name"), "last_name"),
        (field("Email address", "email"), "email"),
        (field("Phone Number", "tel"), "phone"),
        (field("LinkedIn Profile"), "linkedin"),
        (field("GitHub URL"), "github"),
        (field("Location (City)"), "location"),
        (field("Gender"), None),
        (field("Will you now or in the future require sponsorship?"), None),
        (field("How did you hear about us?"), None),
        (field("What is your expected compensation?"), None),
        (field("Resume/CV", "file"), "resume"),
        (field("Cover Letter", "file"), "cover_file"),
        (field("Autofill from resume", "file"), None),
        (field("Cover Letter", "textarea", "textarea"), "cover_letter"),
        (field("", autocomplete="given-name"), "first_name"),
    ],
)
def test_classify(f, kind):
    assert _classify(f) == kind


def radio_field(options, answer_key="work_authorization", label="Are you authorized to work in the US?"):
    return {"label": label, "type": "radio", "tag": "input", "name": "auth", "idAttr": "", "placeholder": "",
            "autocomplete": "", "options": options, "combobox": False, "kind": None, "answer_key": answer_key}


def test_radio_value_comes_from_the_answer_bank_only():
    f = radio_field(["Yes", "No"])
    # _classify never maps a radio group to a resume kind...
    assert _classify(f) is None
    # ...so the only value it can ever get is a saved answer, by canonical key.
    assert _planned_value(f, {"work_authorization": "Yes"}, None) == ("work_authorization", "Yes")
    # Nothing stored -> nothing planned; the group is left for the user.
    assert _planned_value(f, {}, None)[1] is None


def test_radio_option_matching():
    f = radio_field(["Yes", "No"])
    assert _radio_option(f, "yes") == "Yes"
    assert _radio_option(f, "No") == "No"
    # A stored answer that matches no option on this form selects nothing.
    assert _radio_option(f, "Prefer not to say") is None
    assert _radio_option(radio_field([]), "Yes") is None


def test_radio_eeo_answer_applies_only_when_stored():
    # EEO answers live in the bank only when the user opted in per question;
    # the filler applies one only when it is actually there.
    f = radio_field(["Female", "Male"], answer_key="gender", label="Gender")
    assert _planned_value(f, {}, None)[1] is None
    kind, value = _planned_value(f, {"gender": "Female"}, None)
    assert (kind, value) == ("gender", "Female")
    assert _radio_option(f, value) == "Female"


class _FakeFrame:
    """Stands in for a Playwright frame: records evaluate() calls, and the
    text-input locator must never be touched for a radio group."""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def locator(self, _selector):
        raise AssertionError("radio fill must not use the text locator")

    async def evaluate(self, _js, args):
        self.calls.append(args)
        return self.result


def test_fill_field_clicks_the_matching_radio_option():
    frame = _FakeFrame(result=True)
    f = {**radio_field(["Yes", "No"]), "id": 7, "frame": frame}
    assert asyncio.run(_fill_field(f, "work_authorization", "Yes")) is True
    assert frame.calls == [{"id": 7, "option": "Yes"}]


def test_fill_field_leaves_an_unmatched_radio_group_alone():
    frame = _FakeFrame(result=True)
    f = {**radio_field(["Yes", "No"]), "id": 7, "frame": frame}
    assert asyncio.run(_fill_field(f, "work_authorization", "Maybe")) is False
    assert frame.calls == []  # no click was even attempted


def test_fill_field_reports_an_unselected_option_as_unfilled():
    frame = _FakeFrame(result=False)
    f = {**radio_field(["Yes", "No"]), "id": 7, "frame": frame}
    assert asyncio.run(_fill_field(f, "work_authorization", "Yes")) is False


def test_years_option_and_values():
    assert _pick_years_option(["Select", "0-2", "3-5", "6-9", "10+"], 7) == "6-9"
    assert _pick_years_option(["Select", "0-2", "3-5", "6-9", "10+"], 14) == "10+"
    v = _values({"name": "Jane Q. Doe", "linkedin": "linkedin.com/in/jane", "github": "", "years_experience": 6},
                "Newark, CA", "Dear team", {})
    assert (v["first_name"], v["last_name"]) == ("Jane", "Doe")
    assert v["linkedin"] == "https://linkedin.com/in/jane" and v["website"] == ""
    assert v["location"] == "Newark, CA" and v["years"] == "6"
