GRADE_BASE_POINTS = {
    'V0': 100, 'V1': 150, 'V2': 200, 'V3': 300, 'V4': 400,
    'V5': 550, 'V6': 700, 'V7': 900, 'V8': 1100, 'V9': 1400, 'V10': 1700,
}

def calculate_points(grade: str, tries: int) -> int:
    base = GRADE_BASE_POINTS.get(grade, 100)
    if tries == 1:
        return base
    # Tries top out at 5+: anything more scores the same as 5.
    penalty = min(0.4, (min(tries, 5) - 1) * 0.1)
    return round(base * (1 - penalty))