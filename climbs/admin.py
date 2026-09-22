from django.contrib import admin
from .models import (
    Ascent, AscentAudit, Climb, Flag, GradeSuggestion, ModerationLog,
    Rating,
)

admin.site.register(Climb)
admin.site.register(Ascent)
admin.site.register(GradeSuggestion)
admin.site.register(Rating)
admin.site.register(AscentAudit)
admin.site.register(Flag)
admin.site.register(ModerationLog)