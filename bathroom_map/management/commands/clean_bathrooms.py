"""
Deduplicate bathrooms, fix title case, and clear bogus hours.

- Deduplicates by normalized address (incl. St vs Street, etc.); keeps richer record
- Deduplicates by proximity (lat/lon within a few dozen meters); keeps richer record
- Deduplicates by identical rounded coordinates (~1 m); keeps richer record
- Converts ALL CAPS names and addresses to Title Case (preserves state abbrevs like MA, CA)
- Adds state abbreviation to addresses when missing (from zip code)
- Clears hours when it's a numeric code (e.g. from PLS data) rather than real hours
"""
import json
import math
import re
import time
import urllib.request
import urllib.parse
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


def fetch_hours_from_osm(lat, lon, radius_m=80, timeout_s=5):
    """Query OSM Overpass for opening_hours near point. Returns hours string or None."""
    try:
        # Overpass query parameter must be URL-encoded (spaces + special chars break urllib).
        ql = (
            "[out:json][timeout:{timeout_s}];"
            "(node(around:{radius_m},{lat},{lon})[opening_hours];"
            "way(around:{radius_m},{lat},{lon})[opening_hours];);"
            "out body tags;"
        ).format(timeout_s=timeout_s, radius_m=radius_m, lat=lat, lon=lon)
        url = (
            "https://overpass-api.de/api/interpreter?"
            + urllib.parse.urlencode({"data": ql})
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "BathroomAccess/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
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


def normalize_address_for_dedup(address):
    """Stable key for grouping duplicate addresses (after title-case passes).

    Maps common street-type spellings to one form (e.g. Street -> st) so
    "2 Boylston St" and "2 Boylston Street" match.
    """
    if not address or not str(address).strip():
        return ""
    s = re.sub(r"\s+", " ", str(address).strip().lower())
    s = s.replace(".", "")
    s = re.sub(r"\s+", " ", s).strip()
    # USPS-style: full words -> abbreviations (order: longer words first where relevant)
    for pattern, repl in (
        (r"\bboulevard\b", "blvd"),
        (r"\bavenue\b", "ave"),
        (r"\bstreet\b", "st"),
        (r"\bdrive\b", "dr"),
        (r"\broad\b", "rd"),
        (r"\blane\b", "ln"),
        (r"\bcourt\b", "ct"),
        (r"\bcircle\b", "cir"),
        (r"\bplace\b", "pl"),
        (r"\bterrace\b", "ter"),
        (r"\bparkway\b", "pkwy"),
        (r"\bhighway\b", "hwy"),
        (r"\btrail\b", "trl"),
        (r"\bsquare\b", "sq"),
    ):
        s = re.sub(pattern, repl, s)
    return re.sub(r"\s+", " ", s).strip()


def is_bogus_hours(hours):
    """True if hours looks like a numeric code, not real hours text."""
    if not hours or not hours.strip():
        return False
    stripped = hours.strip()
    # Purely numeric (with optional spaces, commas, decimals) = bogus
    if re.match(r"^[+\-]?[\d\s,\.]+$", stripped):
        return True
    # Very short numeric-looking string
    compact = stripped.replace(".", "").replace("-", "").replace("+", "")
    if len(stripped) <= 5 and compact.isdigit():
        return True
    return False


def bathroom_richness_score(record):
    """Higher = prefer keeping this row: real hours, remarks, then total text length."""
    has_hrs = bool(
        record.hours
        and str(record.hours).strip()
        and not is_bogus_hours(record.hours)
    )
    has_rem = bool(record.remarks and str(record.remarks).strip())
    return (
        has_hrs,
        has_rem,
        len(record.hours or "") + len(record.remarks or ""),
    )


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two WGS84 points in meters."""
    r_earth = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(
        dlam / 2
    ) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return r_earth * c


def cluster_indices_proximity(points, radius_m):
    """
    points: list of (lat, lon) for each record.
    Returns list of lists; each inner list is row indices forming one cluster (transitive
    within radius_m). Grid bucketing + union-find.
    """
    n = len(points)
    if n == 0:
        return []
    # Cell slightly smaller than radius (deg) so true distance is always checked; use a 5x5
    # neighbor scan so EW separation at mid-latitudes is covered (lon degrees shrink vs lat).
    cell_deg = max(radius_m / 111000.0, 1e-7)
    neighbor_range = 2
    buckets = defaultdict(list)
    for idx, (lat, lon) in enumerate(points):
        cx = int(lat / cell_deg)
        cy = int(lon / cell_deg)
        buckets[(cx, cy)].append(idx)

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (cx, cy), member_idxs in buckets.items():
        extended = []
        for dx in range(-neighbor_range, neighbor_range + 1):
            for dy in range(-neighbor_range, neighbor_range + 1):
                extended.extend(buckets.get((cx + dx, cy + dy), []))
        for a in member_idxs:
            lat_a, lon_a = points[a]
            for b in extended:
                if b <= a:
                    continue
                lat_b, lon_b = points[b]
                if haversine_m(lat_a, lon_a, lat_b, lon_b) <= radius_m:
                    union(a, b)

    by_root = defaultdict(list)
    for i in range(n):
        by_root[find(i)].append(i)
    return list(by_root.values())


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
        parser.add_argument(
            "--progress-every",
            type=int,
            default=10,
            help="Print progress every N records while fetching hours from OSM",
        )
        parser.add_argument(
            "--overpass-radius-m",
            type=float,
            default=80,
            help="Search radius (meters) for opening_hours near each bathroom point",
        )
        parser.add_argument(
            "--overpass-timeout-s",
            type=int,
            default=5,
            help="Overpass + request timeout (seconds)",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=1.05,
            help="Sleep between Overpass requests (be nice to rate limits)",
        )
        parser.add_argument(
            "--proximity-radius-m",
            type=float,
            default=55,
            help=(
                "Merge bathroom rows within this distance (meters) using lat/lon; "
                "catches same place with different address text (default: 55)"
            ),
        )
        parser.add_argument(
            "--skip-proximity-dedup",
            action="store_true",
            help="Skip merging duplicates by geographic proximity",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        progress_every = max(1, int(options.get("progress_every") or 10))
        started_at = time.time()
        radius_m = float(options.get("overpass_radius_m") or 80)
        timeout_s = int(options.get("overpass_timeout_s") or 5)
        sleep_seconds = float(options.get("sleep_seconds") or 1.05)
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

        # 2a. Collapse repeated trailing state abbreviations (e.g., ", PR, PR, PR" -> ", PR")
        state_suffix_collapsed = 0
        for b in Bathroom.objects.all():
            addr = (b.address or "").strip()
            if not addr:
                continue
            parts = [p.strip() for p in addr.split(",") if p.strip()]
            if len(parts) < 2:
                continue
            changed = False
            # Remove repeated same trailing 2-letter region codes.
            while len(parts) >= 2:
                last = parts[-1].upper()
                prev = parts[-2].upper()
                if len(last) == 2 and last == prev:
                    parts.pop()
                    changed = True
                else:
                    break
            if changed:
                new_addr = ", ".join(parts)
                if not dry_run:
                    b.address = new_addr
                    b.save(update_fields=["address"])
                state_suffix_collapsed += 1
        self.stdout.write(
            "Repeated state suffix collapsed: {} records updated".format(
                state_suffix_collapsed
            )
        )

        # 2b. Replace placeholder zip codes with nearest known zip (by coordinates)
        # Helps rows imported from point-only datasets that defaulted to 00000.
        zip_fixed = 0
        known_zip_points = []
        by_cell = defaultdict(list)
        cell_size = 0.1  # degrees; coarse spatial bucket for fast nearest lookup

        def cell_key(lat, lon):
            return (int(math.floor(lat / cell_size)), int(math.floor(lon / cell_size)))

        for b in Bathroom.objects.all():
            z = (b.zip or "").strip()
            if not z or z == "00000":
                continue
            if not (b.latitude and b.longitude):
                continue
            try:
                lat = float(b.latitude)
                lon = float(b.longitude)
            except (TypeError, ValueError):
                continue
            if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                continue
            known_zip_points.append((lat, lon, z))
            by_cell[cell_key(lat, lon)].append((lat, lon, z))

        if known_zip_points:
            for b in Bathroom.objects.all():
                z = (b.zip or "").strip()
                if z and z != "00000":
                    continue
                if not (b.latitude and b.longitude):
                    continue
                try:
                    lat = float(b.latitude)
                    lon = float(b.longitude)
                except (TypeError, ValueError):
                    continue
                if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                    continue

                cx, cy = cell_key(lat, lon)
                best = None
                # Expand search from local grid cell outward until we find candidates.
                for r in range(0, 12):
                    found_any = False
                    for dx in range(-r, r + 1):
                        for dy in range(-r, r + 1):
                            if r and abs(dx) != r and abs(dy) != r:
                                continue
                            pts = by_cell.get((cx + dx, cy + dy), [])
                            for plat, plon, pzip in pts:
                                found_any = True
                                d2 = (lat - plat) ** 2 + (lon - plon) ** 2
                                if best is None or d2 < best[0]:
                                    best = (d2, pzip)
                    if found_any and best is not None:
                        break

                # Fallback global nearest if bucket search found nothing.
                if best is None:
                    for plat, plon, pzip in known_zip_points:
                        d2 = (lat - plat) ** 2 + (lon - plon) ** 2
                        if best is None or d2 < best[0]:
                            best = (d2, pzip)

                if best and best[1]:
                    if not dry_run:
                        b.zip = best[1]
                        b.save(update_fields=["zip"])
                    zip_fixed += 1

        self.stdout.write("Placeholder zip fixed: {} records updated".format(zip_fixed))

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
        # Speed optimization:
        # - Fetch at most once per rounded coordinate cluster (5 decimals)
        # - If any record in the cluster already has good hours, copy it to the others
        hours_fetched = 0
        if not options.get("skip_hours_fetch", False):
            coord_groups = defaultdict(list)
            for b in Bathroom.objects.exclude(latitude__isnull=True).exclude(
                longitude__isnull=True
            ):
                try:
                    lat = float(b.latitude)
                    lon = float(b.longitude)
                except (ValueError, TypeError):
                    continue
                if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                    continue
                key = (round(lat, 5), round(lon, 5))
                coord_groups[key].append(b)

            to_fetch_keys = []
            total_groups = len(coord_groups)
            filled_from_existing = 0

            for key, group in coord_groups.items():
                good_hours = None
                for r in group:
                    if r.hours and str(r.hours).strip() and not is_bogus_hours(r.hours):
                        good_hours = str(r.hours).strip()
                        break

                if good_hours:
                    for r in group:
                        if (not r.hours) or (not str(r.hours).strip()) or is_bogus_hours(r.hours):
                            if not dry_run:
                                r.hours = good_hours
                                r.save(update_fields=["hours"])
                            hours_fetched += 1
                    filled_from_existing += 1
                else:
                    to_fetch_keys.append(key)

            self.stdout.write(
                "OSM hours fetch coordinate groups: {}/{} (progress every {} groups)".format(
                    len(to_fetch_keys), total_groups, progress_every
                )
            )
            if filled_from_existing and not dry_run:
                self.stdout.write(
                    "Filled missing/bogus hours from existing cluster hours."
                )

            for idx, key in enumerate(to_fetch_keys, start=1):
                lat, lon = key[0], key[1]
                hrs = fetch_hours_from_osm(
                    lat, lon, radius_m=radius_m, timeout_s=timeout_s
                )
                if hrs:
                    for r in coord_groups[key]:
                        if (not r.hours) or (not str(r.hours).strip()) or is_bogus_hours(r.hours):
                            if not dry_run:
                                r.hours = hrs
                                r.save(update_fields=["hours"])
                            hours_fetched += 1

                if idx % progress_every == 0 or idx == len(to_fetch_keys):
                    elapsed = time.time() - started_at
                    self.stdout.write(
                        "Fetching OSM hours groups: {}/{} done ({} filled, elapsed {:.1f}s)".format(
                            idx, len(to_fetch_keys), hours_fetched, elapsed
                        )
                    )
                    try:
                        self.stdout.flush()
                    except Exception:
                        pass

                time.sleep(sleep_seconds)
        self.stdout.write("Hours fetched from OSM: {} records".format(hours_fetched))

        # 6. Deduplicate by normalized address (same full address string)
        addr_groups = defaultdict(list)
        for b in Bathroom.objects.all():
            key = normalize_address_for_dedup(b.address)
            if not key:
                continue
            addr_groups[key].append(b)

        deleted_by_address = 0
        for key, group in addr_groups.items():
            if len(group) <= 1:
                continue
            group.sort(key=bathroom_richness_score, reverse=True)
            for dup in group[1:]:
                if not dry_run:
                    dup.delete()
                deleted_by_address += 1

        self.stdout.write(
            "Duplicates removed (same address): {} records".format(deleted_by_address)
        )

        # 7. Deduplicate by lat/lon proximity (same building, different address strings)
        deleted_proximity = 0
        if not options.get("skip_proximity_dedup"):
            proximity_m = float(options.get("proximity_radius_m") or 55)
            points = []
            proximity_records = []
            for b in Bathroom.objects.all():
                if b.latitude is None or b.longitude is None:
                    continue
                try:
                    lat = float(b.latitude)
                    lon = float(b.longitude)
                except (TypeError, ValueError):
                    continue
                if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                    continue
                points.append((lat, lon))
                proximity_records.append(b)

            for cluster in cluster_indices_proximity(points, proximity_m):
                if len(cluster) <= 1:
                    continue
                group = [proximity_records[i] for i in cluster]
                group.sort(key=bathroom_richness_score, reverse=True)
                for dup in group[1:]:
                    if not dry_run:
                        dup.delete()
                    deleted_proximity += 1

            self.stdout.write(
                "Duplicates removed (within {:.0f} m): {} records".format(
                    proximity_m, deleted_proximity
                )
            )
        else:
            self.stdout.write("Proximity dedup skipped")

        # 8. Deduplicate by (lat, lon) rounded to 5 decimals (~1m)
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
            group.sort(key=bathroom_richness_score, reverse=True)
            for dup in group[1:]:
                if not dry_run:
                    dup.delete()
                deleted += 1

        self.stdout.write("Duplicates removed (same coordinates): {} records".format(deleted))

        total = Bathroom.objects.count()
        self.stdout.write(
            self.style.SUCCESS("\nDone. {} bathroom locations remain.".format(total))
        )
