"""Parse where the candidate wants to work and decide whether a job's location fits.

Job boards write locations inconsistently ("San Francisco, CA • New York, NY", "Remote (US)",
"US-Remote", "Hybrid - Seattle", "Remote - EMEA"), so matching is token/regex based and errs
toward precision: a remote job restricted to another region is not a fit for a US candidate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

US_STATES: dict[str, str] = dict(
    pair.split(":")
    for pair in (
        "al:alabama;ak:alaska;az:arizona;ar:arkansas;ca:california;co:colorado;ct:connecticut;de:delaware;"
        "dc:district of columbia;fl:florida;ga:georgia;hi:hawaii;id:idaho;il:illinois;in:indiana;ia:iowa;"
        "ks:kansas;ky:kentucky;la:louisiana;me:maine;md:maryland;ma:massachusetts;mi:michigan;mn:minnesota;"
        "ms:mississippi;mo:missouri;mt:montana;ne:nebraska;nv:nevada;nh:new hampshire;nj:new jersey;"
        "nm:new mexico;ny:new york;nc:north carolina;nd:north dakota;oh:ohio;ok:oklahoma;or:oregon;"
        "pa:pennsylvania;ri:rhode island;sc:south carolina;sd:south dakota;tn:tennessee;tx:texas;ut:utah;"
        "vt:vermont;va:virginia;wa:washington;wv:west virginia;wi:wisconsin;wy:wyoming"
    ).split(";")
)
STATE_BY_NAME = {name: abbr for abbr, name in US_STATES.items()}

# Default state for well-known US cities, used when the candidate types a bare city name.
US_CITIES: dict[str, str] = {
    "san francisco": "ca", "new york": "ny", "new york city": "ny", "seattle": "wa", "austin": "tx",
    "boston": "ma", "chicago": "il", "los angeles": "ca", "denver": "co", "atlanta": "ga", "miami": "fl",
    "dallas": "tx", "houston": "tx", "san jose": "ca", "san diego": "ca", "portland": "or",
    "philadelphia": "pa", "pittsburgh": "pa", "raleigh": "nc", "durham": "nc", "salt lake city": "ut",
    "phoenix": "az", "minneapolis": "mn", "detroit": "mi", "nashville": "tn", "columbus": "oh",
    "palo alto": "ca", "mountain view": "ca", "menlo park": "ca", "sunnyvale": "ca", "redwood city": "ca",
    "santa clara": "ca", "cupertino": "ca", "oakland": "ca", "san mateo": "ca", "newark": "nj",
    "irvine": "ca", "bellevue": "wa", "redmond": "wa", "kirkland": "wa", "cambridge": "ma",
    "brooklyn": "ny", "jersey city": "nj", "arlington": "va", "reston": "va", "herndon": "va",
    "charlotte": "nc", "boulder": "co", "ann arbor": "mi", "madison": "wi", "st. louis": "mo",
    "kansas city": "mo", "indianapolis": "in", "cincinnati": "oh", "cleveland": "oh", "orlando": "fl",
    "tampa": "fl", "baltimore": "md", "sacramento": "ca", "las vegas": "nv", "plano": "tx",
    "irving": "tx", "provo": "ut", "lehi": "ut", "washington dc": "dc", "washington, dc": "dc",
    "washington d.c.": "dc", "santa monica": "ca", "san antonio": "tx", "fort worth": "tx",
    "south san francisco": "ca", "emeryville": "ca", "foster city": "ca", "san bruno": "ca",
    "burlingame": "ca", "milpitas": "ca", "fremont": "ca", "hoboken": "nj", "stamford": "ct",
    "new haven": "ct", "providence": "ri", "richmond": "va", "mclean": "va", "tysons": "va",
    "bethesda": "md", "rockville": "md", "scottsdale": "az", "tempe": "az", "chandler": "az",
}

CITY_ALIASES: dict[str, list[str]] = {
    "san francisco": ["sf", "san francisco bay area", "sf bay area", "bay area"],
    "new york": ["nyc", "new york city", "manhattan"],
    "new york city": ["nyc", "new york", "manhattan"],
    "washington dc": ["washington, dc", "washington d.c.", "washington, d.c.", "dc metro"],
    "los angeles": ["greater los angeles"],
}

COUNTRIES: dict[str, list[str]] = {
    "us": ["united states", "united states of america", "usa", "us", "u.s.", "u.s.a."],
    "ca": ["canada"],
    "gb": ["united kingdom", "uk", "u.k.", "great britain", "england", "scotland", "wales"],
    "ie": ["ireland"],
    "de": ["germany", "deutschland"],
    "fr": ["france"],
    "nl": ["netherlands"],
    "es": ["spain"],
    "pt": ["portugal"],
    "it": ["italy"],
    "pl": ["poland"],
    "se": ["sweden"],
    "ch": ["switzerland"],
    "in": ["india"],
    "sg": ["singapore"],
    "au": ["australia"],
    "jp": ["japan"],
    "br": ["brazil", "brasil"],
    "mx": ["mexico", "méxico"],
    "ar": ["argentina"],
    "co": ["colombia"],
    "il": ["israel"],
    "ae": ["united arab emirates", "uae"],
    "ph": ["philippines"],
    "bg": ["bulgaria"],
    "ro": ["romania"],
    "ua": ["ukraine"],
    "za": ["south africa"],
    "ng": ["nigeria"],
    "kr": ["south korea", "korea"],
    "cn": ["china"],
    "hk": ["hong kong"],
    "tw": ["taiwan"],
    "nz": ["new zealand"],
}
# Major non-US cities, so "Remote - London" or "Bengaluru" in a job location resolves to a country.
COUNTRY_CITIES: dict[str, list[str]] = {
    "ca": ["toronto", "vancouver", "montreal", "ottawa", "calgary"],
    "gb": ["london", "manchester", "edinburgh"],
    "ie": ["dublin"],
    "de": ["berlin", "munich", "hamburg"],
    "fr": ["paris"],
    "nl": ["amsterdam"],
    "es": ["madrid", "barcelona"],
    "pt": ["lisbon"],
    "it": ["milan"],
    "pl": ["warsaw", "krakow"],
    "se": ["stockholm"],
    "ch": ["zurich"],
    "in": ["bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai", "gurgaon", "gurugram", "noida", "delhi"],
    "au": ["sydney", "melbourne"],
    "jp": ["tokyo"],
    "br": ["sao paulo", "são paulo"],
    "mx": ["mexico city"],
    "ar": ["buenos aires"],
    "co": ["bogota", "bogotá"],
    "il": ["tel aviv"],
    "ae": ["dubai"],
    "ph": ["manila"],
    "bg": ["sofia"],
    "ro": ["bucharest"],
    "ua": ["kyiv"],
    "za": ["cape town"],
    "ng": ["lagos"],
    "kr": ["seoul"],
    "cn": ["shanghai", "beijing", "shenzhen"],
    "tw": ["taipei"],
}
REGIONS: dict[str, set[str]] = {
    "north america": {"us", "ca", "mx"},
    "americas": {"us", "ca", "mx", "br", "ar", "co"},
    "latam": {"mx", "br", "ar", "co"},
    "latin america": {"mx", "br", "ar", "co"},
    "south america": {"br", "ar", "co"},
    "europe": {"gb", "ie", "de", "fr", "nl", "es", "pt", "it", "pl", "se", "ch", "bg", "ro", "ua"},
    "eu": {"ie", "de", "fr", "nl", "es", "pt", "it", "pl", "se", "bg", "ro"},
    "emea": {"gb", "ie", "de", "fr", "nl", "es", "pt", "it", "pl", "se", "ch", "bg", "ro", "ua", "il", "ae", "za", "ng"},
    "apac": {"in", "sg", "au", "jp", "ph", "kr", "cn", "hk", "tw", "nz"},
    "asia": {"in", "sg", "jp", "ph", "kr", "cn", "hk", "tw"},
    "africa": {"za", "ng"},
}
UNRESTRICTED_RE = re.compile(r"\b(worldwide|anywhere|global(ly)?|international|all locations)\b", re.I)
REMOTE_RE = re.compile(r"\b(remote|anywhere|worldwide|distributed|work from home|wfh|telecommute)\b", re.I)
SEGMENT_SPLIT_RE = re.compile(r"\s*(?:•|;|\||\n|/| or | and )\s*")


def _alias_re(words: list[str]) -> re.Pattern[str]:
    parts = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![\w.])(?:" + "|".join(parts) + r")(?![\w])", re.I)


COUNTRY_RES = {code: _alias_re(names + COUNTRY_CITIES.get(code, [])) for code, names in COUNTRIES.items()}
REGION_RES = {name: _alias_re([name]) for name in REGIONS}
STATE_NAME_RE = _alias_re(list(STATE_BY_NAME))
STATE_ABBR_RE = re.compile(r"(?:,|\(|-|^)\s*(" + "|".join(a.upper() for a in US_STATES) + r")\b(?!\w)")
US_CITY_RE = _alias_re(list(US_CITIES))


@dataclass
class Place:
    kind: str  # city | state | country | remote
    city: str = ""
    state: str = ""  # two-letter, lowercase
    state_explicit: bool = False
    country: str = ""  # ISO-ish code from COUNTRIES

    @property
    def label(self) -> str:
        if self.kind == "remote":
            return f"Remote ({self.country.upper()})" if self.country else "Remote"
        if self.kind == "city":
            title = " ".join(w.capitalize() if w not in {"dc"} else "DC" for w in self.city.split())
            if self.state and self.state != "dc" and "dc" not in self.city:
                return f"{title}, {self.state.upper()}"
            return title
        if self.kind == "state":
            return US_STATES[self.state].title()
        return {"us": "United States"}.get(self.country, self.country.upper())


@dataclass
class Wanted:
    places: list[Place] = field(default_factory=list)

    @property
    def remote(self) -> bool:
        return any(p.kind == "remote" for p in self.places)

    @property
    def home_countries(self) -> set[str]:
        return {p.country for p in self.places if p.country}

    @property
    def onsite(self) -> list[Place]:
        return [p for p in self.places if p.kind != "remote"]

    def labels(self) -> list[str]:
        return [p.label for p in self.places]

    def primary(self) -> str:
        """First concrete place, for 'Location' fields on application forms."""
        for p in self.places:
            if p.kind == "city":
                return p.label
        return ""


def country_code(token: str) -> str:
    """'Canada' / 'United States of America' / 'uk' -> ISO-ish code used in COUNTRIES, or ''."""
    return _country_of(token)


def _country_of(token: str) -> str:
    t = token.strip().lower().rstrip(".")
    for code, names in COUNTRIES.items():
        if t in {n.rstrip(".") for n in names}:
            return code
    return ""


def _state_of(token: str) -> str:
    t = token.strip().lower().rstrip(".")
    if t in US_STATES and len(token.strip()) == 2:
        return t
    return STATE_BY_NAME.get(t, "")


def parse_locations(raw: str) -> Wanted:
    """'San Francisco, CA, Remote' -> [San Francisco CA (city), Remote]. Commas both separate places
    and attach states/countries to the preceding city, so the tokens are folded left to right."""
    raw = re.sub(r"\bwashington,?\s*d\.?\s?c\.?(?=\W|$)", "Washington DC", raw or "", flags=re.I)
    tokens = [t.strip() for t in re.split(r"[,;\n|]", raw) if t.strip()]
    places: list[Place] = []
    for token in tokens:
        low = re.sub(r"\s+", " ", token.lower())
        prev = places[-1] if places else None
        if REMOTE_RE.search(low):
            rest = REMOTE_RE.sub(" ", low).strip(" -()")
            places.append(Place("remote", country=_country_of(rest) if rest else ""))
            continue
        if low in US_CITIES:
            places.append(Place("city", city=low, state=US_CITIES[low], country="us"))
            continue
        state = _state_of(token)
        if state:
            if prev and prev.kind == "city" and not prev.state_explicit:
                prev.state, prev.state_explicit, prev.country = state, True, "us"
            else:
                places.append(Place("state", state=state, state_explicit=True, country="us"))
            continue
        country = _country_of(token)
        if country:
            if prev and prev.kind == "remote" and not prev.country:
                prev.country = country
            elif prev and prev.kind == "city":
                prev.country = country
                if country != "us":
                    prev.state, prev.state_explicit = "", False
            elif not (prev and prev.kind == "state" and country == "us"):
                places.append(Place("country", country=country))
            continue
        places.append(Place("city", city=low, country=_city_country(low)))
    return Wanted(places)


def _city_country(city: str) -> str:
    for code, cities in COUNTRY_CITIES.items():
        if city in cities:
            return code
    return ""


def _segments(text: str) -> list[str]:
    return [s for s in SEGMENT_SPLIT_RE.split(text or "") if s and s.strip()]


def _states_in(segment: str) -> set[str]:
    # "Berlin, DE" / "Hyderabad, IN" / "Toronto, ON, CA": two-letter country codes collide with state codes,
    # so abbreviations only count when nothing in the segment points at another country.
    if _foreign_countries(segment):
        return set()
    found = {m.group(1).lower() for m in STATE_ABBR_RE.finditer(segment)}
    found |= {STATE_BY_NAME[m.group(0).lower()] for m in STATE_NAME_RE.finditer(segment)}
    return found


def _foreign_countries(text: str) -> set[str]:
    return {code for code, rx in COUNTRY_RES.items() if code != "us" and rx.search(text)}


def countries_in(text: str) -> set[str]:
    """Countries a location string points at, including via US states/cities and regions.
    Evaluated per segment so "Austin, TX • Toronto, Canada" yields both US and Canada."""
    found: set[str] = set()
    for segment in _segments(text) or [text]:
        here = {code for code, rx in COUNTRY_RES.items() if rx.search(segment)}
        if not here and (STATE_ABBR_RE.search(segment) or STATE_NAME_RE.search(segment) or US_CITY_RE.search(segment)):
            here.add("us")
        found |= here
    for name, rx in REGION_RES.items():
        if rx.search(text):
            found |= REGIONS[name] | {f"region:{name}"}
    return found


def _city_in(place: Place, segment: str) -> bool:
    names = [place.city] + CITY_ALIASES.get(place.city, [])
    if not _alias_re(names).search(segment):
        return False
    if place.state_explicit:
        states = _states_in(segment)
        if states and place.state not in states:
            return False
        if not states:
            other = countries_in(segment) - {"us"}
            if place.country == "us" and other and not any(c.startswith("region:") for c in other):
                return False
    return True


def is_remote(location: str, remote_flag: bool = False) -> bool:
    return remote_flag or bool(REMOTE_RE.search(location or ""))


def match_location(location: str, remote_flag: bool, wanted: Wanted) -> tuple[str, str]:
    """Return (kind, reason). kind is '' when the job doesn't fit; otherwise city/state/country/remote/any."""
    if not wanted.places:
        return "any", ""
    loc = location or ""
    segments = _segments(loc) or [loc]
    for place in wanted.onsite:
        if place.kind == "city" and any(_city_in(place, s) for s in segments):
            return "city", f"Location: {loc}"
    for place in wanted.onsite:
        if place.kind == "state" and any(place.state in _states_in(s) for s in segments):
            return "state", f"Location: {loc}"
    for place in wanted.onsite:
        if place.kind == "country" and place.country in countries_in(loc):
            return "country", f"Location: {loc}"

    if wanted.remote and is_remote(loc, remote_flag):
        homes = {p.country for p in wanted.places if p.kind == "remote" and p.country} or wanted.home_countries
        if not homes or UNRESTRICTED_RE.search(loc):
            return "remote", "Remote-friendly"
        remote_text = " ".join(s for s in segments if REMOTE_RE.search(s)) or loc
        mentioned = {c for c in countries_in(remote_text) if not c.startswith("region:")}
        if not mentioned and remote_text != loc:
            mentioned = {c for c in countries_in(loc) if not c.startswith("region:")}
        if not mentioned or mentioned & homes:
            return "remote", f"Remote ({', '.join(sorted(h.upper() for h in homes))} OK)"
    return "", ""
