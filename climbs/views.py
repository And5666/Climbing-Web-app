import json

from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import user_passes_test
from django.db import IntegrityError
from django.db.models import Avg, Count, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.utils.timezone import now
from django.views.decorators.http import require_http_methods
from django.views.generic import CreateView

from .forms import SignupForm
from .models import (
    Ascent,
    Climb,
    ClimbSet,
    Comment,
    GradeSuggestion,
    Rating,
    TAG_CHOICES,
    TAG_VALUES,
    WALL_CHOICES,
    grade_number,
)

MAP_WIDTH = 256    # update to match your tightened viewBox width
MAP_HEIGHT = 512   # update to match your tightened viewBox height

GRADE_VALUES = [g for g, _ in Climb.GRADE_CHOICES]
WALL_VALUES = [w for w, _ in WALL_CHOICES]


def _selected_wall_set(wall, requested):
    """(selected, active) set for one wall: the requested set when it
    belongs to the wall, else the active set, else the newest set."""
    sets = list(ClimbSet.objects.filter(wall=wall).order_by("-created_at"))
    active = ClimbSet.active(wall)
    selected = active
    if requested:
        match = next((s for s in sets if str(s.id) == requested), None)
        if match is not None:
            selected = match
    if selected is None and sets:
        selected = sets[0]
    return selected, active, sets


def map_view(request):
    # Sent climbs show a green grade for the viewer who sent them.
    sent_ids = set()
    if request.user.is_authenticated:
        sent_ids = set(Ascent.objects.filter(
            user=request.user).values_list("climb_id", flat=True))
    walls = []
    for wall, wall_display in WALL_CHOICES:
        selected, active, sets = _selected_wall_set(
            wall, request.GET.get(f"{wall}_set"))
        walls.append({"wall": wall, "wall_display": wall_display,
                      "sets": sets, "active": active,
                      "selected": selected})
    # Deep link ?climb=<id> (e.g. from a past board) jumps to that
    # climb's own set so the marker is actually on the map.
    jump = request.GET.get("climb")
    if jump:
        try:
            target = Climb.objects.get(id=int(jump))
        except (Climb.DoesNotExist, ValueError):
            target = None
        if target is not None and target.climb_set_id is not None:
            for w in walls:
                if w["wall"] == target.wall:
                    match = next((s for s in w["sets"]
                                  if s.id == target.climb_set_id), None)
                    if match is not None:
                        w["selected"] = match
    climbs = []
    for w in walls:
        if w["selected"] is None:
            continue
        qs = Climb.objects.filter(wall=w["wall"],
                                  climb_set=w["selected"])
        if w["active"] is not None and w["selected"].id == w["active"].id:
            qs = qs.filter(is_active=True)
        climbs.extend(qs)
    for w in walls:
        w["is_archived"] = (
            w["selected"] is not None
            and (w["active"] is None or w["selected"].id != w["active"].id))
    return render(request, "climbs/map.html", {
        "climbs": climbs,
        "sent_ids": sent_ids,
        "walls": walls,
        "has_sets": any(w["sets"] for w in walls),
        "archived_any": any(w["is_archived"] for w in walls),
        "map_width": MAP_WIDTH,
        "map_height": MAP_HEIGHT,
        "grade_choices": GRADE_VALUES,
    })


def _leaderboard_rows(climb_set):
    if climb_set is None:
        return []
    return list(
        Ascent.objects.filter(climb__climb_set=climb_set)
        .values("user__username", "user__avatar")
        .annotate(total_points=Sum("points"), climb_count=Count("climb", distinct=True))
        .order_by("-total_points")
    )


def _deal_positions(rows, key="total_points"):
    """Competition ranking for a board, dealt in place: equal scores
    share a position (1, 2, 2, 4), so a draw splits the medal."""
    last_score, last_rank = None, 0
    for index, row in enumerate(rows, start=1):
        if row[key] != last_score:
            last_score, last_rank = row[key], index
        row["position"] = last_rank
    return rows


def _set_date_range(wall_sets, climb_set):
    """(start, end) datetimes for a set: it runs from its creation
    until the next set on the same wall starts. End is None while the
    set is the latest."""
    if climb_set is None:
        return (None, None)
    key = (climb_set.created_at, climb_set.id)
    later = [s.created_at for s in wall_sets
             if (s.created_at, s.id) > key]
    return (climb_set.created_at, min(later) if later else None)


def _set_stats(climb_set):
    """Best rated / most popular / flash-friendly / totals / grade
    graph for one set. Empty when nothing is logged on it yet."""
    if climb_set is None:
        return None
    climbs = list(climb_set.climbs.all())
    if not climbs:
        return None
    by_id = {c.id: c for c in climbs}
    rated = (Climb.objects.filter(climb_set=climb_set)
             .annotate(avg=Avg("ratings__value"), rc=Count("ratings"))
             .filter(rc__gte=1).order_by("-avg", "-rc").first())
    popular = (Climb.objects.filter(climb_set=climb_set)
               .annotate(sends=Count("ascents"))
               .filter(sends__gte=1).order_by("-sends").first())
    tries = list(Ascent.objects.filter(climb__climb_set=climb_set)
                 .values("climb_id", "user_id", "tries"))
    # Softest/hardest come from grade suggestions vs the set grade.
    # At least two votes in a direction, or the row stays empty.
    soft_votes, hard_votes = {}, {}
    for s in GradeSuggestion.objects.filter(
            climb__climb_set=climb_set).values("climb_id",
                                               "suggested_grade"):
        target = grade_number(by_id[s["climb_id"]].grade)
        number = grade_number(s["suggested_grade"])
        if target is None or number is None:
            continue
        if number < target:
            soft_votes[s["climb_id"]] = soft_votes.get(s["climb_id"], 0) + 1
        elif number > target:
            hard_votes[s["climb_id"]] = hard_votes.get(s["climb_id"], 0) + 1
    softest = max(soft_votes.items(), key=lambda kv: kv[1], default=None)
    hardest = max(hard_votes.items(), key=lambda kv: kv[1], default=None)
    if softest is not None and softest[1] < 2:
        softest = None
    if hardest is not None and hardest[1] < 2:
        hardest = None
    sends_by_grade = {}
    sends_by_grade_tag = {}
    for a in tries:
        climb = by_id[a["climb_id"]]
        sends_by_grade[climb.grade] = sends_by_grade.get(climb.grade, 0) + 1
        tag = climb.tag if climb.tag in TAG_VALUES else 'mystery'
        key = (climb.grade, tag)
        sends_by_grade_tag[key] = sends_by_grade_tag.get(key, 0) + 1
    grades = sorted({c.grade for c in climbs}, key=grade_number)
    top = max([sends_by_grade.get(g, 0) for g in grades] + [0])

    def segments_for(grade):
        # Every segment wears the tag set on its own climbs: blue tag
        # means a blue segment. No voting, no blending.
        segments = []
        for tag in TAG_VALUES:
            sends = sends_by_grade_tag.get((grade, tag), 0)
            if not sends:
                continue
            segments.append({
                "tag": f'tag-{tag}',
                "sends": sends,
                "pct": round(sends / top * 100) if top else 0,
            })
        return segments
    return {
        "best_rated": (
            {"climb": rated, "avg": round(rated.avg, 1),
             "count": rated.rc} if rated else None),
        "most_popular": (
            {"climb": popular, "sends": popular.sends}
            if popular else None),
        "softest": (
            {"climb": by_id[softest[0]], "votes": softest[1]}
            if softest else None),
        "hardest": (
            {"climb": by_id[hardest[0]], "votes": hardest[1]}
            if hardest else None),
        "total_sends": len(tries),
        "climbers": len({a["user_id"] for a in tries}),
        "grade_graph": [{
            "grade": g,
            "sends": sends_by_grade.get(g, 0),
            "segments": segments_for(g),
        } for g in grades],
    }


def leaderboard_view(request):
    walls = []
    for wall, wall_display in WALL_CHOICES:
        sets = list(ClimbSet.objects.filter(wall=wall))
        selected = ClimbSet.active(wall)
        requested = request.GET.get(f"{wall}_set")
        if requested:
            match = next((s for s in sets if str(s.id) == requested), None)
            if match is not None:
                selected = match
        rows = _deal_positions(_leaderboard_rows(selected))
        me = None
        if request.user.is_authenticated:
            for row in rows:
                if row["user__username"] == request.user.username:
                    me = {
                        "rank": row["position"],
                        "of": len(rows),
                        "points": row["total_points"],
                        "climbs": row["climb_count"],
                        "avatar": row["user__avatar"],
                    }
                    break
        archived = [s for s in sets if not s.is_active]
        for s in archived:
            s.range_start, s.range_end = _set_date_range(sets, s)
        if archived:
            climb_counts = dict(
                Climb.objects.filter(climb_set__in=archived)
                .values("climb_set").annotate(n=Count("id"))
                .values_list("climb_set", "n"))
            send_counts = dict(
                Ascent.objects.filter(climb__climb_set__in=archived)
                .values("climb__climb_set").annotate(n=Count("id"))
                .values_list("climb__climb_set", "n"))
            for s in archived:
                s.n_climbs = climb_counts.get(s.id, 0)
                s.n_sends = send_counts.get(s.id, 0)
        is_archived = selected is not None and not selected.is_active
        if is_archived:
            selected.range_start, selected.range_end = _set_date_range(
                sets, selected)
        walls.append({
            "wall": wall,
            "wall_display": wall_display,
            "sets": sets,
            "selected_set": selected,
            "is_archived": is_archived,
            "archived_sets": archived,
            "rows": rows,
            "me": me,
            "stats": _set_stats(selected),
        })
    return render(request, "climbs/leaderboard.html", {"walls": walls})


def _grade_vote_counts(climb):
    """Split grade suggestions into harder/at/softer vs the set grade."""
    target = grade_number(climb.grade)
    counts = {"harder": 0, "at": 0, "softer": 0}
    for suggested in climb.grade_suggestions.values_list("suggested_grade", flat=True):
        number = grade_number(suggested)
        if target is None or number is None:
            continue
        if number > target:
            counts["harder"] += 1
        elif number < target:
            counts["softer"] += 1
        else:
            counts["at"] += 1
    counts["total"] = counts["harder"] + counts["at"] + counts["softer"]
    return counts


def climb_detail_api(request, climb_id):
    climb = get_object_or_404(Climb, id=climb_id)
    active = ClimbSet.active(climb.wall)
    archived = (
        climb.climb_set_id is None
        or active is None
        or climb.climb_set_id != active.id
        or not climb.is_active
    )
    stats = climb.ratings.aggregate(avg_rating=Avg("value"), count=Count("id"))
    comments = [
        {
            "id": c.id,
            "user": c.user.username,
            "text": c.text,
            "created_at": c.created_at.isoformat(),
            "mine": request.user.is_authenticated and c.user_id == request.user.id,
        }
        for c in climb.comments.select_related("user")
    ]
    viewer = {"authenticated": request.user.is_authenticated}
    if request.user.is_authenticated:
        ascent = climb.ascents.filter(user=request.user).first()
        rating = climb.ratings.filter(user=request.user).first()
        suggestion = climb.grade_suggestions.filter(user=request.user).first()
        viewer.update({
            "tries": ascent.tries if ascent else None,
            "points": ascent.points if ascent else None,
            "my_rating": rating.value if rating else None,
            "my_grade": suggestion.suggested_grade if suggestion else None,
        })
    return JsonResponse({
        "id": climb.id,
        "name": climb.name,
        "grade": climb.grade,
        "tag": climb.tag,
        "colour": climb.colour,
        "wall": climb.wall,
        "wall_display": climb.get_wall_display(),
        "climb_set": climb.climb_set.label if climb.climb_set else None,
        "archived": archived,
        "rating_avg": round(stats["avg_rating"], 1) if stats["avg_rating"] else None,
        "rating_count": stats["count"],
        "ascent_count": climb.ascents.count(),
        "recent_ascents": [
            {"user": a.user.username, "tries": a.tries, "points": a.points}
            for a in climb.ascents.select_related("user").order_by("-id")[:3]
        ],
        "grade_votes": _grade_vote_counts(climb),
        "comments": comments,
        "viewer": viewer,
    })


def climber_api(request, username):
    """Mini public profile for tapping a name on a board or comment:
    picture, totals, hardest grade and current ranks."""
    user = get_object_or_404(get_user_model(), username=username)
    totals = user.ascents.aggregate(sends=Count("id"), points=Sum("points"))
    best = None
    for grade in user.ascents.values_list("climb__grade", flat=True):
        number = grade_number(grade)
        if number is not None and (best is None or number > best):
            best = number
    ranks = []
    for wall, display in WALL_CHOICES:
        climb_set = ClimbSet.active(wall)
        if climb_set is None:
            continue
        board = _deal_positions(_leaderboard_rows(climb_set))
        position = next(
            (r["position"] for r in board
             if r["user__username"] == user.username),
            None,
        )
        mine = next(
            (r for r in board if r["user__username"] == user.username), None)
        ranks.append({
            "wall": display,
            "position": position,
            "of": len(board),
            "points": mine["total_points"] if mine else 0,
        })
    return JsonResponse({
        "username": user.username,
        "avatar": user.avatar.url if user.avatar else None,
        "since": user.date_joined.date().strftime("%d/%m/%Y"),
        "sends": totals["sends"] or 0,
        "points": totals["points"] or 0,
        "best_grade": f"V{best}" if best is not None else None,
        "ranks": ranks,
    })


def _login_required_json(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required."}, status=403)
    return None


@require_http_methods(["POST", "DELETE"])
def ascent_api(request, climb_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    climb = get_object_or_404(Climb, id=climb_id)
    if request.method == "DELETE":
        # Undo your own send: the marker, boards and profile all read
        # live data, so everything updates on the next load.
        ascent = Ascent.objects.filter(
            user=request.user, climb=climb).first()
        if ascent is None:
            return JsonResponse({"error": "No send logged."}, status=404)
        ascent.delete()
        return JsonResponse({"status": "deleted"})
    try:
        tries = int(json.loads(request.body).get("tries", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Tries must be a number."}, status=400)
    if tries < 1:
        return JsonResponse({"error": "Tries must be at least 1."}, status=400)
    if Ascent.objects.filter(user=request.user, climb=climb).exists():
        return JsonResponse(
            {"error": "Already logged — undo it first to change it."},
            status=400)
    # The top bucket is 5+: anything higher is stored and scored as 5.
    # The exists() check above races a double-tap: the unique
    # (user, climb) constraint is the backstop, reported the same way.
    try:
        ascent = Ascent.objects.create(
            user=request.user, climb=climb, tries=min(tries, 5))
    except IntegrityError:
        return JsonResponse(
            {"error": "Already logged — undo it first to change it."},
            status=400)
    return JsonResponse({"tries": ascent.tries, "points": ascent.points})


@require_http_methods(["POST"])
def rating_api(request, climb_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    climb = get_object_or_404(Climb, id=climb_id)
    try:
        value = int(json.loads(request.body).get("value", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Rating must be a number."}, status=400)
    if not 1 <= value <= 5:
        return JsonResponse({"error": "Rating must be between 1 and 5."}, status=400)
    Rating.objects.update_or_create(
        user=request.user, climb=climb, defaults={"value": value})
    stats = climb.ratings.aggregate(avg_rating=Avg("value"), count=Count("id"))
    return JsonResponse({
        "my_rating": value,
        "rating_avg": round(stats["avg_rating"], 1),
        "rating_count": stats["count"],
    })


@require_http_methods(["POST"])
def grade_vote_api(request, climb_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    climb = get_object_or_404(Climb, id=climb_id)
    try:
        suggested = json.loads(request.body).get("suggested_grade")
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    if suggested not in GRADE_VALUES:
        return JsonResponse(
            {"error": f"Suggested grade must be one of {', '.join(GRADE_VALUES)}."},
            status=400)
    GradeSuggestion.objects.update_or_create(
        user=request.user, climb=climb,
        defaults={"suggested_grade": suggested})
    return JsonResponse({"my_grade": suggested, **_grade_vote_counts(climb)})


@require_http_methods(["POST"])
def comment_create_api(request, climb_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    climb = get_object_or_404(Climb, id=climb_id)
    try:
        text = (json.loads(request.body).get("text") or "").strip()
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    if not text or len(text) > 1000:
        return JsonResponse(
            {"error": "Comment must be between 1 and 1000 characters."},
            status=400)
    comment = Comment.objects.create(climb=climb, user=request.user, text=text)
    return JsonResponse({
        "id": comment.id,
        "user": request.user.username,
        "text": comment.text,
        "created_at": comment.created_at.isoformat(),
    })


@require_http_methods(["DELETE", "POST"])
def comment_delete_api(request, climb_id, comment_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    comment = get_object_or_404(Comment, id=comment_id, climb_id=climb_id)
    if comment.user_id != request.user.id and not request.user.is_staff:
        return JsonResponse({"error": "Not allowed."}, status=403)
    comment.delete()
    return JsonResponse({"status": "deleted"})


def staff_check(user):
    return user.is_authenticated and user.is_staff


def _active_set_for(wall):
    climb_set = ClimbSet.active(wall)
    if climb_set is None:
        climb_set = ClimbSet.objects.create(
            wall=wall, label=f"{now():%B %Y}")
    return climb_set


def _admin_selection(request):
    """(wall, sets, active, selected) for the admin tabs: the requested
    set when it belongs to the wall, else the live set, else newest."""
    wall = request.GET.get("wall", "main")
    if wall not in WALL_VALUES:
        wall = "main"
    sets = list(ClimbSet.objects.filter(wall=wall).order_by("-created_at"))
    active = ClimbSet.active(wall)
    selected = active
    requested = request.GET.get("set")
    if requested:
        match = next((s for s in sets if str(s.id) == requested), None)
        if match is not None:
            selected = match
    if selected is None and sets:
        selected = sets[0]
    return wall, sets, active, selected


@user_passes_test(staff_check)
def climb_admin_view(request):
    wall, sets, active, selected = _admin_selection(request)
    climbs = (Climb.objects.filter(climb_set=selected).order_by("id")
              if selected is not None else [])
    tree = [{"set": s,
             "climbs": list(Climb.objects.filter(climb_set=s).order_by("id"))}
            for s in sets]
    return render(request, "climbs/climb_admin.html", {
        "admin_tab": "climbs",
        "walls": [{"wall": w, "display": d} for w, d in WALL_CHOICES],
        "wall": wall,
        "sets": sets,
        "active": active,
        "selected": selected,
        "tree": tree,
        "climbs": climbs,
        "map_width": MAP_WIDTH,
        "map_height": MAP_HEIGHT,
        "grade_choices": GRADE_VALUES,
        "tag_choices": TAG_CHOICES,
        "wall_choices": WALL_CHOICES,
    })


@user_passes_test(staff_check)
def board_admin_view(request):
    wall, sets, active, selected = _admin_selection(request)
    board = []
    if selected is not None:
        board = list(
            Ascent.objects.filter(climb__climb_set=selected)
            .values("user__username")
            .annotate(sends=Count("id"), points=Sum("points"))
            .order_by("-points"))
    return render(request, "climbs/board_admin.html", {
        "admin_tab": "boards",
        "walls": [{"wall": w, "display": d} for w, d in WALL_CHOICES],
        "wall": wall,
        "sets": sets,
        "active": active,
        "selected": selected,
        "board": board,
    })


def _activate_set(climb_set):
    """Make a set the live one: its climbs go live, every other set on
    the wall (and their climbs) goes quiet."""
    ClimbSet.objects.filter(wall=climb_set.wall).update(is_active=False)
    climb_set.is_active = True
    climb_set.save(update_fields=["is_active"])
    Climb.objects.filter(climb_set=climb_set).update(is_active=True)
    Climb.objects.filter(wall=climb_set.wall).exclude(
        climb_set=climb_set).update(is_active=False)


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_set_activate_api(request, set_id):
    climb_set = get_object_or_404(ClimbSet, id=set_id)
    _activate_set(climb_set)
    return JsonResponse({"status": "ok"})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_set_update_api(request, set_id):
    climb_set = get_object_or_404(ClimbSet, id=set_id)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    label = (data.get("label") or "").strip()
    if not label:
        return JsonResponse({"error": "Label required."}, status=400)
    climb_set.label = label
    climb_set.save(update_fields=["label"])
    return JsonResponse({"status": "ok", "label": label})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_set_delete_api(request, set_id):
    climb_set = get_object_or_404(ClimbSet, id=set_id)
    wall = climb_set.wall
    was_active = climb_set.is_active
    try:
        climb_set.climbs.all().delete()
        climb_set.delete()
    except Exception:
        return JsonResponse(
            {"error": "Could not delete that set."}, status=400)
    if was_active:
        newest = ClimbSet.objects.filter(wall=wall).order_by(
            "-created_at").first()
        if newest is not None:
            _activate_set(newest)
    return JsonResponse({"status": "deleted"})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def set_remove_user_api(request, set_id):
    """Pull a climber off one set's board: deletes their ascents in it
    (ratings, votes and comments stay untouched)."""
    climb_set = get_object_or_404(ClimbSet, id=set_id)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    username = (data.get("username") or "").strip()
    user = get_object_or_404(get_user_model(), username=username)
    try:
        removed, _ = Ascent.objects.filter(
            climb__climb_set=climb_set, user=user).delete()
    except Exception:
        return JsonResponse(
            {"error": "Could not remove that climber."}, status=400)
    return JsonResponse({"status": "ok", "removed": removed})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_create_api(request):
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    wall = data.get("wall")
    grade = data.get("grade")
    tag = data.get("tag", "white")
    climb_set = None
    if data.get("climb_set") is not None:
        # Placing straight into a set from the inspector: the set's
        # wall wins, so the climb can never land in the wrong set.
        try:
            climb_set = ClimbSet.objects.get(id=int(data["climb_set"]))
        except (ClimbSet.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"error": "Unknown set."}, status=400)
        wall = climb_set.wall
    if wall not in WALL_VALUES:
        return JsonResponse({"error": "Unknown wall."}, status=400)
    if grade not in GRADE_VALUES:
        return JsonResponse({"error": "Unknown grade."}, status=400)
    if tag not in TAG_VALUES:
        return JsonResponse({"error": "Unknown tag."}, status=400)
    try:
        x = float(data["x_percent"])
        y = float(data["y_percent"])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "x_percent/y_percent required."}, status=400)
    climb = Climb.objects.create(
        name=data.get("name", ""),
        grade=grade,
        tag=tag,
        colour=data.get("colour", ""),
        is_active=bool(data.get("is_active", True)),
        wall=wall,
        climb_set=climb_set or _active_set_for(wall),
        x_percent=x,
        y_percent=y,
    )
    return JsonResponse({"id": climb.id})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_update_api(request, climb_id):
    climb = get_object_or_404(Climb, id=climb_id)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    if "grade" in data and data["grade"] not in GRADE_VALUES:
        return JsonResponse({"error": "Unknown grade."}, status=400)
    if "wall" in data and data["wall"] not in WALL_VALUES:
        return JsonResponse({"error": "Unknown wall."}, status=400)
    if "tag" in data and data["tag"] not in TAG_VALUES:
        return JsonResponse({"error": "Unknown tag."}, status=400)
    for field in ("x_percent", "y_percent"):
        # Marker drags send numbers; anything else would blow up on
        # save, so reject it here with a 400 instead of a 500.
        if field in data:
            try:
                data[field] = float(data[field])
            except (TypeError, ValueError):
                return JsonResponse(
                    {"error": f"{field} must be a number."}, status=400)
    for field in ("name", "grade", "tag", "colour", "wall", "x_percent",
                  "y_percent", "is_active"):
        if field in data:
            setattr(climb, field, data[field])
    if "wall" in data:
        climb.climb_set = _active_set_for(data["wall"])
    climb.save()
    return JsonResponse({"status": "ok"})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_delete_api(request, climb_id):
    try:
        Climb.objects.filter(id=climb_id).delete()
    except Exception:
        return JsonResponse(
            {"error": "Could not delete that climb."}, status=400)
    return JsonResponse({"status": "deleted"})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_set_start_api(request):
    """Draft a new set for one wall. It stays quiet until made current;
    only a wall's very first set goes live straight away."""
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    wall = data.get("wall")
    if wall not in WALL_VALUES:
        return JsonResponse({"error": "Unknown wall."}, status=400)
    label = (data.get("label") or "").strip()
    if not label:
        label = f"{now():%B %Y}"
    # A new set drafts quietly: it goes live only when someone hits
    # "Make current". The exception is a wall's very first set, which
    # must be live or nothing on the wall is loggable.
    first = not ClimbSet.objects.filter(wall=wall).exists()
    climb_set = ClimbSet.objects.create(
        wall=wall, label=label, is_active=first)
    return JsonResponse({"id": climb_set.id, "label": climb_set.label})


class SignupView(CreateView):
    form_class = SignupForm
    template_name = "registration/signup.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        return response

    def get_success_url(self):
        return reverse_lazy("climbs:map")
