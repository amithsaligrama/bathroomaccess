"""
Deduplicate bathrooms, fix title case, and clear bogus hours.

- Deduplicates by location (lat/lon); keeps record with hours/remarks when available
- Converts ALL CAPS names and addresses to Title Case (preserves state abbrevs like MA, CA)
- Adds state abbreviation to addresses when missing (from zip code)
- Clears hours when it's a numeric code (e.g. from PLS data) rather than real hours
"""
import json
import re
import time
import urllib.request
from collections import defaultdict

from django.core.management.base import BaseCommand

from bathroom_map.models import Bathroom
from bathroom_map.utils import US_STATE_ABBREVS, ensure_state_in_address


def ensure_suffix(name):
    """Add 'Library' or 'Town Hall' suffix when missing."""
    if not name or not name.strip():
        return name
    n = name.strip()
    nl = n.lower()
    if ("library" in nl or " lib " in nl or nl.endswith(" lib")) and not nl.endswith("library"):
        return n.rstrip() + " Library"
    if "municipal" in nl and "city hall" not in nl:
        return n.rstrip() + " City Hall"
    if ("town hall" in nl or "city hall" in nl) and not (nl.endswith("town hall") or nl.endswith("city hall")):
        return n.rstrip() + (" City Hall" if "city" in nl else " Town Hall")
    return n


def fetch_hours_from_osm(lat, lon):
    """Query OSM Overpass for opening_hours near point. Returns hours string or None."""
    try:
        url = (
            "https://overpass-api.de/api/interpreter?"
            "data=[out:json][timeout:5];"
            "(node(around:80,{},{})[opening_hours];"
            "way(around:80,{},{})[opening_hours];);"
            "out body tags;"
        ).format(lat, lon, lat, lon)
        req = urllib.request.Request(url, headers={"User-Agent": "BathroomAccess/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            hours = tags.get("opening_hours") or tags.get("opening_hours:source")
            if hours and len(hours) > 3 and not re.match(r"^[\d\s,\.]+$", hours.strip()):
                return hours.strip()
    except Exception:
        pass
    return None


def title_case(s):
    """Convert 'CITYNAME TOWN HALL' to 'Cityname Town Hall'. Preserves state abbreviations (MA, CA, etc)."""
    if not s or not s.strip():
        return s
    parts = s.strip().split()
    result = []
    for word in parts:
        upp = word.upper()
        # Preserve 2-letter state abbreviations (MA, CA, NY)
        if len(word) == 2 and upp in US_STATE_ABBREVS:
            result.append(upp)
        else:
            result.append(word.title())
    return " ".join(result)


def is_bogus_hours(hours):
    """True if hours looks like a numeric code, not real hours text."""
    if not hours or not hours.strip():
        return False
    stripped = hours.strip()
    # Purely numeric (with optional spaces, commas, decimals) = bogus
    if re.match(r"^[\d\s,\.]+$", stripped):
        return True
    # Very short numeric-looking string
    if len(stripped) <= 4 and stripped.replace(".", "").isdigit():
        return True
    return False


class Command(BaseCommand):
    help = "Deduplicate bathrooms, fix title case, clear bogus hours"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--skip-hours-fetch",
            action="store_true",
            help="Skip fetching hours from OSM (avoids slow API calls)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write("DRY RUN - no changes will be saved\n")

        # 1. Title-case name and address
        updated = 0
        for b in Bathroom.objects.all():
            new_name = title_case(b.name)
            new_addr = title_case(b.address)
            changed = False
            if new_name != b.name:
                if not dry_run:
                    b.name = new_name
                changed = True
            if new_addr != b.address:
                if not dry_run:
                    b.address = new_addr
                changed = True
            if changed:
                updated += 1
                if not dry_run:
                    b.save()

        self.stdout.write("Title case: {} records updated".format(updated))

        # 2. Add state abbreviation to addresses when missing
        state_added = 0
        for b in Bathroom.objects.all():
            new_addr = ensure_state_in_address(b.address or "", b.zip or "")
            if new_addr and new_addr != (b.address or ""):
                if not dry_run:
                    b.address = new_addr
                    b.save()
                state_added += 1
        self.stdout.write("State abbreviation added: {} records updated".format(state_added))

        # 3. Add Library/Town Hall suffix
        suffixed = 0
        for b in Bathroom.objects.all():
            new_name = ensure_suffix(title_case(b.name))
            if new_name != b.name:
                if not dry_run:
                    b.name = new_name
                    b.save()
                suffixed += 1
        self.stdout.write("Library/Town Hall suffix: {} records updated".format(suffixed))

        # 4. Clear bogus hours
        cleared = 0
        for b in Bathroom.objects.all():
            if is_bogus_hours(b.hours):
                if not dry_run:
                    b.hours = ""
                    b.save()
                cleared += 1

        self.stdout.write("Cleared bogus hours: {} records".format(cleared))

        # 5. Fetch hours from OSM for records missing hours
        hours_fetched = 0
        if not options.get("skip_hours_fetch", False):
            for b in Bathroom.objects.all():
                if not b.hours or not b.hours.strip() or is_bogus_hours(b.hours):
                    if b.latitude and b.longitude:
                        lat, lon = float(b.latitude), float(b.longitude)
                        if -90 <= lat <= 90 and -180 <= lon <= 180:
                            hrs = fetch_hours_from_osm(lat, lon)
                            if hrs:
                                if not dry_run:
                                    b.hours = hrs
                                    b.save()
                                hours_fetched += 1
                            time.sleep(1.05)
        self.stdout.write("Hours fetched from OSM: {} records".format(hours_fetched))

        # 6. Deduplicate by (lat, lon) rounded to 5 decimals (~1m)
        def coord_key(b):
            lat = float(b.latitude) if b.latitude else 0
            lon = float(b.longitude) if b.longitude else 0
            return (round(lat, 5), round(lon, 5))

        groups = defaultdict(list)
        for b in Bathroom.objects.all():
            groups[coord_key(b)].append(b)

        deleted = 0
        for key, group in groups.items():
            if len(group) <= 1:
                continue
            # Prefer record with hours or remarks
            def score(r):
                has_hrs = bool(r.hours and r.hours.strip() and not is_bogus_hours(r.hours))
                has_rem = bool(r.remarks and r.remarks.strip())
                return (has_hrs, has_rem, len(r.hours or "") + len(r.remarks or ""))

            group.sort(key=score, reverse=True)
            keep = group[0]
            for dup in group[1:]:
                if not dry_run:
                    dup.delete()
                deleted += 1

        self.stdout.write("Duplicates removed: {} records".format(deleted))

        total = Bathroom.objects.count()
        self.stdout.write(
            self.style.SUCCESS("\nDone. {} bathroom locations remain.".format(total))
        )
