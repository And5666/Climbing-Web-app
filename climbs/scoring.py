"""points for a send.

points = grade base times the tries multiplier, rounded to a whole
number. the grade mode and tries table are both in settings so they
can be changed together. points always use the setter grade, not the
grade people voted for, or votes could bump the score.

mystery climbs and weird grades just get a flat amount until the grade
is known.
"""

from django.conf import settings

# flat score while the grade is hidden. normal scoring starts after the
# reveal
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
    """base points for a grade"""
    from .models import grade_number

    mode, _ = _grade_mode_and_table(grade_mode=grade_mode)
    if mode not in GRADE_MODES:
        mode = DEFAULT_GRADE_MODE
    number = grade_number(grade)
    if number is None:
        # bad grade, same flat score as mystery
        return MYSTERY_FIXED_POINTS
    if mode == "flat":
        return 300
    if mode == "linear":
        return 100 * (number + 1)
    return 200 + 50 * number


def tries_multiplier(tries, tries_table=None):
    """multiplier for the try count. anything above the top key gets the
    top value, never lower"""
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
    # gap in a custom table, use the next key down
    lower = [key for key in table if key <= count]
    return table[max(lower)] if lower else table[top]


def calculate_points(grade, tries, tag=None,
                     grade_mode=None, tries_table=None):
    """work out the points for one send. mystery or a bad grade gets the
    flat amount. the grade mode and table args are for tests"""
    if tag == "mystery":
        return MYSTERY_FIXED_POINTS
    base = grade_base_points(grade, grade_mode=grade_mode)
    mult = tries_multiplier(tries, tries_table=tries_table)
    return round(base * mult)
