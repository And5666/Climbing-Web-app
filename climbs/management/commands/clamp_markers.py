from django.core.management.base import BaseCommand

from climbs.models import Climb, clamp_to_wall


class Command(BaseCommand):
    help = (
        "Clamp existing out-of-bounds climb markers back inside the "
        "wall photo bounds (minus the marker radius). The coordinate "
        "system is unchanged. Safe to re-run; lists every climb "
        "changed."
    )

    def handle(self, *args, **options):
        changed = []
        for climb in Climb.objects.all().order_by("id"):
            x, y = clamp_to_wall(climb.x_percent, climb.y_percent)
            if x != climb.x_percent or y != climb.y_percent:
                Climb.objects.filter(pk=climb.pk).update(
                    x_percent=x, y_percent=y)
                changed.append(
                    f"#{climb.id} {climb.name or climb.colour} "
                    f"({climb.x_percent}, {climb.y_percent}) -> ({x}, {y})")
        for line in changed:
            self.stdout.write(line)
        self.stdout.write(f"Clamped {len(changed)} climbs.")
