"""Points for a logged send.

points = grade_base[grade] x tries_multiplier[tries], rounded to int.

Both halves of the formula live in ONE config location in
``summit_map_project/settings.py`` so they can be retuned together:

* ``POINTS_GRADE_MODE`` — one of ``"flat"``, ``"gentle"`` (default)
  or ``"linear"``:

  - ``"flat"``: every grade scores 300.
  - ``"gentle"`` (default): 200 + 50 x V-number
    (V0=200 ... V4=400 ... V8=600).
  - ``"linear"``: 100 x (V-number + 1) (V0=100 ... V4=500 ...).

* ``POINTS_TRIES_MULTIPLIERS`` — one line mapping tries -> multiplier:

  ``{1: 1.00, 2: 0.99, 3: 0.98, 4: 0.96, 5: 0.94}``

  Tries at or above the highest key all score the table's last value
  (hard floor, never lower): 6 tries and 40 tries score the same.
  The floor stays high enough that, under the ``"gentle"`` and
  ``"linear"`` grade modes, any grade sent at 5+ tries still outscores
  a flash one grade below it (grade always beats tries): keep the
  floor above ~0.93 when retuning, or that guarantee breaks.

Points always use the climb's OFFICIAL (setter) grade — never the
community-suggested grade — so suggestions can't inflate scores.

Mystery climbs (tag ``"mystery"``) and climbs with an unparseable grade
score one flat amount (``MYSTERY_FIXED_POINTS``), exactly as before:
no flash bonus, no try penalty, until the grade is known.
"""

from django.conf import settings

# Mystery climbs award one flat amount while their grade is hidden —
# no flash bonus, no try penalty — until the setters reveal the grade
# the following week (later sends then score normally). Unparseable
# grades fall back to the same flat amount.
MYSTERY_FIXED_POINTS = 300

DEFAULT_TRIES_MULTIPLIERS = {1: 1.00, 2: 0.99, 3: 0.98, 4: 0.96, 5: 0.94}

GRADE_MODES = ("flat", "gentle", "linear")
DEFAULT_GRADE_MODE = "gentle"


def _grade_mode_and_table(grade_mode=None, tries_table=None):
    if grade_mode is None:
        grade_mode = getattr(
            settings, "POINTS_GRADE_MODE", DEFAULT_GRADE_MODE)
    if tries_table is None:
        tries_table = getattr(
            settings, "POINTS_TRIES_MULTIPLIERS",
            DEFAULT_TRIES_MULTIPLIERS)
    return grade_mode, tries_table


def grade_base_points(grade, grade_mode=None):
    """Base points for an official grade under one grade mode."""
    from .models import grade_number

    mode, _ = _grade_mode_and_table(grade_mode=grade_mode)
    if mode not in GRADE_MODES:
        mode = DEFAULT_GRADE_MODE
    number = grade_number(grade)
    if number is None:
        # Unrated / unparseable grade: flat amount, same as mystery.
        return MYSTERY_FIXED_POINTS
    if mode == "flat":
        return 300
    if mode == "linear":
        return 100 * (number + 1)
    return 200 + 50 * number


def tries_multiplier(tries, tries_table=None):
    """Multiplier for a try count. Tries at/above the top key take the
    top key's value (hard floor, never lower)."""
    _, table = _grade_mode_and_table(tries_table=tries_table)
    try:
        top = max(table)
    except (TypeError, ValueError):
        return 1.0
    try:
        count = int(tries)
    except (TypeError, ValueError):
        count = 1
    if count < 1:
        count = 1
    if count >= top:
        return table[top]
    if count in table:
        return table[count]
    # A gap in a custom table: take the nearest lower key's value.
    lower = [key for key in table if key <= count]
    return table[max(lower)] if lower else table[top]


def calculate_points(grade, tries, tag=None,
                     grade_mode=None, tries_table=None):
    """Points for sending `grade` in `tries` goes.

    `tag="mystery"` (or an unparseable grade) scores the flat mystery
    amount. Everything else is grade_base x tries_multiplier, rounded.
    `grade_mode` / `tries_table` override the configured values and
    exist so tests can exercise modes without touching settings.
    """
    if tag == "mystery":
        return MYSTERY_FIXED_POINTS
    base = grade_base_points(grade, grade_mode=grade_mode)
    mult = tries_multiplier(tries, tries_table=tries_table)
    return round(base * mult)
