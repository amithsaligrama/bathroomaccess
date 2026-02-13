import json
import time
import urllib.parse
import urllib.request

from django.core.serializers import serialize
from django.http import JsonResponse
from django.shortcuts import render, redirect
# from django.contrib.gis.geoip2 import GeoIP2
from geopy.geocoders import Nominatim
from geopy.distance import distance
from .models import Bathroom
from .utils import (
    ensure_state_in_address,
    city_slug,
    parse_city_slug,
    parse_city_state_from_address,
)

# Cache for place search: {("city", "ST"): (lat, lon)}, refreshed every 5 min
_places_cache = None
_places_cache_time = 0
_PLACES_CACHE_TTL = 300


def _build_places_index():
    """Build index of unique (city, state) -> (lat, lon) from bathroom records."""
    global _places_cache, _places_cache_time
    now = time.time()
    if _places_cache is not None and (now - _places_cache_time) < _PLACES_CACHE_TTL:
        return _places_cache
    places = {}
    for b in Bathroom.objects.exclude(latitude__isnull=True).exclude(longitude__isnull=True):
        city, state = parse_city_state_from_address(b.address or "", b.zip)
        if not city:
            continue
        try:
            lat, lon = float(b.latitude), float(b.longitude)
        except (ValueError, TypeError):
            continue
        key = (city.strip(), (state or "").strip())
        if key not in places:
            places[key] = [lat, lon, 1]
        else:
            places[key][0] += lat
            places[key][1] += lon
            places[key][2] += 1
    # Average coords per place; also build slug->(lat,lon) for city param lookup
    result = {}
    slugs = {}
    for (city, state), (lat_sum, lon_sum, count) in places.items():
        display = "{}, {}".format(city, state) if state else city
        lat, lon = lat_sum / count, lon_sum / count
        result[display] = (lat, lon)
        if state:
            slug = city_slug(city, state)
            if slug:
                slugs[slug] = (lat, lon)
    _places_cache = (result, slugs)
    _places_cache_time = now
    return (result, slugs)


def place_search_view(request):
    """Search only towns/places that have bathroom locations in our database."""
    q = (request.GET.get("q") or "").strip().lower()
    if len(q) < 2:
        return JsonResponse({"results": []})
    places_data = _build_places_index()
    places = places_data[0] if isinstance(places_data, tuple) else places_data
    slugs = places_data[1] if isinstance(places_data, tuple) and len(places_data) > 1 else {}
    results = []
    for display_name, (lat, lon) in places.items():
        if q in display_name.lower():
            slug = None
            if "," in display_name:
                city_part, state_part = display_name.rsplit(",", 1)
                slug = city_slug(city_part.strip(), state_part.strip())
            if not slug and (lat, lon) in [v for v in slugs.values()]:
                for s, coords in slugs.items():
                    if coords == (lat, lon):
                        slug = s
                        break
            results.append({
                "display_name": display_name,
                "lat": lat,
                "lon": lon,
                "city": slug,
            })
    results.sort(key=lambda x: (x["display_name"].lower().startswith(q), x["display_name"].lower()))
    return JsonResponse({"results": results[:12]})


def bathrooms_view(request):
    latitude, longitude = None, None
    if request.GET.get("latitude") and request.GET.get("longitude"):
        try:
            latitude = float(request.GET["latitude"])
            longitude = float(request.GET["longitude"])
        except (ValueError, TypeError):
            pass
    elif "city" in request.GET:
        city_param = request.GET["city"].strip().lower().replace("%20", " ")
        places_data = _build_places_index()
        slugs = places_data[1] if isinstance(places_data, tuple) and len(places_data) > 1 else {}
        if city_param in slugs:
            latitude, longitude = slugs[city_param]
        else:
            city, state = parse_city_slug(city_param)
            if city and state:
                geocoder = Nominatim(user_agent="bathroom_map_3")
                location = geocoder.geocode(city + ", " + state)
                if location:
                    latitude, longitude = location.latitude, location.longitude
    
    if latitude is not None and longitude is not None:
        center = (latitude, longitude)
        max_markers = 2000
        ret_ordered = []
        for marker in Bathroom.objects.all():
            if marker.latitude is None or marker.longitude is None:
                continue
            dist = distance(center, (marker.latitude, marker.longitude))
            addr = ensure_state_in_address(marker.address or "", marker.zip or "") or marker.address
            ret_ordered.append({
                'name': marker.name,
                'address': addr,
                'zip': marker.zip,
                'latitude': float(marker.latitude),
                'longitude': float(marker.longitude),
                'hours': marker.hours or '',
                'remarks': marker.remarks or '',
                'dist': dist.miles
            })
        ret_ordered.sort(key=lambda x: x['dist'])
        markers = ret_ordered[:max_markers]
    else:
        markers = []

    return render(request, "map.html", {
        'markers': markers,
        'latitude': latitude,
        'longitude': longitude
    })

def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

def markers_json_view(request):
    """Return markers for the map. Supports:
    - lat/lon: return up to 2000 nearest markers to center (for initial load).
    - sw_lat, sw_lon, ne_lat, ne_lon: return ALL markers within bounding box (for zoomed-out viewport)."""
    # Bounds-based: return all markers in visible viewport (used when zooming out)
    sw_lat = request.GET.get("sw_lat")
    sw_lon = request.GET.get("sw_lon")
    ne_lat = request.GET.get("ne_lat")
    ne_lon = request.GET.get("ne_lon")
    if all(v is not None and v != "" for v in (sw_lat, sw_lon, ne_lat, ne_lon)):
        try:
            sw_lat = float(sw_lat)
            sw_lon = float(sw_lon)
            ne_lat = float(ne_lat)
            ne_lon = float(ne_lon)
        except (ValueError, TypeError):
            pass
        else:
            lat_min, lat_max = min(sw_lat, ne_lat), max(sw_lat, ne_lat)
            lon_min, lon_max = min(sw_lon, ne_lon), max(sw_lon, ne_lon)
            qs = Bathroom.objects.filter(
                latitude__gte=lat_min, latitude__lte=lat_max,
                longitude__gte=lon_min, longitude__lte=lon_max
            ).exclude(latitude__isnull=True).exclude(longitude__isnull=True)
            max_bounds = 25000  # allow full nationwide dataset when zoomed out
            ret = []
            for m in qs[:max_bounds]:
                try:
                    mlat, mlon = float(m.latitude), float(m.longitude)
                except (ValueError, TypeError):
                    continue
                if mlat < -90 or mlat > 90 or mlon < -180 or mlon > 180:
                    continue
                addr = ensure_state_in_address(m.address or "", m.zip or "") or m.address or ""
                ret.append({
                    "name": m.name or "",
                    "address": addr,
                    "zip": m.zip or "",
                    "latitude": mlat,
                    "longitude": mlon,
                    "hours": m.hours or "",
                    "remarks": m.remarks or "",
                })
            return JsonResponse({"markers": ret})

    # Center-based: return nearest 2000 markers (for initial/zoomed-in load)
    try:
        lat = float(request.GET.get("lat", 42.36))
        lon = float(request.GET.get("lon", -71.06))
    except (ValueError, TypeError):
        lat, lon = 42.36, -71.06
    center = (lat, lon)
    max_markers = 2000
    ret = []
    for m in Bathroom.objects.exclude(latitude__isnull=True).exclude(longitude__isnull=True):
        try:
            mlat, mlon = float(m.latitude), float(m.longitude)
        except (ValueError, TypeError):
            continue
        if mlat < -90 or mlat > 90 or mlon < -180 or mlon > 180:
            continue
        addr = ensure_state_in_address(m.address or "", m.zip or "") or m.address or ""
        ret.append({
            "name": m.name or "",
            "address": addr,
            "zip": m.zip or "",
            "latitude": mlat,
            "longitude": mlon,
            "hours": m.hours or "",
            "remarks": m.remarks or "",
        })
    ret.sort(key=lambda x: distance(center, (x["latitude"], x["longitude"])).miles)
    return JsonResponse({"markers": ret[:max_markers]})


def bathrooms_order_by_distance_view(request):
    user_lat = request.GET['latitude']
    user_long = request.GET['longitude']
    # if not (user_lat and user_long):
    #     g = GeoIP2()
    #     user_imprecise_loc = g.city(get_client_ip(request))
    #     user_lat = user_imprecise_loc['latitude']
    #     user_long = user_imprecise_loc['longitude']

    ret_ordered = []
    for marker in Bathroom.objects.all():
        dist = distance((float(user_lat), float(user_long)), (marker.latitude, marker.longitude))
        addr_display = ensure_state_in_address(marker.address or "", marker.zip or "")
        marker_dict = {
            'name': marker.name,
            'address': addr_display or marker.address,
            'zip': marker.zip,
            'latitude': float(marker.latitude),
            'longitude': float(marker.longitude),
            'hours': marker.hours,
            'remarks': marker.remarks,
            'dist': dist.miles
        }
        ret_ordered.append(marker_dict)

    ret_ordered.sort(key=lambda x: x['dist'])

    return render(request, "list.html", {
        'markers': ret_ordered
    })
