"""
Utilities for address parsing, place extraction, and state abbreviations.
"""
import re

# US state abbreviations - preserve these (don't title-case to "Ma", "Ca")
US_STATE_ABBREVS = frozenset([
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
])

# Map state abbreviation to full name (for city URL slugs like belmont-massachusetts)
STATE_ABBREV_TO_FULL = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode-island", "SC": "south-carolina",
    "SD": "south-dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west-virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district-of-columbia",
}


# Known state slug suffixes for parsing city param (multi-word states)
STATE_SLUGS = frozenset(v for v in STATE_ABBREV_TO_FULL.values())


def city_slug(city_name, state_abbrev):
    """Return URL slug for city param, e.g. 'Belmont, MA' -> 'belmont-massachusetts'."""
    if not city_name or not state_abbrev:
        return None
    state_full = STATE_ABBREV_TO_FULL.get(state_abbrev.upper())
    if not state_full:
        return None
    city_s = city_name.strip().lower().replace(" ", "-").replace("'", "")
    return "{}-{}".format(city_s, state_full)


def parse_city_slug(slug):
    """Parse 'belmont-massachusetts' or 'concord-new-hampshire' -> (city, state) for geocoding."""
    if not slug or not isinstance(slug, str):
        return (None, None)
    parts = slug.lower().strip().split("-")
    if len(parts) < 2:
        return (None, None)
    for n in range(1, min(5, len(parts)) + 1):
        state_slug = "-".join(parts[-n:])
        if state_slug in STATE_SLUGS:
            city_slug = "-".join(parts[:-n]) if len(parts) > n else ""
            city = city_slug.replace("-", " ")
            state = state_slug.replace("-", " ")
            return (city, state) if city else (None, None)
    state = parts[-1]
    city = " ".join(p.replace("-", " ") for p in parts[:-1])
    return (city, state)


def get_state_from_zip(zip_code):
    """Return state abbreviation for a US zip code, or None."""
    if not zip_code:
        return None
    zip_str = str(zip_code).strip()
    # Handle Zip+4 format (12345-6789)
    if "-" in zip_str:
        zip_str = zip_str.split("-")[0]
    if len(zip_str) >= 5 and zip_str[:5].isdigit():
        try:
            import zipcodes
            matches = zipcodes.matching(zip_str[:5])
            if matches:
                return matches[0].get("state")
        except Exception:
            pass
    return None


def parse_city_state_from_address(address, zip_code=None):
    """
    Extract (city, state) from an address string.
    Returns (city, state) where state may be None if not found.
    Address formats: "123 Main St, Boston, MA" or "123 Main St, Boston" or "123 Main St, Palo Alto, CA"
    """
    if not address or not address.strip():
        return (None, None)
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return (None, None)

    city = None
    state = None

    # Last part: might be "MA", "CA" (state) or "Boston" (city) or "94301" (zip)
    if len(parts) >= 2:
        last = parts[-1].upper()
        # Two letters = likely state abbreviation
        if len(last) == 2 and last in US_STATE_ABBREVS:
            state = last
            city = parts[-2] if len(parts) >= 2 else None
        # Five digits = zip in address
        elif len(last) == 5 and last.isdigit():
            city = parts[-2] if len(parts) >= 2 else None
            if zip_code:
                state = get_state_from_zip(zip_code)
            else:
                state = get_state_from_zip(last)
        else:
            # Last part is likely city (e.g. "Boston", "Palo Alto")
            city = parts[-1]
            if zip_code:
                state = get_state_from_zip(zip_code)

    elif len(parts) == 1:
        city = parts[0]
        if zip_code:
            state = get_state_from_zip(zip_code)

    return (city, state)


def ensure_state_in_address(address, zip_code):
    """
    If address doesn't end with a state abbreviation, append it (from zip) when possible.
    Returns the address, possibly with state appended.
    """
    if not address or not address.strip():
        return address
    addr = address.strip()
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if not parts:
        return address

    last = parts[-1].upper()
    # Already has state abbreviation at end
    if len(last) == 2 and last in US_STATE_ABBREVS:
        return address
    # Last part is 5-digit zip in address - insert state before it if we have zip
    if len(last) == 5 and last.isdigit():
        state = get_state_from_zip(zip_code or last)
        if state and len(parts) >= 2:
            second_last = parts[-2].upper()
            if len(second_last) == 2 and second_last in US_STATE_ABBREVS:
                return address
            # "123 Main St, Boston, 02101" -> "123 Main St, Boston, MA"
            return "{}, {}".format(addr.rsplit(",", 1)[0].strip(), state)
        return address

    state = get_state_from_zip(zip_code)
    if state:
        return "{}, {}".format(addr, state)
    return address
