import json

from django.core.serializers import serialize
from django.shortcuts import render, redirect
# from django.contrib.gis.geoip2 import GeoIP2
from geopy.geocoders import Nominatim
from geopy.distance import distance
from .models import Bathroom

def bathrooms_view(request):
    if 'city' in request.GET:
        state_and_city = request.GET['city'].split('-')
        city, state = state_and_city[0], state_and_city[1]
        geocoder = Nominatim(user_agent = 'bathroom_map')
        location = geocoder.geocode(city + ', ' + state)
        latitude, longitude = location.latitude, location.longitude
    else:
        latitude, longitude = None, None
    
    ret_ordered = []
    for marker in Bathroom.objects.all():
        dist = distance((latitude, longitude), (marker.latitude, marker.longitude))
        marker_dict = {
            'name': marker.name,
            'address': marker.address,
            'zip': marker.zip,
            'latitude': float(marker.latitude),
            'longitude': float(marker.longitude),
            'hours': marker.hours,
            'remarks': marker.remarks,
            'dist': dist.miles
        }
        ret_ordered.append(marker_dict)


    return render(request, "map.html", {
        'markers': ret_ordered,
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
        marker_dict = {
            'name': marker.name,
            'address': marker.address,
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
