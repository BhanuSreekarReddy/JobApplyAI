import pytest

from app.agent.formfill import _classify, _pick_years_option, _values


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


def test_years_option_and_values():
    assert _pick_years_option(["Select", "0-2", "3-5", "6-9", "10+"], 7) == "6-9"
    assert _pick_years_option(["Select", "0-2", "3-5", "6-9", "10+"], 14) == "10+"
    v = _values({"name": "Jane Q. Doe", "linkedin": "linkedin.com/in/jane", "github": "", "years_experience": 6},
                "Newark, CA", "Dear team", {})
    assert (v["first_name"], v["last_name"]) == ("Jane", "Doe")
    assert v["linkedin"] == "https://linkedin.com/in/jane" and v["website"] == ""
    assert v["location"] == "Newark, CA" and v["years"] == "6"
