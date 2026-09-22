from datetime import date

from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.utils.timezone import now

from climbs.models import WALL_CHOICES, ClimbSet, grade_number

from .forms import DeleteAccountForm, ProfileForm

STAT_METRICS = (
    ("grade", "Best grade"),
    ("sends", "Sends"),
    ("points", "Points"),
)
STAT_WALLS = [("all", "All walls")] + list(WALL_CHOICES)
STAT_RANGES = (("6", "6 months"), ("12", "12 months"), ("all", "All time"))


def _month_window(end, n):
    """n (year, month) keys ending at end, oldest first."""
    keys = []
    year, month = end.year, end.month
    for _ in range(n):
        keys.append((year, month))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return keys[::-1]


def _stats_months(user, metric, wall, months_back):
    """Monthly series for the stats graph. Grade runs cumulative
    (hardest to date, the progression curve); sends and points are
    per-month counts, with quiet months shown as zero."""
    ascents = user.ascents.filter(
        is_voided=False).select_related("climb").order_by("logged_at")
    if wall != "all":
        ascents = ascents.filter(climb__wall=wall)
    ascents = list(ascents)
    today = now().date()
    if not ascents:
        return []
    if months_back is None:
        first = ascents[0].logged_at.date()
        span = ((today.year - first.year) * 12
                + (today.month - first.month) + 1)
        keys = _month_window(today, max(span, 1))
    else:
        keys = _month_window(today, months_back)
    buckets = {key: {"sends": 0, "points": 0, "best": 0} for key in keys}
    for ascent in ascents:
        key = (ascent.logged_at.year, ascent.logged_at.month)
        if key not in buckets:
            continue
        buckets[key]["sends"] += 1
        buckets[key]["points"] += ascent.points or 0
        number = grade_number(ascent.climb.grade) or 0
        buckets[key]["best"] = max(buckets[key]["best"], number)
    months = []
    running = 0
    for year, month in keys:
        bucket = buckets[(year, month)]
        if metric == "points":
            value, display = bucket["points"], str(bucket["points"])
        elif metric == "sends":
            value, display = bucket["sends"], str(bucket["sends"])
        else:
            running = max(running, bucket["best"])
            value = running
            display = f"V{running}" if running else "—"
        months.append({
            "label": date(year, month, 1).strftime("%b %Y"),
            "value": value,
            "display": display,
            "sends": bucket["sends"],
        })
    ceiling = max([m["value"] for m in months] + [1])
    for month in months:
        month["pct"] = round(month["value"] / ceiling * 100)
    return months


def _grade_distribution(user, wall):
    """The user's sends stacked by grade, hardest last."""
    ascents = user.ascents.filter(is_voided=False).select_related("climb")
    if wall != "all":
        ascents = ascents.filter(climb__wall=wall)
    counts = {}
    for grade in ascents.values_list("climb__grade", flat=True):
        counts[grade] = counts.get(grade, 0) + 1
    grades = sorted(counts, key=grade_number)
    top = max(counts.values(), default=0)
    return [{"grade": grade, "sends": counts[grade],
             "pct": round(counts[grade] / top * 100) if top else 0}
            for grade in grades]


def _wall_ranks(user):
    """Rank/points of one user on each wall's active set."""
    ranks = {}
    for wall, _display in WALL_CHOICES:
        climb_set = ClimbSet.active(wall)
        if climb_set is None:
            continue
        # Same scoring as the leaderboard: one query per wall.
        from climbs.views import _deal_positions, _leaderboard_rows
        board = _deal_positions(_leaderboard_rows(climb_set))
        position = next(
            (r["position"] for r in board
             if r["user__username"] == user.username),
            None,
        )
        mine = next(
            (r for r in board if r["user__username"] == user.username), None)
        ranks[wall] = {
            "position": position,
            "of": len(board),
            "points": mine["total_points"] if mine else 0,
        }
    return ranks


@login_required
def stats_view(request):
    metric = request.GET.get("metric", "grade")
    if metric not in dict(STAT_METRICS):
        metric = "grade"
    wall = request.GET.get("wall", "all")
    if wall not in dict(STAT_WALLS):
        wall = "all"
    span = request.GET.get("range", "12")
    if span not in ("6", "12", "all"):
        span = "12"
    totals = request.user.ascents.filter(
        is_voided=False).aggregate(sends=Count("id"), points=Sum("points"))
    grades = [grade_number(grade) for grade in
              request.user.ascents.filter(
                  is_voided=False).values_list(
                  "climb__grade", flat=True)]
    grades = [n for n in grades if n is not None]
    ranks = _wall_ranks(request.user)
    return render(request, "accounts/stats.html", {
        "metric": metric,
        "wall": wall,
        "span": span,
        "metric_choices": STAT_METRICS,
        "wall_choices": STAT_WALLS,
        "range_choices": STAT_RANGES,
        "total_sends": totals["sends"] or 0,
        "total_points": totals["points"] or 0,
        "best_grade": f"V{max(grades)}" if grades else "—",
        "months": _stats_months(
            request.user, metric, wall,
            None if span == "all" else int(span)),
        "distribution": _grade_distribution(request.user, wall),
        "rank_lines": [(display, ranks[w])
                       for w, display in WALL_CHOICES if w in ranks],
        "recent": list(request.user.ascents.select_related("climb")
                       .order_by("-logged_at")[:10]),
    })


@login_required
def profile_view(request):
    totals = request.user.ascents.filter(
        is_voided=False).aggregate(sends=Count("id"), points=Sum("points"))
    ranks = _wall_ranks(request.user)
    password_form = PasswordChangeForm(request.user)
    # Django autofocuses the current-password field, which yanks the
    # page down to Change password on every visit and invites the
    # browser to autofill. The graph lives on Full stats now.
    password_form.fields["old_password"].widget.attrs.pop(
        "autofocus", None)
    return render(request, "accounts/profile.html", {
        "profile_form": ProfileForm(instance=request.user),
        "password_form": password_form,
        "delete_form": DeleteAccountForm(request.user),
        "total_sends": totals["sends"] or 0,
        "total_points": totals["points"] or 0,
        "rank_lines": [(display, ranks[wall])
                       for wall, display in WALL_CHOICES if wall in ranks],
    })


@login_required
def profile_update_view(request):
    if request.method != "POST":
        return redirect("accounts:profile")
    form = ProfileForm(request.POST, request.FILES, instance=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Profile updated.")
    else:
        for errors in form.errors.values():
            messages.error(request, errors[0])
    return redirect("accounts:profile")


@login_required
def password_change_view(request):
    if request.method != "POST":
        return redirect("accounts:profile")
    form = PasswordChangeForm(request.user, request.POST)
    if form.is_valid():
        form.save()
        update_session_auth_hash(request, form.user)
        messages.success(request, "Password changed.")
    else:
        for errors in form.errors.values():
            messages.error(request, errors[0])
    return redirect("accounts:profile")


@login_required
def account_delete_view(request):
    if request.method != "POST":
        return redirect("accounts:profile")
    form = DeleteAccountForm(request.user, request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect("accounts:profile")
    username = request.user.username
    if request.user.avatar:
        request.user.avatar.delete(save=False)
    request.user.delete()
    logout(request)
    messages.success(request, f"Account {username} deleted.")
    return redirect("climbs:map")
