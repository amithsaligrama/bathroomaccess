"""bathroom_map URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.urls import path, include
from django.conf.urls.static import static
from django.contrib import admin
from django.views.generic import TemplateView
from rest_framework import routers, serializers, viewsets

from .models import Bathroom
from .views import bathrooms_view, bathrooms_order_by_distance_view

class BathroomSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Bathroom
        fields = ['name', 'address', 'zip', 'latitude', 'longitude', 'hours', 'remarks']

class BathroomViewSet(viewsets.ModelViewSet):
    queryset = Bathroom.objects.all()
    serializer_class = BathroomSerializer

router = routers.DefaultRouter()
router.register(r'bathrooms', BathroomViewSet)

urlpatterns = [
    path('api/', include(router.urls)),
    path('admin/', admin.site.urls),
    path('', bathrooms_view, name='home'),
    path('', include('pwa.urls')),
    path('map/', bathrooms_view, name='map'),
    path('api_ordered', bathrooms_order_by_distance_view, name='api'),
    path('.well-known/assetlinks.json', TemplateView.as_view(template_name='assetlinks.json', content_type='application/json')),
    path('privacy/', TemplateView.as_view(template_name='privacy.html', content_type='text/html')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
