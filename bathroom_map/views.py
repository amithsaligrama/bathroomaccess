import json

from django.core.serializers import serialize
from django.shortcuts import render, redirect
from geopy.geocoders import Nominatim
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

    return render(request, "map.html", {
        'markers': json.loads(serialize("json", Bathroom.objects.all())),
        'latitude': latitude,
        'longitude': longitude
    })