from django.db import models

class Bathroom(models.Model):
    name = models.CharField(max_length=255, blank=True)
    address = models.CharField(max_length=511)
    zip = models.CharField(max_length=5)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True)
    hours = models.TextField(blank=True)
    remarks = models.TextField(blank=True)