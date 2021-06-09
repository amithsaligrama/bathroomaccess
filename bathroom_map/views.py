import json

from django.views.generic.base import TemplateView
from django.core.serializers import serialize
from .models import Bathroom

class MarkersMapView(TemplateView):
    """Markers map view."""

    template_name = "map.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["markers"] = json.loads(serialize("json", Bathroom.objects.all()))
        return context