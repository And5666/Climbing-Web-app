from django.contrib import admin
from .models import Climb, Ascent, GradeSuggestion, Rating

admin.site.register(Climb)
admin.site.register(Ascent)
admin.site.register(GradeSuggestion)
admin.site.register(Rating)