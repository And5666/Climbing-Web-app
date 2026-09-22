from django.core.management.base import BaseCommand

from climbs.anticheat import recompute_all


class Command(BaseCommand):
    help = (
        "Recompute anti-cheat flags for every user. Open flags are "
        "regenerated from current data; reviewed, dismissed and "
        "confirmed flags are history and stay untouched. Safe to re-run."
    )

    def handle(self, *args, **options):
        checked, created = recompute_all()
        self.stdout.write(
            f"Checked {checked} users, {created} open flags raised.")
