"""Seed five months of demo history per wall: sets, climbs on the wall,
crew sends, ratings, comments and grade votes. Playground data only —
refuses to run when climbing data already exists unless --reset is
given (user accounts are always kept)."""

import random
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import make_aware, now

from climbs.models import (
    Ascent,
    Climb,
    ClimbSet,
    Comment,
    GradeSuggestion,
    Rating,
    WALL_CHOICES,
)
from climbs.views import MAP_HEIGHT, MAP_WIDTH

CREW = ["june", "leo", "milo", "nadia", "priya", "sam", "theo", "yara"]
PASSWORD = "demo1234"
MONTHS = 5
CLIMBS_PER_SET = 9

TAG_BY_GRADE = {
    0: ["white"], 1: ["white", "black"],
    2: ["red", "green"], 3: ["red", "green"], 4: ["green", "blue"],
    5: ["blue", "yellow"], 6: ["yellow", "orange"],
    7: ["orange"], 8: ["project"], 9: ["project"],
    10: ["project"],
}
COLOURS = ["red", "blue", "yellow", "green", "purple", "orange",
           "pink", "white", "black", "lime", "teal", "cyan",
           "magenta", "brown", "grey", "navy", "maroon", "coral"]
NAMES = [
    "Slab Happy", "Pinch Punch", "Crimp Reaper", "Dyno-mite",
    "Pocket Rocket", "Heel Hooker", "Toe Jam", "Mantle Piece",
    "Gaston Castle", "Undercling King", "Sidepull Cindy",
    "Compression Session", "Deadpoint", "Campus Life",
    "Sloper Operator", "Juggernaut", "Arete Racing", "Dihedral Dan",
]
COMMENTS = [
    "Crux is the third move — trust the smear.",
    "Soft for the grade, get on it.",
    "Harder than it looks from below.",
    "Flowed really well, great set.",
    "Flashable if you spot the heel.",
    "Took me all session. Worth it.",
    "Skip the pocket, go straight to the jug.",
    "Careful with the dab on the left.",
    "My new warmup.",
    "Fell on the last move twice. Rude.",
    "Beta: high foot, then commit.",
    "Slopers were better than they looked.",
]


class Command(BaseCommand):
    help = "Seed five months of demo climbing history per wall."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true",
            help="Delete all climbing data first (users are kept).")

    def handle(self, *args, **options):
        if options["reset"]:
            self._wipe()
        elif ClimbSet.objects.exists() or Climb.objects.exists():
            raise CommandError(
                "Climbing data already exists; re-run with --reset "
                "to wipe it first (users are kept).")
        rng = random.Random(20260918)
        crew = self._crew()
        today = now().date()
        first = (today.year, today.month)
        for _ in range(MONTHS - 1):
            first = (first[0], first[1] - 1)
            if first[1] == 0:
                first = (first[0] - 1, 12)
        months = []
        year, month = first
        for _ in range(MONTHS):
            months.append((year, month))
            month += 1
            if month == 13:
                month, year = 1, year + 1

        for wall, _display in WALL_CHOICES:
            for pos, (year, month) in enumerate(months):
                live = pos == len(months) - 1
                label = datetime(year, month, 1).strftime("%B %Y")
                stamp = make_aware(datetime(year, month, 12, 12, 0))
                climb_set = ClimbSet.objects.create(
                    wall=wall, label=label, is_active=live)
                ClimbSet.objects.filter(id=climb_set.id).update(
                    created_at=stamp)
                # The crew improves over time: harder grades creep in.
                grades = [0, 1, 2, 2, 3, 3, 4, 4 + pos // 2, 5 + pos // 2]
                for slot in range(CLIMBS_PER_SET):
                    grade_num = min(grades[slot % len(grades)], 8)
                    colour = COLOURS[(pos * 3 + slot) % len(COLOURS)]
                    tag = rng.choice(TAG_BY_GRADE[grade_num])
                    x = 30 + (slot % 3) * 98 + rng.uniform(-12, 12)
                    y = 60 + (slot // 3) * 130 + rng.uniform(-12, 12)
                    climb = Climb.objects.create(
                        name=NAMES[(pos * CLIMBS_PER_SET + slot) % len(NAMES)],
                        grade=f"V{grade_num}", tag=tag, colour=colour,
                        wall=wall, climb_set=climb_set,
                        is_active=live,
                        x_percent=round(min(max(x, 8), MAP_WIDTH - 8), 1),
                        y_percent=round(min(max(y, 8), MAP_HEIGHT - 8), 1),
                    )
                    Climb.objects.filter(id=climb.id).update(date_set=stamp.date())
                self._sends(rng, crew, climb_set, year, month, live)
                if live:
                    self._ratings(rng, crew, climb_set)
                    self._votes(rng, crew, climb_set)
            self._comments(rng, crew, wall, months[-1])

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {MONTHS} months per wall for "
            f"{len(CREW)} users (password {PASSWORD!r}): "
            f"{ClimbSet.objects.count()} sets, "
            f"{Climb.objects.count()} climbs, "
            f"{Ascent.objects.count()} sends, "
            f"{Rating.objects.count()} ratings, "
            f"{Comment.objects.count()} comments, "
            f"{GradeSuggestion.objects.count()} grade votes."))

    def _wipe(self):
        Ascent.objects.all().delete()
        Rating.objects.all().delete()
        GradeSuggestion.objects.all().delete()
        Comment.objects.all().delete()
        Climb.objects.all().delete()
        ClimbSet.objects.all().delete()

    def _crew(self):
        users = []
        for username in CREW:
            user, created = get_user_model().objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@example.com"})
            user.set_password(PASSWORD)
            user.save()
            users.append(user)
        return users

    def _sends(self, rng, crew, climb_set, year, month, live):
        # Recent sets get heavy traffic; old ones a scattering.
        chance = 0.8 if live else 0.3
        for climb in climb_set.climbs.all():
            difficulty = int(climb.grade.lstrip("V")) / 10
            for user in crew:
                if rng.random() > chance - difficulty * 0.3:
                    continue
                tries = min(5, 1 + int(rng.expovariate(0.8)))
                ascent = Ascent.objects.create(
                    user=user, climb=climb, tries=tries)
                day = rng.randint(1, 28)
                stamp = make_aware(datetime(year, month, day,
                                            rng.randint(9, 21),
                                            rng.randint(0, 59)))
                Ascent.objects.filter(id=ascent.id).update(logged_at=stamp)

    def _ratings(self, rng, crew, climb_set):
        for climb in climb_set.climbs.all():
            for user in rng.sample(crew, rng.randint(2, 4)):
                Rating.objects.get_or_create(
                    user=user, climb=climb,
                    defaults={"value": rng.choices([3, 4, 5], [1, 3, 2])[0]})

    def _votes(self, rng, crew, climb_set):
        climbs = list(climb_set.climbs.all())
        for climb, direction in zip(climbs[:6], ["harder"] * 3 + ["softer"] * 3):
            number = int(climb.grade.lstrip("V"))
            target = min(10, number + 1) if direction == "harder" else max(0, number - 1)
            for user in rng.sample(crew, 3 if direction == "harder" else 2):
                GradeSuggestion.objects.get_or_create(
                    user=user, climb=climb,
                    defaults={"suggested_grade": f"V{target}"})

    def _comments(self, rng, crew, wall, latest):
        year, month = latest
        climbs = list(Climb.objects.filter(
            wall=wall, climb_set__is_active=True))
        for i, text in enumerate(rng.sample(COMMENTS, min(6, len(COMMENTS)))):
            if i >= len(climbs):
                break
            comment = Comment.objects.create(
                climb=climbs[i], user=crew[i % len(crew)], text=text)
            stamp = make_aware(datetime(year, month, rng.randint(1, 28),
                                        rng.randint(9, 21), 0))
            Comment.objects.filter(id=comment.id).update(created_at=stamp)
