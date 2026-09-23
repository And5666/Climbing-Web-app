import json

from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import user_passes_test
from django.core.paginator import Paginator
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
    NewsPost,
    Rating,
    TAG_CHOICES,
    TAG_VALUES,
    WALL_CHOICES,
    TAGS_BY_GRADE,
    clamp_to_wall,
    grade_number,
    grades_for_tag,
    tag_supports_grade,
)
from .scoring import MYSTERY_FIXED_POINTS

MAP_WIDTH = 256    # overlay viewBox width (see models.WALL_MAP_WIDTH)
MAP_HEIGHT = 512   # overlay viewBox height (see models.WALL_MAP_HEIGHT)

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
    """Public board: voided ascents score nothing and hidden users sit
    out while under review. On the live set, soft-removed climbs
    (is_active=False) are gone from the wall, so their sends leave
    the board too; archived sets keep their full history."""
    if climb_set is None:
        return []
    ascents = Ascent.objects.filter(
        climb__climb_set=climb_set, is_voided=False,
        user__leaderboard_hidden=False)
    if climb_set.is_active:
        ascents = ascents.filter(climb__is_active=True)
    return list(
        ascents
        .values("user__username", "user__avatar")
        .annotate(total_points=Sum("points"), climb_count=Count("climb", distinct=True))
        .order_by("-total_points")
    )


def _audit_ascent(user, ascent, climb, actor, action,
                  old_tries=None, new_tries=None,
                  old_grade="", new_grade="",
                  old_points=None, new_points=None, note=""):
    """One row in the ascent audit ledger (server timestamps only)."""
    from .models import AscentAudit
    return AscentAudit.objects.create(
        user=user, ascent=ascent, climb=climb, actor=actor, action=action,
        old_tries=old_tries, new_tries=new_tries,
        old_grade=old_grade, new_grade=new_grade,
        old_points=old_points, new_points=new_points, note=note)


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
        if by_id[s["climb_id"]].tag == 'mystery':
            continue
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
        # Mystery sends bucket under '?' so the hidden grade can't be
        # read back out of the per-grade counts before the reveal.
        grade_key = '?' if climb.tag == 'mystery' else climb.grade
        sends_by_grade[grade_key] = sends_by_grade.get(grade_key, 0) + 1
        tag = climb.tag if climb.tag in TAG_VALUES else 'mystery'
        key = (grade_key, tag)
        sends_by_grade_tag[key] = sends_by_grade_tag.get(key, 0) + 1
    grades = sorted({('?' if c.tag == 'mystery' else c.grade) for c in climbs},
                    key=lambda g: (grade_number(g) is None, grade_number(g) or 0))
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
        if selected is None:
            climb_count = 0
        elif selected.is_active:
            # Live wall: removed climbs are gone from the map, so the
            # header total counts live climbs only.
            climb_count = Climb.objects.filter(
                climb_set=selected, is_active=True).count()
        else:
            climb_count = Climb.objects.filter(
                climb_set=selected).count()
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
            "climb_count": climb_count,
        })
    return render(request, "climbs/leaderboard.html", {"walls": walls})


def _tag_grade_error(tag, grade):
    """400 message when a tag/grade pair leaves its circuit band, or
    None when the pair is allowed (mystery allows any grade)."""
    if tag not in TAG_VALUES:
        return "Unknown tag."
    if grade not in GRADE_VALUES:
        return "Unknown grade."
    if not tag_supports_grade(tag, grade):
        allowed = ", ".join(grades_for_tag(tag))
        return f"That tag covers grades {allowed}."
    return None


def _grade_vote_counts(climb):
    """Split grade suggestions into harder/at/softer vs the set grade."""
    if climb.tag == 'mystery':
        return {"harder": 0, "at": 0, "softer": 0, "total": 0}
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
        "grade": climb.display_grade,
        "tag": climb.tag,
        "is_mystery": climb.is_mystery,
        "mystery_points": MYSTERY_FIXED_POINTS,
        "colour": climb.colour,
        "colour_name": climb.colour_name,
        "colour_hex": climb.colour_hex,
        "wall": climb.wall,
        "wall_display": climb.get_wall_display(),
        "climb_set": climb.climb_set.label if climb.climb_set else None,
        "archived": archived,
        "rating_avg": round(stats["avg_rating"], 1) if stats["avg_rating"] else None,
        "rating_count": stats["count"],
        "ascent_count": climb.ascents.count(),
        "recent_ascents": [
            {"user": a.user.username, "tries": a.tries, "points": a.points,
             "voided": a.is_voided}
            for a in climb.ascents.select_related("user").order_by("-id")
        ],
        "grade_votes": _grade_vote_counts(climb),
        "comments": comments,
        "viewer": viewer,
    })


def set_user_sends_api(request, set_id, username):
    """One climber's whole log on one set, newest first, for tapping
    a name on either leaderboard: climb, grade, tries, points and
    time per send. Non-voided ascents only — no flags, audit trail
    or voided history (staff keep the full user-log page for that).
    Mystery grades stay '?' until revealed. Hidden users stay
    private to everyone but staff."""
    from django.http import Http404
    from zoneinfo import ZoneInfo
    climb_set = get_object_or_404(ClimbSet, id=set_id)
    user = get_object_or_404(get_user_model(), username=username)
    if user.leaderboard_hidden and not (
            request.user.is_authenticated and request.user.is_staff):
        raise Http404()
    ascents = (Ascent.objects
               .filter(user=user, climb__climb_set=climb_set,
                       is_voided=False)
               .select_related("climb").order_by("-logged_at", "-id"))
    london = ZoneInfo("Europe/London")
    return JsonResponse({
        "username": user.username,
        "set": climb_set.label,
        "sends": [{
            "climb": a.climb.name or a.climb.colour_name,
            "colour": a.climb.colour,
            "grade": a.climb.display_grade,
            "tries": a.tries,
            "points": a.points,
            "time": a.logged_at.astimezone(london).strftime(
                "%d/%m/%Y %H:%M"),
        } for a in ascents],
    })


def climber_api(request, username):
    """Mini public profile for tapping a name on a board or comment:
    picture, totals, hardest grade and current ranks."""
    user = get_object_or_404(get_user_model(), username=username)
    totals = user.ascents.filter(is_voided=False).aggregate(
        sends=Count("id"), points=Sum("points"))
    best = None
    # Mystery climbs stay out: their hidden grade must not leak
    # through a climber's "best" line before the reveal. Voided
    # ascents score nothing, so they never count as a best either.
    for grade in user.ascents.exclude(
            climb__tag="mystery").filter(
            is_voided=False).values_list("climb__grade", flat=True):
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
        _audit_ascent(request.user, None, climb, request.user, "delete",
                      old_tries=ascent.tries, old_grade=climb.grade,
                      old_points=ascent.points)
        ascent.delete()
        from .anticheat import refresh_user_flags
        refresh_user_flags(request.user)
        return JsonResponse({"status": "deleted"})
    try:
        tries = int(json.loads(request.body).get("tries", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Tries must be a number."}, status=400)
    if tries < 1 or tries > 5:
        return JsonResponse(
            {"error": "Tries must be between 1 and 5."}, status=400)
    if Ascent.objects.filter(user=request.user, climb=climb).exists():
        return JsonResponse(
            {"error": "Already logged — undo it first to change it."},
            status=400)
    # Basic prevention: per-user rate limit on logging.
    from .anticheat import rate_limited, refresh_user_flags
    if rate_limited(request.user):
        return JsonResponse(
            {"error": "Too many sends logged lately — try again later."},
            status=429)
    # The actual try count is stored (scoring floors at 5+, so 6 and
    # 40 score the same). The exists() check above races a double-tap:
    # the unique (user, climb) constraint is the backstop, reported
    # the same way.
    try:
        ascent = Ascent.objects.create(
            user=request.user, climb=climb, tries=tries)
    except IntegrityError:
        return JsonResponse(
            {"error": "Already logged — undo it first to change it."},
            status=400)
    _audit_ascent(request.user, ascent, climb, request.user, "create",
                  new_tries=ascent.tries, new_grade=climb.grade,
                  new_points=ascent.points)
    refresh_user_flags(request.user)
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
    if climb.tag == 'mystery':
        return JsonResponse(
            {"error": "Mystery climbs can't be voted harder or softer "
                      "until their grade is revealed."},
            status=400)
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
    from .models import TAPE_COLOURS, TAPE_FAMILIES
    tape_colours_json = json.dumps(dict(TAPE_COLOURS))
    by_name = dict(TAPE_COLOURS)
    tape_swatches_json = json.dumps([
        {"family": family,
         "swatches": [{"name": name, "hex": by_name[name]}
                      for name in names]}
        for family, names in TAPE_FAMILIES
    ])
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
        "tape_colours": TAPE_COLOURS,
        "tape_colours_json": tape_colours_json,
        "tape_swatches_json": tape_swatches_json,
    })


def _group_flags(flags):
    """Collapse same-rule flags into display groups (newest first):
    one header line per rule with a combined points total, keeping
    the individual findings and their review buttons underneath."""
    groups = []
    index = {}
    for flag in flags:
        key = (flag.rule_code, flag.severity, flag.status)
        group = index.get(key)
        if group is None:
            group = {"rule": flag.rule_code, "severity": flag.severity,
                     "status": flag.status, "flags": [], "points": 0}
            index[key] = group
            groups.append(group)
        group["flags"].append(flag)
        group["points"] += flag.points or 0
    for group in groups:
        group["count"] = len(group["flags"])
    return groups


@user_passes_test(staff_check)
def board_admin_view(request):
    """Staff board with anti-cheat review: a Flags column per user
    (suspicion badge + expandable rule evidence), moderation actions,
    and flagged / suspicion / status filters. The wall tab scopes by
    wall; the set picker scopes by set."""
    from .models import Flag
    wall, sets, active, selected = _admin_selection(request)
    board = []
    if selected is not None:
        board_ascents = Ascent.objects.filter(
            climb__climb_set=selected, is_voided=False)
        if selected.is_active:
            # Match the public board: removed climbs leave the review
            # totals too while the set is live.
            board_ascents = board_ascents.filter(climb__is_active=True)
        rows = list(
            board_ascents
            .values("user__username")
            .annotate(sends=Count("id"), points=Sum("points"))
            .order_by("-points"))
        usernames = [r["user__username"] for r in rows]
        User = get_user_model()
        users = {u.username: u
                 for u in User.objects.filter(username__in=usernames)}
        all_flags = list(Flag.objects.filter(
            user__username__in=usernames)
            .select_related("ascent__climb").order_by("-created_at"))
        by_user = {}
        for flag in all_flags:
            by_user.setdefault(flag.user_id, []).append(flag)
        by_name = {}
        for username, user in users.items():
            by_name[username] = by_user.get(user.id, [])
        for row in rows:
            user = users.get(row["user__username"])
            flags = by_name.get(row["user__username"], [])
            # This board reviews one set: ascent-tied flags show only
            # when the ascent sits on the viewed set. User-level flags
            # (no ascent: whole-history patterns like flash rate) stay
            # visible wherever the climber boards.
            row["hidden"] = user.leaderboard_hidden if user else False
            row["flags"] = [
                f for f in flags
                if f.ascent is None or f.ascent.climb.climb_set_id
                == selected.id
            ]
            row["open_flags"] = [f for f in row["flags"]
                                 if f.status == "open"]
        flagged_only = request.GET.get("flagged") == "1"
        try:
            suspicion_min = int(request.GET.get("suspicion", "") or 0)
        except ValueError:
            suspicion_min = 0
        if suspicion_min < 0:
            suspicion_min = 0
        status = request.GET.get("status", "")
        if flagged_only:
            rows = [r for r in rows if r["open_flags"]]
        if status in ("open", "reviewed", "dismissed", "confirmed"):
            rows = [r for r in rows if any(
                f.status == status for f in r["flags"])]
        for row in rows:
            # The status filter narrows the rows AND the flags listed
            # inside them: filtering status=open must not keep showing
            # the row's closed flags. If the status filter leaves a
            # displayed row with nothing shown, fall back to all its
            # flags rather than hiding evidence.
            shown = [
                f for f in row["flags"]
                if status not in ("open", "reviewed", "dismissed",
                                  "confirmed") or f.status == status
            ]
            if not shown:
                shown = list(row["flags"])
            row["shown_flags"] = shown
            row["shown_open"] = [f for f in shown if f.status == "open"]
            row["shown_suspicion"] = min(
                sum(f.points for f in row["shown_open"]), 100)
            # Fifteen flashes in a row read as one line, not fifteen:
            # same-rule flags collapse into a group with a combined
            # points total; the individual findings stay expandable
            # underneath with their own review buttons.
            row["open_groups"] = _group_flags(row["shown_open"])
            row["closed_groups"] = _group_flags(
                [f for f in shown if f.status != "open"])
        if suspicion_min:
            rows = [r for r in rows
                    if r["shown_suspicion"] >= suspicion_min]
        board = rows
    total_sends = sum(r["sends"] for r in board)
    open_flag_count = sum(len(r["shown_open"]) for r in board)
    if selected is None:
        climb_count = 0
    elif selected.is_active:
        climb_count = Climb.objects.filter(
            climb_set=selected, is_active=True).count()
    else:
        climb_count = Climb.objects.filter(climb_set=selected).count()
    return render(request, "climbs/board_admin.html", {
        "admin_tab": "boards",
        "walls": [{"wall": w, "display": d} for w, d in WALL_CHOICES],
        "wall": wall,
        "sets": sets,
        "active": active,
        "selected": selected,
        "board": board,
        "total_sends": total_sends,
        "open_flag_count": open_flag_count,
        "climb_count": climb_count,
        "flagged_only": request.GET.get("flagged") == "1",
        "suspicion_filter": request.GET.get("suspicion", ""),
        "status_filter": request.GET.get("status", ""),
    })


def _staff_or_404(request):
    """Staff/superuser only, 404 for everyone else: flag review must
    never be visible — or inferable — to normal users."""
    if not (request.user.is_authenticated and request.user.is_staff):
        from django.http import Http404
        raise Http404()
    return True


def _moderation_log(actor, action, username="", details=None, note=""):
    from .models import ModerationLog
    return ModerationLog.objects.create(
        actor=actor, action=action, username=username,
        details=details or {}, note=note or "")


@require_http_methods(["POST"])
def flag_review_api(request, flag_id):
    """Review one flag: dismiss / mark reviewed / confirm. Dismissing
    or confirming requires a note; marking reviewed offers one. The
    action is logged. Non-punitive wording throughout."""
    from .models import Flag
    _staff_or_404(request)
    flag = get_object_or_404(Flag, id=flag_id)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    status = (data.get("status") or "").strip()
    if status not in ("reviewed", "dismissed", "confirmed"):
        return JsonResponse({"error": "Unknown status."}, status=400)
    note = (data.get("note") or "").strip()
    if status in ("dismissed", "confirmed") and not note:
        return JsonResponse(
            {"error": "A note is required to dismiss or confirm."},
            status=400)
    flag.status = status
    flag.note = note
    flag.reviewed_by = request.user
    flag.save(update_fields=["status", "note", "reviewed_by"])
    _moderation_log(request.user, f"flag_{status}",
                    username=flag.user.username,
                    details={"flag_id": flag.id,
                             "rule": flag.rule_code},
                    note=note)
    return JsonResponse({"status": "ok"})


@require_http_methods(["POST"])
def flags_review_many_api(request):
    """Mark several flags reviewed in one action (one optional note
    for the lot). Bulk is reviewed-only: dismissing or confirming
    stays per-flag, where the required note belongs to one finding.
    Only open flags move; anything already closed is skipped and
    reported. Every change is logged per flag, like the single API.
    """
    from .models import Flag
    _staff_or_404(request)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    ids = data.get("ids") or []
    if (not isinstance(ids, list) or not ids
            or len(ids) > 200
            or any(not isinstance(i, int) for i in ids)):
        return JsonResponse({"error": "Provide 1-200 flag ids."},
                            status=400)
    note = (data.get("note") or "").strip()
    flags = list(Flag.objects.filter(id__in=ids).select_related("user"))
    reviewed, skipped = 0, 0
    for flag in flags:
        if flag.status != "open":
            skipped += 1
            continue
        flag.status = "reviewed"
        flag.note = note
        flag.reviewed_by = request.user
        flag.save(update_fields=["status", "note", "reviewed_by"])
        _moderation_log(request.user, "flag_reviewed",
                        username=flag.user.username,
                        details={"flag_id": flag.id,
                                 "rule": flag.rule_code,
                                 "bulk": True},
                        note=note)
        reviewed += 1
    return JsonResponse({"status": "ok", "reviewed": reviewed,
                         "skipped": skipped})


@require_http_methods(["POST"])
def ascent_void_api(request, ascent_id):
    """Void (or restore) one ascent: voided ascents stay in logs but
    score nothing and leave every leaderboard. Reason required to
    void; restoring offers a note. Logged + audited."""
    from .anticheat import refresh_user_flags
    _staff_or_404(request)
    ascent = get_object_or_404(
        Ascent.objects.select_related("climb", "user"), id=ascent_id)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    void = data.get("void", True)
    void = void not in (False, "false", "0", 0, "no")
    note = (data.get("reason") or data.get("note") or "").strip()
    if void and not note:
        return JsonResponse(
            {"error": "A reason is required to void a send."}, status=400)
    ascent.is_voided = void
    ascent.void_reason = note if void else ""
    ascent.save(update_fields=["is_voided", "void_reason"])
    _audit_ascent(ascent.user, ascent, ascent.climb, request.user,
                  "void" if void else "unvoid",
                  old_tries=ascent.tries, new_tries=ascent.tries,
                  old_grade=ascent.climb.grade,
                  new_grade=ascent.climb.grade,
                  old_points=None if void else 0,
                  new_points=0 if void else ascent.points,
                  note=note)
    _moderation_log(request.user, "void" if void else "unvoid",
                    username=ascent.user.username,
                    details={"ascent_id": ascent.id,
                             "climb_id": ascent.climb_id},
                    note=note)
    refresh_user_flags(ascent.user)
    return JsonResponse({"status": "ok", "voided": ascent.is_voided})


def user_log_view(request, username):
    """Admin-only full climb log for one user, newest first. Columns:
    date/time (stored UTC, shown Europe/London), climb name/id, wall,
    colour swatch + name, official grade, tries, points, voided
    status, flags tied to the ascent. Filters: wall, date range,
    flagged, voided. Totals on top, audit history below, CSV export.
    Staff only (404 otherwise)."""
    from zoneinfo import ZoneInfo

    from django.http import HttpResponse

    from .anticheat import suspicion_score
    from .models import AscentAudit
    _staff_or_404(request)
    user = get_object_or_404(get_user_model(), username=username)
    ascents = (user.ascents.select_related("climb")
               .prefetch_related("flags").order_by("-logged_at", "-id"))
    wall = request.GET.get("wall", "all")
    if wall in [w for w, _ in WALL_CHOICES]:
        ascents = ascents.filter(climb__wall=wall)
    else:
        wall = "all"
    date_from = (request.GET.get("from") or "").strip()
    date_to = (request.GET.get("to") or "").strip()
    from datetime import datetime
    if date_from:
        try:
            ascents = ascents.filter(
                logged_at__date__gte=datetime.strptime(
                    date_from, "%Y-%m-%d").date())
        except ValueError:
            date_from = ""
    if date_to:
        try:
            ascents = ascents.filter(
                logged_at__date__lte=datetime.strptime(
                    date_to, "%Y-%m-%d").date())
        except ValueError:
            date_to = ""
    flagged = request.GET.get("flagged", "all")
    if flagged == "flagged":
        ascents = ascents.filter(flags__isnull=False).distinct()
    elif flagged == "unflagged":
        ascents = ascents.filter(flags__isnull=True)
    else:
        flagged = "all"
    voided = request.GET.get("voided", "all")
    if voided == "voided":
        ascents = ascents.filter(is_voided=True)
    elif voided == "active":
        ascents = ascents.filter(is_voided=False)
    else:
        voided = "all"
    ascents = list(ascents)
    london = ZoneInfo("Europe/London")
    for ascent in ascents:
        ascent.local_time = ascent.logged_at.astimezone(london)
    live = [a for a in ascents if not a.is_voided]
    totals = {
        "sends": len(live),
        "points": sum(a.points or 0 for a in live),
        "flashes": sum(1 for a in live if a.tries == 1),
        "voided": sum(1 for a in ascents if a.is_voided),
        "suspicion": suspicion_score(user),
    }
    audits = list(AscentAudit.objects.filter(user=user)
                  .select_related("climb")[:100])
    for audit in audits:
        audit.local_time = audit.created_at.astimezone(london)
    if request.GET.get("format") == "csv":
        import csv
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="climb-log-{user.username}.csv"')
        writer = csv.writer(response)
        writer.writerow(["logged_at", "climb_id", "climb_name", "wall",
                         "colour", "grade", "tries", "points", "voided",
                         "void_reason", "flags"])
        for ascent in ascents:
            writer.writerow([
                ascent.local_time.strftime("%Y-%m-%d %H:%M"),
                ascent.climb_id,
                ascent.climb.name or ascent.climb.colour_name,
                ascent.climb.get_wall_display(),
                ascent.climb.colour_name,
                ascent.climb.grade,
                ascent.tries,
                ascent.points,
                "yes" if ascent.is_voided else "no",
                ascent.void_reason,
                "; ".join(
                    f"{f.rule_code} ({f.severity}/{f.status})"
                    for f in ascent.flags.all()),
            ])
        return response
    return render(request, "climbs/user_log.html", {
        "log_user": user,
        "ascents": ascents,
        "totals": totals,
        "audits": audits,
        "wall": wall,
        "wall_choices": [("all", "All walls")] + list(WALL_CHOICES),
        "date_from": date_from,
        "date_to": date_to,
        "flagged": flagged,
        "voided": voided,
    })


@require_http_methods(["POST"])
def user_hide_api(request, username):
    """Hide a user from public leaderboards while under review (or
    restore them). Note optional; every action is logged."""
    _staff_or_404(request)
    user = get_object_or_404(get_user_model(), username=username)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    hidden = data.get("hidden", True)
    hidden = hidden not in (False, "false", "0", 0, "no")
    note = (data.get("note") or "").strip()
    user.leaderboard_hidden = hidden
    user.save(update_fields=["leaderboard_hidden"])
    _moderation_log(request.user, "hide" if hidden else "unhide",
                    username=user.username, note=note)
    return JsonResponse({"status": "ok", "hidden": user.leaderboard_hidden})


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
    doomed = list(Ascent.objects.filter(
        climb__climb_set=climb_set, user=user).select_related("climb"))
    try:
        removed, _ = Ascent.objects.filter(
            climb__climb_set=climb_set, user=user).delete()
    except Exception:
        return JsonResponse(
            {"error": "Could not remove that climber."}, status=400)
    for ascent in doomed:
        _audit_ascent(user, None, ascent.climb, request.user, "delete",
                      old_tries=ascent.tries,
                      old_grade=ascent.climb.grade,
                      old_points=ascent.points,
                      note=f"Removed from set {climb_set.label}")
    _moderation_log(request.user, "remove_from_set", username=username,
                    details={"set_id": climb_set.id, "removed": removed})
    from .anticheat import refresh_user_flags
    refresh_user_flags(user)
    return JsonResponse({"status": "ok", "removed": removed})


@require_http_methods(["POST"])
def recalc_points_api(request):
    """Recompute every ascent's points from its stored grade and tries
    (official grade only). Fixes boards left stale by a scoring change.
    Tries and timestamps are never touched; only rows whose points
    differ are updated. Staff only (404 otherwise)."""
    from .scoring import calculate_points
    _staff_or_404(request)
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
    _moderation_log(request.user, "recalc_points",
                    details={"total": total, "changed": changed})
    return JsonResponse(
        {"status": "ok", "total": total, "changed": changed})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def climb_create_api(request):
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    wall = data.get("wall")
    grade = data.get("grade")
    tag = data.get("tag")
    if tag is None and grade in GRADE_VALUES:
        # Grade-only posts keep working: wear the grade's own band.
        tag = TAGS_BY_GRADE[grade][0]
    if tag is None:
        tag = "white"
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
    pair_error = _tag_grade_error(tag, grade)
    if pair_error:
        return JsonResponse({"error": pair_error}, status=400)
    try:
        x = float(data["x_percent"])
        y = float(data["y_percent"])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "x_percent/y_percent required."}, status=400)
    # Markers live inside the wall bounds (minus the marker radius):
    # anything outside is clamped back in, never stored out of view.
    x, y = clamp_to_wall(x, y)
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
    if "wall" in data and data["wall"] not in WALL_VALUES:
        return JsonResponse({"error": "Unknown wall."}, status=400)
    if "grade" in data or "tag" in data:
        # Revealing a mystery climb is just an update: set its real
        # tag and grade together and voting unlocks on its own.
        pair_error = _tag_grade_error(
            data.get("tag", climb.tag), data.get("grade", climb.grade))
        if pair_error:
            return JsonResponse({"error": pair_error}, status=400)
    for field in ("x_percent", "y_percent"):
        # Marker drags send numbers; anything else would blow up on
        # save, so reject it here with a 400 instead of a 500.
        if field in data:
            try:
                data[field] = float(data[field])
            except (TypeError, ValueError):
                return JsonResponse(
                    {"error": f"{field} must be a number."}, status=400)
    if "x_percent" in data or "y_percent" in data:
        # A drag past the photo edge clamps back inside (minus the
        # marker radius) instead of stranding the marker off-map.
        data["x_percent"], data["y_percent"] = clamp_to_wall(
            data.get("x_percent", climb.x_percent),
            data.get("y_percent", climb.y_percent))
        climb.x_percent = data["x_percent"]
        climb.y_percent = data["y_percent"]
    for field in ("name", "grade", "tag", "colour", "wall",
                  "is_active"):
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


NEWS_PER_PAGE = 5


def news_view(request):
    """Gym news, newest first, five to a page with buttons back."""
    posts = NewsPost.objects.select_related("author").all()
    page = Paginator(posts, NEWS_PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "climbs/news.html", {"page": page})


def _news_payload(request):
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, JsonResponse({"error": "Invalid data."}, status=400)
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not title or not body:
        return None, JsonResponse(
            {"error": "Title and body required."}, status=400)
    if len(title) > 200 or len(body) > 5000:
        return None, JsonResponse(
            {"error": "Title or body too long."}, status=400)
    return {"title": title, "body": body}, None


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def news_create_api(request):
    fields, error = _news_payload(request)
    if error:
        return error
    post = NewsPost.objects.create(author=request.user, **fields)
    return JsonResponse({"id": post.id})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def news_update_api(request, post_id):
    post = get_object_or_404(NewsPost, id=post_id)
    fields, error = _news_payload(request)
    if error:
        return error
    post.title = fields["title"]
    post.body = fields["body"]
    post.save(update_fields=["title", "body"])
    return JsonResponse({"status": "ok"})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def news_delete_api(request, post_id):
    NewsPost.objects.filter(id=post_id).delete()
    return JsonResponse({"status": "deleted"})


class SignupView(CreateView):
    form_class = SignupForm
    template_name = "registration/signup.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        return response

    def get_success_url(self):
        return reverse_lazy("climbs:map")
