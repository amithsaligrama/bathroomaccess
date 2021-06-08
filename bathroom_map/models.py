from django.db import models


class Bathroom(models.Model):
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=511)
    zip = models.CharField(max_length=5)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    hours = models.CharField(max_length=255)
    remarks = models.TextField()