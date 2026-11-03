import json

from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import user_passes_test
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Avg, Count, Exists, OuterRef, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.timezone import now
from django.views.decorators.http import require_http_methods
from django.views.generic import CreateView

from .forms import SignupForm
from .models import (
    Ascent,
    Climb,
    ClimbSet,
    Comment,
    FORUM_COMMENT_MAX_LENGTH,
    ForumComment,
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
from .services import reveal_due_mystery_climbs

MAP_WIDTH = 256    # overlay viewBox width (see models.WALL_MAP_WIDTH)
MAP_HEIGHT = 512   # overlay viewBox height (see models.WALL_MAP_HEIGHT)

GRADE_VALUES = [g for g, _ in Climb.GRADE_CHOICES]
WALL_VALUES = [w for w, _ in WALL_CHOICES]


def _selected_wall_set(wall, requested):
    """picks the set to show for a wall. use the one in the url if its
    real, otherwise the live set, otherwise the newest one"""
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
    # mystery climbs from a week ago reveal when someone loads the map
    reveal_due_mystery_climbs()
    # we need the ids the user has sent so the marker can show green
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
    # ?climb=id jumps to the set that climb is in, otherwise the marker
    # is not on the wall
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
    """rows for the public board. voided sends and hidden users are
    left out. on the live set removed climbs come off the board too,
    old sets keep all there history"""
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
    """write one row to the send audit table"""
    from .models import AscentAudit
    return AscentAudit.objects.create(
        user=user, ascent=ascent, climb=climb, actor=actor, action=action,
        old_tries=old_tries, new_tries=new_tries,
        old_grade=old_grade, new_grade=new_grade,
        old_points=old_points, new_points=new_points, note=note)


def _deal_positions(rows, key="total_points"):
    """give each row a position. same score gets the same place, so it
    goes 1, 2, 2, 4 like a comp"""
    last_score, last_rank = None, 0
    for index, row in enumerate(rows, start=1):
        if row[key] != last_score:
            last_score, last_rank = row[key], index
        row["position"] = last_rank
    return rows


def _set_date_range(wall_sets, climb_set):
    """start and end date for a set. it runs from when it was made till
    the next set on that wall. end is none for the newest set"""
    if climb_set is None:
        return (None, None)
    key = (climb_set.created_at, climb_set.id)
    later = [s.created_at for s in wall_sets
             if (s.created_at, s.id) > key]
    return (climb_set.created_at, min(later) if later else None)


def _set_stats(climb_set):
    """stats for one set, best rated, popular, votes, totals and the
    grade graph. none if the set is empty"""
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
    # softest and hardest come from grade votes. need 2 votes before it
    # shows anything
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
        # mystery sends go under ? so the hidden grade cant be worked out
        # from the graph
        grade_key = '?' if climb.tag == 'mystery' else climb.grade
        sends_by_grade[grade_key] = sends_by_grade.get(grade_key, 0) + 1
        tag = climb.tag if climb.tag in TAG_VALUES else 'mystery'
        key = (grade_key, tag)
        sends_by_grade_tag[key] = sends_by_grade_tag.get(key, 0) + 1
    grades = sorted({('?' if c.tag == 'mystery' else c.grade) for c in climbs},
                    key=lambda g: (grade_number(g) is None, grade_number(g) or 0))
    top = max([sends_by_grade.get(g, 0) for g in grades] + [0])

    def segments_for(grade):
        # each bit of the bar uses the tag on that climb
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


def _leaderboard_wall(request):
    """which wall the board should open on. ?wall= wins, then a set
    param, otherwise main wall"""
    wall = request.GET.get("wall")
    if wall in WALL_VALUES:
        return wall
    for value, _display in WALL_CHOICES:
        if request.GET.get(f"{value}_set"):
            return value
    return "main"


def leaderboard_view(request):
    # reveal first so the board gets the new points
    reveal_due_mystery_climbs()
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
    return render(request, "climbs/leaderboard.html", {
        "walls": walls,
        "selected_wall": _leaderboard_wall(request),
    })


def _tag_grade_error(tag, grade):
    """error text if the tag and grade dont match, otherwise none.
    mystery can use any grade"""
    if tag not in TAG_VALUES:
        return "Unknown tag."
    if grade not in GRADE_VALUES:
        return "Unknown grade."
    if not tag_supports_grade(tag, grade):
        allowed = ", ".join(grades_for_tag(tag))
        return f"That tag covers grades {allowed}."
    return None


def _grade_vote_counts(climb):
    """count the grade votes as harder, at or softer"""
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
    reveal_due_mystery_climbs()
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
    """one users log on one set, newest first. used when you tap a name
    on the board. only normal sends, no flags or voids. hidden users
    are private unless you are staff"""
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
    """small profile for the popup when you tap a name"""
    user = get_object_or_404(get_user_model(), username=username)
    totals = user.ascents.filter(is_voided=False).aggregate(
        sends=Count("id"), points=Sum("points"))
    best = None
    # no mystery climbs here, the hidden grade would leak. voided sends
    # dont count either
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
        # undo a send
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
    # stop people spamming sends
    from .anticheat import rate_limited, refresh_user_flags
    if rate_limited(request.user):
        return JsonResponse(
            {"error": "Too many sends logged lately — try again later."},
            status=429)
    # the real try count is saved. the exists check above can race a
    # double tap so the unique constraint is the backup
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
    """picks the wall and set for the admin tabs"""
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
    """group flags with the same rule so 15 flashes show as one line,
    the single flags are still under it"""
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
    """staff board with the anticheat review bits and filters"""
    from .models import Flag
    wall, sets, active, selected = _admin_selection(request)
    board = []
    if selected is not None:
        board_ascents = Ascent.objects.filter(
            climb__climb_set=selected, is_voided=False)
        if selected.is_active:
            # same as the public board, removed climbs dont count while
            # the set is live
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
            # only show flags for sends on this set. flags with no send
            # are whole history so they show on any set
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
            # the status filter hides closed flags too. if that leaves
            # nothing shown fall back to all of them
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
            # group the same rule flags so the row is not huge
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
    """staff only. normal users get a 404 so they dont even know this
    page exists"""
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
    """review one flag. dismiss and confirm need a note, reviewed does
    not. every action gets logged"""
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
    """mark a load of flags as reviewed at once. bulk only does
    reviewed, dismiss and confirm stay one at a time so the note
    belongs to one flag. closed flags are skipped"""
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
    """void or restore one send. void needs a reason. both get logged"""
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
    """full climb log for one user, staff only. has filters, totals,
    audit history and a csv download"""
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
    """hide or unhide a user from the public boards. logged either way"""
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
    """make this set the live one and put the other sets on that wall
    to sleep"""
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
    """remove a climber from one set. deletes there sends on that set,
    ratings and comments stay"""
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
    """recalculate every send from the stored grade and tries. fixes
    stale points after a scoring change. only points get updated"""
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
        # no tag sent, use the normal tag for that grade
        tag = TAGS_BY_GRADE[grade][0]
    if tag is None:
        tag = "white"
    climb_set = None
    if data.get("climb_set") is not None:
        # making the climb straight into a set, the set wall wins
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
    # keep the marker on the wall
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
        # revealing a mystery is done here, set tag and grade together
        pair_error = _tag_grade_error(
            data.get("tag", climb.tag), data.get("grade", climb.grade))
        if pair_error:
            return JsonResponse({"error": pair_error}, status=400)
    for field in ("x_percent", "y_percent"):
        # dragging sends numbers. anything else would 500 on save
        if field in data:
            try:
                data[field] = float(data[field])
            except (TypeError, ValueError):
                return JsonResponse(
                    {"error": f"{field} must be a number."}, status=400)
    if "x_percent" in data or "y_percent" in data:
        # dont let a drag put the marker off the wall
        data["x_percent"], data["y_percent"] = clamp_to_wall(
            data.get("x_percent", climb.x_percent),
            data.get("y_percent", climb.y_percent))
        climb.x_percent = data["x_percent"]
        climb.y_percent = data["y_percent"]
    previous_wall = climb.wall
    previous_grade = climb.grade
    previous_tag = climb.tag
    for field in ("name", "grade", "tag", "colour", "wall",
                  "is_active"):
        if field in data:
            setattr(climb, field, data[field])
    if "wall" in data and data["wall"] != previous_wall:
        # only a real wall change moves the climb to the new live set.
        # a normal edit must not pull it out of a draft set
        climb.climb_set = _active_set_for(data["wall"])
    climb.save()
    if climb.grade != previous_grade or climb.tag != previous_tag:
        # grade or tag changed so every send is worth something else now
        from .scoring import calculate_points
        for ascent in climb.ascents.all():
            fresh = calculate_points(
                climb.grade, ascent.tries, climb.tag)
            if ascent.points != fresh:
                Ascent.objects.filter(pk=ascent.pk).update(points=fresh)
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
    """start a new set. it stays a draft until someone makes it current.
    the first set on a wall goes live straight away"""
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
    # first set on a wall has to go live or there is nothing to log sends
    # against
    first = not ClimbSet.objects.filter(wall=wall).exists()
    climb_set = ClimbSet.objects.create(
        wall=wall, label=label, is_active=first)
    return JsonResponse({"id": climb_set.id, "label": climb_set.label})


FORUM_PER_PAGE = 8
FORUM_SORTS = {
    # pinned posts always sit above the chosen order
    "new": ("-is_pinned", "-created_at"),
    "old": ("-is_pinned", "created_at"),
    "popular": ("-is_pinned", "-comment_count", "-like_count",
                "-created_at"),
}


def _forum_sort(request):
    """new, old or popular. unknown values fall back to new"""
    sort = request.GET.get("sort", "new")
    return sort if sort in FORUM_SORTS else "new"


def _forum_posts(request):
    """posts with like and comment totals, plus whether this viewer
    liked each one. one query for the list"""
    posts = (NewsPost.objects.select_related("author")
             .annotate(like_count=Count("likes", distinct=True),
                       comment_count=Count(
                           "forum_comments", distinct=True)))
    if request.user.is_authenticated:
        posts = posts.annotate(is_liked=Exists(
            NewsPost.likes.through.objects.filter(
                newspost_id=OuterRef("pk"), user_id=request.user.id)))
    return posts


def _forum_comments(request, post):
    """every comment on a post, oldest first, with like totals"""
    comments = (post.forum_comments.select_related(
        "user", "reply_to", "parent__user")
        .annotate(like_count=Count("likes", distinct=True)))
    if request.user.is_authenticated:
        comments = comments.annotate(is_liked=Exists(
            ForumComment.likes.through.objects.filter(
                forumcomment_id=OuterRef("pk"),
                user_id=request.user.id)))
    return comments.order_by("created_at")


def _comment_tree(comments):
    """turn the flat oldest-first list into reddit-style threads.
    replies keep the same oldest-first order under their parent"""
    by_parent = {}
    for comment in comments:
        comment.child_comments = []
        by_parent.setdefault(comment.parent_id, []).append(comment)

    def attach(comment):
        comment.child_comments = [
            attach(child) for child in by_parent.get(comment.id, [])
        ]
        return comment

    return [attach(comment) for comment in by_parent.get(None, [])]


def forum_view(request):
    """the forum: everyone can post, pinned posts sit on top, and the
    list can run newest, oldest or most discussed first"""
    sort = _forum_sort(request)
    posts = _forum_posts(request).order_by(*FORUM_SORTS[sort])
    page = Paginator(posts, FORUM_PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "climbs/forum.html", {
        "page": page,
        "sort": sort,
        "sort_choices": [
            ("new", "Newest"), ("old", "Oldest"),
            ("popular", "Popular"),
        ],
    })


def news_redirect(request):
    """old /news/ links land on the forum"""
    return redirect("climbs:forum", permanent=True)


def forum_post_view(request, post_id):
    post = get_object_or_404(_forum_posts(request), id=post_id)
    comments = list(_forum_comments(request, post))
    return render(request, "climbs/forum_post.html", {
        "post": post,
        "comments": _comment_tree(comments),
        "comment_count": post.comment_count,
        "comment_max_length": FORUM_COMMENT_MAX_LENGTH,
        "back_sort": _forum_sort(request),
    })


def _forum_payload(request):
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, JsonResponse({"error": "Invalid data."}, status=400)
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not body:
        return None, JsonResponse(
            {"error": "Write something first."}, status=400)
    if len(title) > 200 or len(body) > 5000:
        return None, JsonResponse(
            {"error": "Keep the title under 200 and the post under 5000 "
                      "characters."}, status=400)
    return {"title": title, "body": body}, None


def _can_manage_post(user, post):
    """staff can manage everything, authors can manage their own post"""
    return user.is_authenticated and (
        user.is_staff or post.author_id == user.id)


@require_http_methods(["POST"])
def forum_create_api(request):
    denied = _login_required_json(request)
    if denied:
        return denied
    fields, error = _forum_payload(request)
    if error:
        return error
    post = NewsPost.objects.create(author=request.user, **fields)
    return JsonResponse({
        "id": post.id,
        "url": reverse("climbs:forum-post", args=[post.id]),
    })


@require_http_methods(["POST"])
def forum_update_api(request, post_id):
    post = get_object_or_404(NewsPost, id=post_id)
    if not _can_manage_post(request.user, post):
        return JsonResponse({"error": "Not allowed."}, status=403)
    fields, error = _forum_payload(request)
    if error:
        return error
    post.title = fields["title"]
    post.body = fields["body"]
    post.save(update_fields=["title", "body", "updated_at"])
    return JsonResponse({"status": "ok"})


@require_http_methods(["POST"])
def forum_delete_api(request, post_id):
    post = get_object_or_404(NewsPost, id=post_id)
    if not _can_manage_post(request.user, post):
        return JsonResponse({"error": "Not allowed."}, status=403)
    post.delete()
    return JsonResponse({"status": "deleted"})


@user_passes_test(staff_check)
@require_http_methods(["POST"])
def forum_pin_api(request, post_id):
    post = get_object_or_404(NewsPost, id=post_id)
    post.is_pinned = not post.is_pinned
    post.save(update_fields=["is_pinned"])
    return JsonResponse({"pinned": post.is_pinned})


def _toggle_like(manager, user):
    """add or remove one like. returns (liked, new total)"""
    if manager.filter(pk=user.pk).exists():
        manager.remove(user)
        liked = False
    else:
        manager.add(user)
        liked = True
    return liked, manager.count()


@require_http_methods(["POST"])
def forum_post_like_api(request, post_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    post = get_object_or_404(NewsPost, id=post_id)
    liked, count = _toggle_like(post.likes, request.user)
    return JsonResponse({"liked": liked, "count": count})


@require_http_methods(["POST"])
def forum_comment_create_api(request, post_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    post = get_object_or_404(NewsPost, id=post_id)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid data."}, status=400)
    text = (data.get("text") or "").strip()
    if not text or len(text) > FORUM_COMMENT_MAX_LENGTH:
        return JsonResponse(
            {"error": "Comment must be between 1 and "
                      f"{FORUM_COMMENT_MAX_LENGTH} characters."},
            status=400)
    parent = None
    parent_id = data.get("parent")
    if parent_id not in (None, "", 0, "0"):
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            return JsonResponse(
                {"error": "That comment is not on this post."}, status=400)
        parent = (ForumComment.objects
                  .filter(id=parent_id, post=post)
                  .select_related("user").first())
        if parent is None:
            return JsonResponse(
                {"error": "That comment is not on this post."}, status=400)
    reply_to = parent.user if parent else None
    if parent is None:
        # old clients could still send a plain @mention with no parent
        reply_name = (data.get("reply_to") or "").strip().lstrip("@")
        if reply_name:
            reply_to = get_user_model().objects.filter(
                username__iexact=reply_name).first()
            if reply_to is None:
                return JsonResponse(
                    {"error": "That climber does not exist."}, status=400)
    comment = ForumComment.objects.create(
        post=post, user=request.user, text=text, parent=parent,
        reply_to=reply_to)
    return JsonResponse({
        "id": comment.id,
        "user": comment.user.username,
        "text": comment.text,
        "parent": comment.parent_id,
        "reply_to": comment.reply_to.username if comment.reply_to else None,
        "created_at": comment.created_at.isoformat(),
    })


@require_http_methods(["POST"])
def forum_comment_like_api(request, comment_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    comment = get_object_or_404(ForumComment, id=comment_id)
    liked, count = _toggle_like(comment.likes, request.user)
    return JsonResponse({"liked": liked, "count": count})


@require_http_methods(["POST"])
def forum_comment_delete_api(request, comment_id):
    denied = _login_required_json(request)
    if denied:
        return denied
    comment = get_object_or_404(ForumComment, id=comment_id)
    if comment.user_id != request.user.id and not request.user.is_staff:
        return JsonResponse({"error": "Not allowed."}, status=403)
    comment.delete()
    return JsonResponse({"status": "deleted"})


def manifest_view(request):
    """the pwa manifest. icons come from staticfiles so the hashed prod
    names work. related_applications lets the install buttons know if
    the app is already installed"""
    from django.contrib.staticfiles.storage import staticfiles_storage
    from django.urls import reverse
    icons = [
        ("climbs/icons/icon-192.png", "192x192", "any"),
        ("climbs/icons/icon-512.png", "512x512", "any"),
        ("climbs/icons/icon-maskable-512.png", "512x512", "maskable"),
        ("climbs/icons/apple-touch-icon.png", "180x180", "any"),
    ]
    payload = {
        "name": "Summit Map",
        "short_name": "Summit Map",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#070c13",
        "theme_color": "#0e1622",
        "orientation": "portrait-primary",
        "related_applications": [
            {
                "platform": "webapp",
                "url": request.build_absolute_uri(
                    reverse("climbs:manifest")),
            },
        ],
        "icons": [
            {
                "src": staticfiles_storage.url(path),
                "sizes": sizes,
                "type": "image/png",
                **({"purpose": purpose} if purpose else {}),
            }
            for path, sizes, purpose in icons
        ],
    }
    return JsonResponse(payload, content_type="application/manifest+json")


SERVICE_WORKER_JS = """\
const CACHE = "summit-map-v4";
// only cache static files from our own site. never cache html because
// it might have someones private data. skip non get and other origins
self.addEventListener("install", (event) => {
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === "navigate") return;
  if (
    url.pathname === "/manifest.webmanifest" ||
    url.pathname.indexOf("/static/") === 0
  ) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(req).then(
          (hit) =>
            hit ||
            fetch(req).then((res) => {
              if (res && res.ok) cache.put(req, res.clone());
              return res;
            })
        )
      )
    );
  }
});
"""


def service_worker_view(request):
    """serves sw.js from django so the scope stays / and updates dont
    get a hashed name"""
    response = HttpResponse(
        SERVICE_WORKER_JS, content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


class SignupView(CreateView):
    form_class = SignupForm
    template_name = "registration/signup.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        return response

    def get_success_url(self):
        return reverse_lazy("climbs:map")
