from django.core.management.base import BaseCommand

from climbs.services import reveal_due_mystery_climbs


class Command(BaseCommand):
    help = (
        "Reveal mystery climbs set at least a week ago, using the lower "
        "circuit tag when a grade overlaps two bands, and re-score their "
        "ascents. Safe to re-run: already-revealed climbs are untouched."
    )

    def handle(self, *args, **options):
        revealed = reveal_due_mystery_climbs()
        if revealed:
            names = ", ".join(
                f"{climb.name or climb.grade} ({climb.tag})"
                for climb in revealed)
            self.stdout.write(
                f"Revealed {len(revealed)} mystery climb(s): {names}")
        else:
            self.stdout.write("No mystery climbs due for reveal.")
