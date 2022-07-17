from django.contrib import admin
from .models import Bathroom
from geopy.geocoders import Nominatim

@admin.register(Bathroom)
class BathroomAdmin(admin.ModelAdmin):
    list_display = ("name", "address", "zip", "hours", "remarks")
    
    def save_model(self, request, obj, form, change):
        geocoder = Nominatim(user_agent = 'bathroom_map')
        location = geocoder.geocode(obj.address + ", " + obj.zip)
        print(location.latitude)
        if not (obj.latitude and obj.longitude):
            try:
                obj.latitude, obj.longitude = location.latitude, location.longitude
            except:
                pass
        super().save_model(request, obj, form, change)