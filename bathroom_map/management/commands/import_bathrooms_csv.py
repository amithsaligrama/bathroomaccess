import csv
import io
import json
import math
import re
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from geopy.geocoders import Nominatim

from bathroom_map.models import Bathroom


def _normalize_row(row):
    n = {}
    for key, value in row.items():
        k = (key or "").strip().lower()
        if value is None:
            n[k] = ""
        elif isinstance(value, str):
            n[k] = value.strip()
        else:
            n[k] = str(value).strip()
    if n.get("zip_code") and not n.get("zip"):
        n["zip"] = n["zip_code"]
    if n.get("postal") and not n.get("zip"):
        n["zip"] = n["postal"]
    if n.get("postal_code") and not n.get("zip"):
        n["zip"] = n["postal_code"]
    return n


def _normalize_zip_code(z):
    if z is None:
        return ""
    zip_code = str(z).strip()
    if not zip_code:
        return ""
    if "-" in zip_code:
        zip_code = zip_code.split("-")[0]
    if len(zip_code) >= 5 and zip_code[:5].isdigit():
        return zip_code[:5]
    if len(zip_code) == 5 and zip_code.isdigit():
        return zip_code
    return ""


def _parse_lat_long(row, errors, row_index):
    latitude = None
    longitude = None
    lat_raw = (row.get("latitude") or "").strip()
    lon_raw = (row.get("longitude") or row.get("longitud") or "").strip()

    if lat_raw:
        try:
            latitude = Decimal(lat_raw)
        except InvalidOperation:
            errors.append(f"Row {row_index}: invalid latitude '{lat_raw}'.")
    if lon_raw:
        try:
            longitude = Decimal(lon_raw)
        except InvalidOperation:
            errors.append(f"Row {row_index}: invalid longitude '{lon_raw}'.")
    if latitude is not None and longitude is not None:
        latitude = Decimal(str(round(float(latitude), 6)))
        longitude = Decimal(str(round(float(longitude), 6)))
    return latitude, longitude


def _parse_point_wkt(point_str):
    if not point_str:
        return None, None
    m = re.search(
        r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)",
        str(point_str).strip(),
        re.I,
    )
    if not m:
        return None, None
    try:
        lon_f, lat_f = float(m.group(1)), float(m.group(2))
        if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
            return None, None
        if lat_f < -90 or lat_f > 90 or lon_f < -180 or lon_f > 180:
            return None, None
        return (
            Decimal(str(round(lat_f, 6))),
            Decimal(str(round(lon_f, 6))),
        )
    except (ValueError, TypeError):
        return None, None


def _is_bogus_hours(hours):
    if not hours or not hours.strip():
        return True
    s = hours.strip()
    if re.match(r"^[\d\s,\.]+$", s):
        return True
    if len(s) <= 4 and s.replace(".", "").isdigit():
        return True
    return False


def _fmt_time_hm(t):
    t = (t or "").strip()
    if not t:
        return ""
    parts = t.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return t


def _hours_from_notes_json(notes):
    if "{" not in notes or "}" not in notes:
        return ""
    try:
        data = json.loads(notes)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(data, dict):
        return "; ".join(f"{k}: {v}" for k, v in sorted(data.items()))
    return ""


def _build_hours_from_row(row):
    parts = []
    raw_hours = (row.get("hours") or "").strip()
    if raw_hours and not _is_bogus_hours(raw_hours):
        parts.append(raw_hours)

    op_hours = (row.get("hours of operation") or row.get("hours_of_operation") or "").strip()
    if op_hours and not _is_bogus_hours(op_hours) and op_hours not in parts:
        parts.append(op_hours)

    open_season = (row.get("open") or "").strip()
    if open_season and open_season.lower() not in ("future", "closed", "closed for construction"):
        if open_season not in parts:
            parts.append(open_season)

    open_h = (row.get("public_access_hours_open") or "").strip()
    close_h = (row.get("public_access_hours_close") or "").strip()
    days = (row.get("public_access_days") or "").strip()
    if open_h and close_h:
        t_open = _fmt_time_hm(open_h)
        t_close = _fmt_time_hm(close_h)
        line = f"{days} {t_open}–{t_close}" if days else f"{t_open}–{t_close}"
        if line not in parts:
            parts.append(line)

    notes = (row.get("notes") or "").strip()
    if notes:
        jh = _hours_from_notes_json(notes)
        if jh and jh not in parts:
            parts.append(jh)

    return "; ".join(parts)


def _build_place_zip_index(normalized_rows):
    place_to_zips = defaultdict(set)
    for nr in normalized_rows:
        z = _normalize_zip_code(nr.get("zip") or "")
        if not z:
            continue
        park = (nr.get("park") or "").strip().lower()
        if park:
            place_to_zips[("park", park)].add(z)
        neigh = (nr.get("analysis_neighborhood") or "").strip().lower()
        if neigh:
            place_to_zips[("neighborhood", neigh)].add(z)
    return place_to_zips


def _zip_from_same_place(nr, place_to_zips):
    park = (nr.get("park") or "").strip().lower()
    if park and place_to_zips.get(("park", park)):
        return sorted(place_to_zips[("park", park)])[0]
    neigh = (nr.get("analysis_neighborhood") or "").strip().lower()
    if neigh and place_to_zips.get(("neighborhood", neigh)):
        return sorted(place_to_zips[("neighborhood", neigh)])[0]
    return ""


def _reverse_geocode_zip_cached(
    geocoder, latitude, longitude, cache, round_decimals=4, sleep_s=0.0
):
    try:
        key = (round(float(latitude), round_decimals), round(float(longitude), round_decimals))
    except Exception:
        key = None
    if key is not None and key in cache:
        return cache[key]
    try:
        loc = geocoder.reverse((float(latitude), float(longitude)), language="en", timeout=10)
        addr = (loc.raw or {}).get("address") or {}
        pc = addr.get("postcode") or ""
        z = _normalize_zip_code(pc)
    except Exception:
        z = ""
    if sleep_s:
        time.sleep(sleep_s)
    if key is not None:
        cache[key] = z
    return z


def _build_remarks_from_row(row, access_raw):
    chunks = []
    for label, key in (
        ("Park", "park"),
        ("Source", "source"),
        ("Neighborhood", "analysis_neighborhood"),
        ("Supervisor district", "supervisor_district"),
        ("Location type", "location type"),
        ("Operator", "operator"),
        ("Status", "status"),
        ("Restroom type", "restroom type"),
        ("Changing stations", "changing stations"),
        ("Website", "website"),
        # Generic restroom datasets (example: LA "Restroom_*.csv")
        ("Level", "level"),
        ("Gender", "gender"),
        ("Toilets", "toilets"),
        ("Urinals", "urinals"),
        ("Faucets", "faucets"),
        ("Dryer tower", "dryertower"),
        ("Soap dispenser", "soap_disp"),
        ("Year built", "year_built"),
        ("Maintenance date", "maint_date"),
        ("Janitor", "janitor"),
    ):
        v = (row.get(key) or "").strip()
        if v:
            chunks.append(f"{label}: {v}")
    extra_notes = (row.get("additional notes") or row.get("additional_notes") or "").strip()
    if extra_notes:
        chunks.append(f"Additional notes: {extra_notes}")
    notes = (row.get("notes") or "").strip()
    if notes:
        chunks.append(f"Notes: {notes}")
    if access_raw:
        chunks.append(f"Access: {access_raw}")
    return "\n".join(chunks)


class Command(BaseCommand):
    help = "Import bathrooms from a CSV filepath (same mapping as admin import)"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to CSV file")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report without creating records")
        parser.add_argument("--limit", type=int, default=0, help="Only import first N matching rows")
        parser.add_argument(
            "--skip-reverse-zip",
            action="store_true",
            help="Do not reverse-geocode missing zip codes (faster, but rows without zip may be skipped)",
        )
        parser.add_argument(
            "--reverse-zip-sleep",
            type=float,
            default=0.0,
            help="Sleep seconds between reverse-zip requests (be nice to Nominatim)",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        dry_run = options["dry_run"]
        limit = int(options["limit"] or 0)
        skip_reverse_zip = options["skip_reverse_zip"]
        reverse_zip_sleep = float(options["reverse_zip_sleep"] or 0.0)

        with open(csv_path, "rb") as f:
            raw = f.read()
        decoded = None
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                decoded = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ValueError("Could not decode CSV. Save as UTF-8.")

        reader = csv.DictReader(io.StringIO(decoded))
        header = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
        has_resource_type = "resource_type" in header
        is_nyc_csv = (
            "facility name" in header
            or "changing stations" in header
            or "hours of operation" in header
        )
        is_the_geom_csv = "the_geom" in header or "geom" in header or "geometry" in header
        has_toilets_col = "toilets" in header
        rows_raw = list(reader)

        geocoder = Nominatim(user_agent="bathroom_map_3")
        errors = []
        created = 0

        candidates = []
        for row_index, row in enumerate(rows_raw, start=2):
            nr = _normalize_row(row)
            if has_resource_type:
                rt = (nr.get("resource_type") or "").strip().lower()
                if rt != "restroom":
                    continue
            if has_toilets_col:
                try:
                    toilets_cnt = int((nr.get("toilets") or "0").strip() or "0")
                except Exception:
                    toilets_cnt = 0
                if toilets_cnt <= 0:
                    continue
            candidates.append((row_index, nr))
            if limit and len(candidates) >= limit:
                break

        place_to_zips = _build_place_zip_index([nr for _, nr in candidates])
        reverse_zip_cache = {}
        reverse_zip_cache_round_decimals = (
            1 if is_nyc_csv else (2 if is_the_geom_csv else 4)
        )

        for row_index, nr in candidates:
            name = (nr.get("name") or nr.get("facility") or nr.get("facility name") or nr.get("facility_name") or nr.get("libname") or "").strip()
            if not name:
                name = (nr.get("site_name") or "").strip()

            address = (nr.get("address") or nr.get("street address") or "").strip()
            city = (nr.get("city") or "").strip()
            state = (nr.get("state") or "").strip()
            if city and address:
                address = f"{address}, {city}"
            elif city and not address:
                address = city
            if state and address and state.upper() not in address.upper():
                address = f"{address}, {state}"

            if is_the_geom_csv and not address:
                address = (name or "").strip()

            zip_code = _normalize_zip_code(nr.get("zip") or "")
            if not zip_code:
                zip_code = _zip_from_same_place(nr, place_to_zips)

            latitude, longitude = _parse_lat_long(nr, errors, row_index)
            point_raw = (nr.get("point") or nr.get("location") or nr.get("the_geom") or nr.get("geom") or "").strip()
            if (latitude is None or longitude is None) and point_raw:
                plat, plon = _parse_point_wkt(point_raw)
                if plat is not None and plon is not None:
                    latitude, longitude = plat, plon

            if latitude is None or longitude is None:
                errors.append(f"Row {row_index}: missing coordinates (lat/lon or POINT).")
                continue

            if not zip_code and not skip_reverse_zip:
                zip_code = _reverse_geocode_zip_cached(
                    geocoder,
                    latitude,
                    longitude,
                    reverse_zip_cache,
                    round_decimals=reverse_zip_cache_round_decimals,
                    sleep_s=reverse_zip_sleep,
                )

            if not zip_code:
                # If reverse geocoding fails/rate-limits, still import the row.
                # NYC rows get a NYC placeholder zip for cleaner display.
                zip_code = "10001" if is_nyc_csv else "00000"

            # Prefer showing a real location label instead of a generic fallback.
            if not (address or "").strip() and (name or "").strip():
                address = (name or "").strip()
            if not (address or "").strip():
                address = "View on Google Maps"

            hours = _build_hours_from_row(nr)
            access_raw = (nr.get("access") or nr.get("accessibility") or "").strip()
            remarks = _build_remarks_from_row(nr, access_raw)
            user_remarks = (nr.get("remarks") or "").strip()
            if user_remarks:
                remarks = f"{user_remarks}\n\n{remarks}" if remarks else user_remarks

            if dry_run:
                created += 1
                continue

            Bathroom.objects.create(
                name=name,
                address=address,
                zip=zip_code,
                latitude=latitude,
                longitude=longitude,
                hours=hours,
                remarks=remarks,
            )
            created += 1

        self.stdout.write(f"Imported: {created}")
        if errors:
            self.stdout.write(f"Row errors: {len(errors)}")
            self.stdout.write("First errors: " + "; ".join(errors[:10]))

