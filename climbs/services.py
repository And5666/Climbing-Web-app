"""Small cross-model services that do not belong in a view."""
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
    """Reveal mystery climbs set at least a week ago.

    The climb keeps its stored grade and takes the lower circuit tag
    when the grade overlaps two bands (V4 -> green). Every ascent on a
    revealed climb is re-scored from its stored tries and the new tag,
    so leaderboards change with the reveal. Idempotent: a second call
    finds no mystery climbs and changes nothing.

    Returns the climbs that were revealed this call.
    """
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
                # A malformed grade should not silently become a tag.
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
