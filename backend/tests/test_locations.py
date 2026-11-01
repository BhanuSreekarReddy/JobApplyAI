import pytest

from app.agent.locations import match_location, parse_locations


@pytest.mark.parametrize(
    "raw, labels",
    [
        ("San Francisco, CA, Remote", ["San Francisco, CA", "Remote"]),
        ("San Francisco, Remote", ["San Francisco, CA", "Remote"]),
        ("Austin, TX, USA; New York", ["Austin, TX", "New York, NY"]),
        ("New York, NY", ["New York, NY"]),
        ("Washington, DC", ["Washington DC"]),
        ("Seattle, Washington", ["Seattle, WA"]),
        ("Newark, CA", ["Newark, CA"]),
        ("Portland, ME", ["Portland, ME"]),
        ("California", ["California"]),
        ("United States", ["United States"]),
        ("Remote, US", ["Remote (US)"]),
        ("London, UK", ["London"]),
        ("Cambridge, UK", ["Cambridge"]),
        ("", []),
    ],
)
def test_parse_locations(raw, labels):
    assert parse_locations(raw).labels() == labels


def test_parse_keeps_country_context():
    w = parse_locations("Cambridge, UK")
    assert w.places[0].country == "gb" and w.places[0].state == ""
    assert parse_locations("San Francisco, CA, Remote").home_countries == {"us"}
    assert parse_locations("Remote").home_countries == set()


def test_primary_location_is_first_city():
    assert parse_locations("Remote, Newark, CA").primary() == "Newark, CA"
    assert parse_locations("Remote").primary() == ""


SF_REMOTE = parse_locations("San Francisco, CA, Remote")


@pytest.mark.parametrize(
    "location, remote, kind",
    [
        ("San Francisco, CA", False, "city"),
        ("San Francisco, CA • New York, NY • United States", False, "city"),
        ("SF Bay Area", False, "city"),
        ("San Francisco, California", False, "city"),
        ("Hybrid - San Francisco", False, "city"),
        ("Remote (US)", True, "remote"),
        ("US-Remote", False, "remote"),
        ("Remote - United States", False, "remote"),
        ("Remote", False, "remote"),
        ("Worldwide", True, "remote"),
        ("Remote - Americas", False, "remote"),
        ("New York, NY (HQ) • Remote (Canada) • Remote (US)", True, "remote"),
        ("Remote (Canada)", True, ""),
        ("Remote - EMEA", True, ""),
        ("Europe", True, ""),
        ("Bulgaria", True, ""),
        ("Remote - Latin America", True, ""),
        ("London, UK", False, ""),
        ("Berlin", False, ""),
        ("Seattle, WA", False, ""),
        ("Canada", False, ""),
    ],
)
def test_match_us_candidate(location, remote, kind):
    assert match_location(location, remote, SF_REMOTE)[0] == kind


def test_state_conflict_rejects_same_named_city():
    newark_ca = parse_locations("Newark, CA")
    assert match_location("Newark, CA", False, newark_ca)[0] == "city"
    assert match_location("Newark, California, United States", False, newark_ca)[0] == "city"
    assert match_location("Newark, NJ", False, newark_ca)[0] == ""
    # A bare city name is not state-qualified, so both Newarks fit.
    assert match_location("Newark, NJ", False, parse_locations("Newark"))[0] == "city"


def test_country_and_state_places():
    us = parse_locations("United States")
    assert match_location("Austin, TX", False, us)[0] == "country"
    assert match_location("Toronto, Canada", False, us)[0] == ""
    ca = parse_locations("California")
    assert match_location("Mountain View, CA", False, ca)[0] == "state"
    assert match_location("Austin, TX", False, ca)[0] == ""


def test_remote_without_home_country_accepts_any_remote():
    anywhere = parse_locations("Remote")
    assert match_location("Remote - EMEA", True, anywhere)[0] == "remote"
    assert match_location("Berlin", False, anywhere)[0] == ""


def test_non_us_candidate_remote():
    uk = parse_locations("London, UK, Remote")
    assert match_location("Remote - EMEA", True, uk)[0] == "remote"
    assert match_location("Remote (US)", True, uk)[0] == ""
    assert match_location("London, United Kingdom", False, uk)[0] == "city"


def test_no_locations_matches_everything():
    assert match_location("Anywhere at all", False, parse_locations(""))[0] == "any"


@pytest.mark.parametrize(
    "location",
    ["Berlin, DE", "Hyderabad, IN", "Toronto, ON, CA", "IN-Pune", "DE-Berlin-Trion Building", "Munich, DE",
     "CA - Canada", "IL-Israel-Remote"],
)
def test_country_codes_are_not_us_states(location):
    assert match_location(location, False, parse_locations("United States"))[0] == ""


def test_country_codes_do_not_satisfy_state_or_remote_searches():
    assert match_location("Bengaluru, IN", False, parse_locations("Indiana"))[0] == ""
    assert match_location("Vancouver, BC, CA", False, parse_locations("California"))[0] == ""
    assert match_location("Remote - Bengaluru, IN", True, parse_locations("Remote, US"))[0] == ""
    assert match_location("Remote, CO, Colombia", True, parse_locations("Remote, US"))[0] == ""
    # Real US states still work, including next to a foreign office in another segment.
    assert match_location("Indianapolis, IN", False, parse_locations("Indiana"))[0] == "state"
    assert match_location("Austin, TX • Toronto, Canada", False, parse_locations("United States"))[0] == "country"
