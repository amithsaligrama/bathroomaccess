import csv
import io
import math
import os
from collections import defaultdict
import re
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.core.management import call_command
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import path
from geopy.geocoders import Nominatim
import shapefile

from .management.commands import import_bathrooms_csv as bath_csv_import
from .models import Bathroom


class BathroomCsvImportForm(forms.Form):
    csv_file = forms.FileField()


class BathroomShapefileImportForm(forms.Form):
    zip_file = forms.FileField(
        help_text="ZIP file containing the Shapefile (.shp, .shx, .dbf, etc.)",
    )

@admin.register(Bathroom)
class BathroomAdmin(admin.ModelAdmin):
    list_display = ("name", "address", "zip", "hours", "remarks")
    search_fields = ("name", "address", "zip", "hours", "remarks")
    change_list_template = "admin/bathroom_change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv),
                name="bathroom_import_csv",
            ),
            path(
                "import-shapefile/",
                self.admin_site.admin_view(self.import_shapefile),
                name="bathroom_import_shapefile",
            ),
            path(
                "clean-bathrooms/",
                self.admin_site.admin_view(self.clean_bathrooms),
                name="bathroom_clean",
            ),
        ]
        return custom_urls + urls

    def import_csv(self, request):
        if request.method == "POST":
            form = BathroomCsvImportForm(request.POST, request.FILES)
            if form.is_valid():
                csv_file = form.cleaned_data["csv_file"]
                raw_bytes = csv_file.read()
                decoded = None
                for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                    try:
                        decoded = raw_bytes.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                if decoded is None:
                    messages.error(
                        request,
                        "Could not decode CSV. Try saving as UTF-8 in Excel or another editor.",
                    )
                    return redirect("..")

                reader = csv.DictReader(io.StringIO(decoded, newline=""))
                fieldnames_raw = reader.fieldnames or []
                header = [field.strip().lower() for field in fieldnames_raw]
                has_resource_type = "resource_type" in header
                is_nyc_csv = "facility name" in header or "changing stations" in header or "hours of operation" in header
                is_the_geom_csv = "the_geom" in header or "geom" in header or "geometry" in header
                has_toilets_col = "toilets" in header

                rows_raw = list(reader)
                geocoder = Nominatim(user_agent="bathroom_map_3")
                created_count = 0
                errors = []

                candidates = []
                for row_index, row in enumerate(rows_raw, start=2):
                    normalized_row = self._normalize_row(row)
                    if has_resource_type:
                        rt = (normalized_row.get("resource_type") or "").strip().lower()
                        if rt != "restroom":
                            continue
                    if has_toilets_col:
                        try:
                            toilets_cnt = int((normalized_row.get("toilets") or "0").strip() or "0")
                        except Exception:
                            toilets_cnt = 0
                        if toilets_cnt <= 0:
                            continue
                    if is_nyc_csv:
                        st = (normalized_row.get("status") or "").strip().lower()
                        if st != "operational":
                            continue
                    candidates.append((row_index, normalized_row))

                place_to_zips = self._build_place_zip_index(
                    [nr for _, nr in candidates]
                )
                reverse_zip_cache = {}
                reverse_zip_cache_round_decimals = (
                    1 if is_nyc_csv else (2 if is_the_geom_csv else 4)
                )

                for row_index, normalized_row in candidates:
                    name = bath_csv_import._row_display_name(normalized_row)
                    address = (
                        normalized_row.get("address")
                        or normalized_row.get("street address")
                        or ""
                    ).strip()
                    city = (normalized_row.get("city") or "").strip()
                    state = (normalized_row.get("state") or "").strip()
                    if city and address:
                        address = "{}, {}".format(address, city)
                    elif city and not address:
                        address = city
                    if state and address and state.upper() not in address.upper():
                        address = "{}, {}".format(address, state)

                    zip_code = self._normalize_zip_code(
                        normalized_row.get("zip") or ""
                    )
                    if not zip_code:
                        zip_code = self._zip_from_same_place(
                            normalized_row, place_to_zips
                        )

                    if is_the_geom_csv and not address:
                        # Some datasets only provide a facility/address label plus POINT geometry.
                        address = (name or "").strip()

                    latitude, longitude = self._parse_lat_long(
                        normalized_row, row_index, errors
                    )
                    point_raw = (
                        normalized_row.get("point")
                        or normalized_row.get("location")
                        or normalized_row.get("the_geom")
                        or normalized_row.get("geom")
                        or ""
                    ).strip()
                    if (latitude is None or longitude is None) and point_raw:
                        plat, plon = self._parse_point_wkt(
                            point_raw
                        )
                        if plat is not None and plon is not None:
                            latitude, longitude = plat, plon

                    if (latitude is None or longitude is None) and address and zip_code:
                        try:
                            location = geocoder.geocode(
                                "{}, {}".format(address, zip_code)
                            )
                            if location:
                                latitude = Decimal(
                                    str(round(float(location.latitude), 6))
                                )
                                longitude = Decimal(
                                    str(round(float(location.longitude), 6))
                                )
                        except Exception:
                            pass

                    if latitude is None or longitude is None:
                        errors.append(
                            "Row {}: need latitude/longitude, point column, "
                            "or geocodable address + zip.".format(row_index)
                        )
                        continue

                    if not zip_code:
                        zip_code = self._reverse_geocode_zip_cached(
                            geocoder,
                            latitude,
                            longitude,
                            reverse_zip_cache,
                            reverse_zip_cache_round_decimals,
                        )

                    if not zip_code:
                        # If reverse geocoding fails/rate-limits, still import the row.
                        # NYC rows get a NYC placeholder zip for cleaner display.
                        zip_code = "10001" if is_nyc_csv else "00000"

                    # Prefer showing a real location label instead of a generic fallback.
                    if not (address or "").strip() and (name or "").strip():
                        address = (name or "").strip()
                    addr_stripped = (address or "").strip()
                    if not addr_stripped:
                        address = "View on Google Maps"
                    if is_nyc_csv and name:
                        address = "{}, New York, NY".format(name)

                    hours = bath_csv_import._build_hours_from_row(
                        normalized_row, is_nyc_csv=is_nyc_csv
                    )
                    access_raw = (
                        normalized_row.get("access")
                        or normalized_row.get("accessibility")
                        or ""
                    ).strip()
                    remarks = bath_csv_import._build_remarks_from_row(
                        normalized_row, access_raw, is_nyc_csv=is_nyc_csv
                    )
                    user_remarks = (normalized_row.get("remarks") or "").strip()
                    if user_remarks:
                        remarks = (
                            "{}\n\n{}".format(user_remarks, remarks)
                            if remarks
                            else user_remarks
                        )

                    Bathroom.objects.create(
                        name=name,
                        address=address,
                        zip=zip_code,
                        latitude=latitude,
                        longitude=longitude,
                        hours=hours,
                        remarks=remarks,
                    )
                    created_count += 1

                if errors:
                    messages.warning(
                        request,
                        "Imported {} bathrooms with {} row errors. "
                        "First errors: {}".format(
                            created_count, len(errors), "; ".join(errors[:5])
                        ),
                    )
                else:
                    messages.success(
                        request, "Imported {} bathrooms.".format(created_count)
                    )

                return redirect("..")
        else:
            form = BathroomCsvImportForm()

        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "title": "Import Bathrooms from CSV",
        }
        return render(request, "admin/bathroom_csv_upload.html", context)

    def import_shapefile(self, request):
        if request.method == "POST":
            form = BathroomShapefileImportForm(request.POST, request.FILES)
            if form.is_valid():
                zip_file = form.cleaned_data["zip_file"]
                if not zip_file.name.lower().endswith(".zip"):
                    messages.error(
                        request,
                        "Please upload a ZIP file containing the Shapefile (.shp, .shx, .dbf).",
                    )
                    return redirect("..")
                try:
                    created_count, errors = self._process_shapefile(zip_file)
                    if errors:
                        messages.warning(
                            request,
                            "Imported {} locations with {} row errors. "
                            "First errors: {}".format(
                                created_count, len(errors), "; ".join(errors[:5])
                            ),
                        )
                    else:
                        messages.success(
                            request,
                            "Imported {} locations from Shapefile.".format(created_count),
                        )
                except Exception as e:
                    messages.error(
                        request,
                        "Shapefile import failed: {}".format(str(e)),
                    )
                return redirect("..")
        else:
            form = BathroomShapefileImportForm()

        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "title": "Import Locations from Shapefile",
        }
        return render(request, "admin/bathroom_shapefile_upload.html", context)

    def clean_bathrooms(self, request):
        from io import StringIO
        out = StringIO()
        fetch_hours = request.GET.get("fetch_hours") == "1"
        call_command(
            "clean_bathrooms",
            skip_hours_fetch=not fetch_hours,
            stdout=out,
        )
        messages.success(request, out.getvalue().replace("\n", " ").strip())
        return HttpResponseRedirect(request.META.get("HTTP_REFERER", ".."))

    def _process_shapefile(self, zip_file):
        zip_file.seek(0)
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(zip_file, "r") as zf:
                zf.extractall(tmpdir)

            shp_path = None
            for f in os.listdir(tmpdir):
                if f.lower().endswith(".shp"):
                    shp_path = os.path.join(tmpdir, f)
                    break

            if not shp_path:
                raise ValueError(
                    "No .shp file found in the ZIP. "
                    "ZIP the .shp, .shx, and .dbf files together."
                )

            transformer = None
            prj_path = os.path.splitext(shp_path)[0] + ".prj"
            if os.path.exists(prj_path):
                try:
                    from pyproj import Transformer
                    from pyproj import CRS
                    with open(prj_path, "r") as f:
                        wkt = f.read()
                    crs = CRS.from_wkt(wkt)
                    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
                except Exception:
                    pass

            sf = shapefile.Reader(shp_path)
            fields = [f[0].lower() for f in sf.fields[1:]]

            def get_attr(record, *keys):
                for k in keys:
                    if k in field_idx:
                        val = record[field_idx[k]]
                        if val is not None and str(val).strip():
                            return str(val).strip()
                return ""

            field_idx = {f.lower(): i for i, f in enumerate(fields)}
            name_keys = ("name", "town", "facility", "site_name", "label", "title")
            addr_keys = ("address", "addr", "street", "full_addr", "location")
            zip_keys = ("zip", "zipcode", "zip_code", "postal")
            city_keys = ("city", "town", "municipality")

            created_count = 0
            errors = []

            for i, (shape, record) in enumerate(zip(sf.shapes(), sf.records())):
                if shape.shapeType not in (
                    shapefile.POINT,
                    shapefile.POINTZ,
                    shapefile.POINTM,
                ):
                    errors.append("Row {}: not a point (skipped).".format(i + 1))
                    continue
                if not shape.points:
                    errors.append("Row {}: empty point (skipped).".format(i + 1))
                    continue

                try:
                    x, y = shape.points[0][0], shape.points[0][1]
                    if x is None or y is None:
                        raise ValueError("Missing coordinates")
                    x_f, y_f = float(x), float(y)
                    if not (math.isfinite(x_f) and math.isfinite(y_f)):
                        raise ValueError("Non-finite coordinates")

                    if transformer is not None:
                        lon_f, lat_f = transformer.transform(x_f, y_f)
                    else:
                        lon_f, lat_f = x_f, y_f

                    if lat_f < -90 or lat_f > 90 or lon_f < -180 or lon_f > 180:
                        raise ValueError(
                            "Coordinates out of range. Include the .prj file in the ZIP "
                            "if the shapefile uses a projected coordinate system."
                        )
                    latitude = Decimal(str(round(lat_f, 6)))
                    longitude = Decimal(str(round(lon_f, 6)))
                except (ValueError, TypeError, IndexError, InvalidOperation) as e:
                    errors.append("Row {}: invalid coordinates (skipped): {}".format(i + 1, e))
                    continue

                name = get_attr(record, *name_keys)
                address = get_attr(record, *addr_keys)
                city = get_attr(record, *city_keys)
                if city and address:
                    address = "{}, {}".format(address, city)
                elif city and not address:
                    address = city
                zip_code = get_attr(record, *zip_keys)
                if len(zip_code) > 5 and zip_code[:5].isdigit():
                    zip_code = zip_code[:5]

                if not address:
                    address = name or "Address unavailable"
                if not zip_code:
                    zip_code = "00000"
                if not name:
                    name = address

                Bathroom.objects.create(
                    name=name,
                    address=address,
                    zip=zip_code,
                    latitude=latitude,
                    longitude=longitude,
                    hours="",
                    remarks="",
                )
                created_count += 1

        return created_count, errors

    def _is_bogus_hours(self, hours):
        if not hours or not hours.strip():
            return True
        s = hours.strip()
        if re.match(r"^[+\-]?[\d\s,\.]+$", s):
            return True
        compact = s.replace(".", "").replace("-", "").replace("+", "")
        if len(s) <= 5 and compact.isdigit():
            return True
        return False

    def _normalize_row(self, row):
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

    def _normalize_zip_code(self, z):
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

    def _build_place_zip_index(self, normalized_rows):
        place_to_zips = defaultdict(set)
        for nr in normalized_rows:
            z = self._normalize_zip_code(nr.get("zip") or "")
            if not z:
                continue
            park = (nr.get("park") or "").strip().lower()
            if park:
                place_to_zips[("park", park)].add(z)
            neigh = (nr.get("analysis_neighborhood") or "").strip().lower()
            if neigh:
                place_to_zips[("neighborhood", neigh)].add(z)
        return place_to_zips

    def _zip_from_same_place(self, nr, place_to_zips):
        park = (nr.get("park") or "").strip().lower()
        if park and place_to_zips.get(("park", park)):
            return sorted(place_to_zips[("park", park)])[0]
        neigh = (nr.get("analysis_neighborhood") or "").strip().lower()
        if neigh and place_to_zips.get(("neighborhood", neigh)):
            return sorted(place_to_zips[("neighborhood", neigh)])[0]
        return ""

    def _parse_point_wkt(self, point_str):
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

    def _reverse_geocode_zip(self, geocoder, latitude, longitude):
        try:
            loc = geocoder.reverse(
                (float(latitude), float(longitude)), language="en", timeout=10
            )
            if not loc or not loc.raw:
                return ""
            addr = loc.raw.get("address") or {}
            pc = addr.get("postcode") or ""
            return self._normalize_zip_code(pc)
        except Exception:
            return ""

    def _reverse_geocode_zip_cached(
        self, geocoder, latitude, longitude, cache, round_decimals=4
    ):
        try:
            key = (
                round(float(latitude), round_decimals),
                round(float(longitude), round_decimals),
            )
        except Exception:
            key = None
        if key is not None and key in cache:
            return cache[key]
        z = self._reverse_geocode_zip(geocoder, latitude, longitude)
        if key is not None:
            cache[key] = z
        return z

    def _fmt_time_hm(self, t):
        t = (t or "").strip()
        if not t:
            return ""
        parts = t.split(":")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return "{}:{}".format(parts[0].zfill(2), parts[1].zfill(2))
        return t

    def _parse_lat_long(self, row, row_index, errors):
        latitude = None
        longitude = None

        lat_raw = (row.get("latitude") or row.get("y") or "").strip()
        lon_raw = (
            row.get("longitude")
            or row.get("longitud")
            or row.get("x")
            or ""
        ).strip()

        if lat_raw:
            try:
                latitude = Decimal(lat_raw)
            except InvalidOperation:
                errors.append(
                    "Row {}: invalid latitude '{}'.".format(row_index, lat_raw)
                )

        if lon_raw:
            try:
                longitude = Decimal(lon_raw)
            except InvalidOperation:
                errors.append(
                    "Row {}: invalid longitude '{}'.".format(row_index, lon_raw)
                )

        if latitude is not None and longitude is not None:
            latitude = Decimal(str(round(float(latitude), 6)))
            longitude = Decimal(str(round(float(longitude), 6)))

        return latitude, longitude
    
    def save_model(self, request, obj, form, change):
        if not (obj.latitude and obj.longitude):
            addr = (obj.address or "").strip()
            if addr and addr != "View on Google Maps":
                geocoder = Nominatim(user_agent="bathroom_map_3")
                try:
                    location = geocoder.geocode("{}, {}".format(addr, obj.zip))
                    if location:
                        obj.latitude = location.latitude
                        obj.longitude = location.longitude
                except Exception:
                    pass
        super().save_model(request, obj, form, change)
