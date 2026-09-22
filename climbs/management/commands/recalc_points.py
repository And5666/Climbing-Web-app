from django.core.management.base import BaseCommand

from climbs.models import Ascent
from climbs.scoring import calculate_points


class Command(BaseCommand):
    help = (
        "Recompute points for every ascent from its stored grade and "
        "tries (official grade only). Tries and timestamps are never "
        "touched. Safe to re-run: only rows whose points differ are "
        "updated."
    )

    def handle(self, *args, **options):
        total = 0
        changed = 0
        ascents = Ascent.objects.select_related("climb").all()
        for ascent in ascents.iterator():
            total += 1
            fresh = calculate_points(
                ascent.climb.grade, ascent.tries, ascent.climb.tag)
            if ascent.points != fresh:
                # Queryset update: touches points only, never tries or
                # timestamps.
                Ascent.objects.filter(pk=ascent.pk).update(points=fresh)
                changed += 1
        self.stdout.write(
            f"Recalculated {total} ascents, {changed} changed.")
