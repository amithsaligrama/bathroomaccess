import csv
import io
import math
import os
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

                reader = csv.DictReader(io.StringIO(decoded))
                header = [
                    field.strip().lower() for field in (reader.fieldnames or [])
                ]
                required = {"address", "zip"}
                missing = required - set(header)
                if missing:
                    messages.error(
                        request,
                        "Missing required columns: {}.".format(", ".join(sorted(missing))),
                    )
                    return redirect("..")

                geocoder = Nominatim(user_agent="bathroom_map_3")
                created_count = 0
                errors = []

                for row_index, row in enumerate(reader, start=2):
                    normalized_row = self._normalize_row(row)
                    name = (
                        normalized_row.get("name")
                        or normalized_row.get("libname")
                        or ""
                    ).strip()
                    address = (normalized_row.get("address") or "").strip()
                    city = (normalized_row.get("city") or "").strip()
                    if city and address:
                        address = "{}, {}".format(address, city)
                    zip_code = (normalized_row.get("zip") or "").strip()
                    if len(zip_code) > 5 and zip_code[:5].isdigit():
                        zip_code = zip_code[:5]
                    hours_raw = (
                        normalized_row.get("hours") or ""
                    ).strip()
                    if hours_raw and not self._is_bogus_hours(hours_raw):
                        hours = hours_raw
                    else:
                        hours = ""
                    remarks = (normalized_row.get("remarks") or "").strip()

                    if not address or not zip_code:
                        errors.append(
                            "Row {}: address and zip are required.".format(row_index)
                        )
                        continue

                    latitude, longitude = self._parse_lat_long(
                        normalized_row, row_index, errors
                    )
                    if not latitude or not longitude:
                        try:
                            location = geocoder.geocode(
                                "{}, {}".format(address, zip_code)
                            )
                            if location:
                                latitude, longitude = (
                                    Decimal(str(location.latitude)),
                                    Decimal(str(location.longitude)),
                                )
                        except Exception:
                            pass

                    Bathroom.objects.create(
                        name=name,
                        address=address,
                        zip=zip_code,
                        latitude=latitude or Decimal("0"),
                        longitude=longitude or Decimal("0"),
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
        if re.match(r"^[\d\s,\.]+$", s):
            return True
        if len(s) <= 4 and s.replace(".", "").isdigit():
            return True
        return False

    def _normalize_row(self, row):
        return {
            (key or "").strip().lower(): value
            for key, value in row.items()
        }

    def _parse_lat_long(self, row, row_index, errors):
        latitude = None
        longitude = None

        lat_raw = (row.get("latitude") or "").strip()
        lon_raw = (row.get("longitude") or row.get("longitud") or "").strip()

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

        return latitude, longitude
    
    def save_model(self, request, obj, form, change):
        geocoder = Nominatim(user_agent='bathroom_map_3')
        location = geocoder.geocode(obj.address + ", " + obj.zip)
        if not (obj.latitude and obj.longitude):
            try:
                obj.latitude, obj.longitude = location.latitude, location.longitude
            except:
                pass
        super().save_model(request, obj, form, change)
