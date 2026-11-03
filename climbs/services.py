"""small helpers that touch more than one model"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import (
    Ascent,
    Climb,
    MYSTERY_REVEAL_DAYS,
    tag_for_grade,
)
from .scoring import calculate_points


def reveal_due_mystery_climbs(today=None):
    """reveal mystery climbs that are a week old. they keep the grade
    and get the lower tag. sends on them get new points. calling it
    twice does nothing the second time"""
    cutoff = (today or timezone.localdate()) - timedelta(
        days=MYSTERY_REVEAL_DAYS)
    revealed = []
    with transaction.atomic():
        due = list(
            Climb.objects.select_for_update()
            .filter(tag="mystery", date_set__lte=cutoff)
        )
        for climb in due:
            tag = tag_for_grade(climb.grade)
            if not tag:
                # bad grade, dont guess a tag
                continue
            climb.tag = tag
            climb.save(update_fields=["tag"])
            revealed.append(climb)
        if revealed:
            ascents = Ascent.objects.filter(
                climb__in=revealed).select_related("climb")
            for ascent in ascents.iterator():
                fresh = calculate_points(
                    ascent.climb.grade, ascent.tries, ascent.climb.tag)
                if ascent.points != fresh:
                    Ascent.objects.filter(pk=ascent.pk).update(points=fresh)
    return revealed
