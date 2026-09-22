"""Anti-cheat flagging (staff review only, never auto-punishing).

Every threshold lives in the ONE ``ANTI_CHEAT`` dict in settings.
Statistical (peer-comparison) rules only fire once
``min_users_for_peer_stats`` users exist; below that the absolute
thresholds alone apply.

Entry points:

* :func:`refresh_user_flags` — delete a user's open flags and
  regenerate them. Called on ascent create/delete and by the
  ``recompute_flags`` management command.
* :func:`recompute_all` — refresh every user with ascents.
* :func:`suspicion_score` — sum of a user's open flag points, capped.
* :func:`rate_limited` — basic ascent-logging rate limit.
"""

import math
import statistics
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Sum
from django.utils.timezone import now

from .models import Ascent, AscentAudit, Flag, grade_number

LONDON = ZoneInfo("Europe/London")

RULE_HIGH_FLASH_RATE = "high_flash_rate"
RULE_BIG_GRADE_FLASH = "big_grade_flash"
RULE_FLASH_VS_COMMUNITY = "flash_vs_community"
RULE_TRIES_VS_PEERS = "tries_vs_peers"
RULE_IMPOSSIBLE_PACE = "impossible_pace"
RULE_BULK_OR_ODD_HOURS = "bulk_or_odd_hours"
RULE_TRIES_EDITED_DOWN = "tries_edited_down"
RULE_RAPID_RANK_JUMP = "rapid_rank_jump"
RULE_SEND_RATE_OUTLIER = "send_rate_outlier"
RULE_GRADE_PACE_FLOOR = "grade_pace_floor"
RULE_FLASH_DRIFT = "flash_drift"
RULE_GRADE_JUMP_VELOCITY = "grade_jump_velocity"
RULE_SESSION_CLUSTERING = "session_clustering"
RULE_PEER_Z_SCORE = "peer_z_score"


def get_config():
    return dict(getattr(settings, "ANTI_CHEAT", {}))


def suspicion_score(user):
    """Sum of the user's OPEN flag points, capped at 100."""
    cfg = get_config()
    total = (Flag.objects.filter(user=user, status="open")
             .aggregate(total=Sum("points"))["total"] or 0)
    return min(total, cfg.get("suspicion_cap", 100))


def rate_limited(user):
    """True when the user already logged the hourly max (prevention)."""
    cfg = get_config()
    limit = cfg.get("ascent_rate_limit_per_hour", 30)
    since = now() - timedelta(hours=1)
    return Ascent.objects.filter(
        user=user, logged_at__gte=since).count() >= limit


def _live_ascents(user):
    return list(Ascent.objects.filter(
        user=user, is_voided=False).select_related("climb")
        .order_by("logged_at", "id"))


def _peer_user_count():
    return (Ascent.objects.filter(is_voided=False)
            .values("user_id").distinct().count())


def _make(user, rule, severity, points, evidence, ascent=None,
          ascent_id=None):
    if ascent is None and ascent_id is not None:
        ascent = Ascent.objects.filter(pk=ascent_id).first()
    return Flag(user=user, ascent=ascent, rule_code=rule,
                severity=severity, points=points, details=evidence)


def _rule_high_flash_rate(user, ascents, cfg, peers_ok):
    flags = []
    sends = [a for a in ascents if a.climb.tag != "mystery"]
    flashes = [a for a in sends if a.tries == 1]
    if len(sends) >= cfg.get("flash_rate_min_sends", 8):
        rate = len(flashes) / len(sends)
        if rate >= cfg.get("flash_rate_threshold", 0.90):
            flags.append(_make(
                user, RULE_HIGH_FLASH_RATE, "high", 30,
                {"summary": (
                    f"{len(flashes)}/{len(sends)} sends were flashes "
                    f"({rate:.0%}, threshold "
                    f"{cfg.get('flash_rate_threshold', 0.90):.0%})"),
                 "flashes": len(flashes), "sends": len(sends),
                 "rate": round(rate, 3)}))
            return flags
    if not peers_ok:
        return flags
    # Far above peers at the same grade, grade by grade.
    margin = cfg.get("flash_rate_peer_margin", 0.40)
    min_sends = cfg.get("flash_rate_min_grade_sends", 3)
    by_grade = {}
    for a in sends:
        by_grade.setdefault(a.climb.grade, []).append(a)
    for grade, mine in by_grade.items():
        if len(mine) < min_sends:
            continue
        peers = list(Ascent.objects.filter(
            climb__grade=grade, climb__tag__in=_non_mystery_tags(),
            is_voided=False).exclude(user=user))
        if len(peers) < 5:
            continue
        peer_rate = sum(1 for p in peers if p.tries == 1) / len(peers)
        my_rate = sum(1 for a in mine if a.tries == 1) / len(mine)
        if my_rate - peer_rate >= margin:
            mine_flashes = sum(1 for a in mine if a.tries == 1)
            flags.append(_make(
                user, RULE_HIGH_FLASH_RATE, "medium", 15,
                {"summary": (
                    f"{mine_flashes}/{len(mine)} sends at {grade} were "
                    f"flashes, peers avg {peer_rate:.0%}"),
                 "grade": grade, "flashes": mine_flashes,
                 "sends": len(mine),
                 "peer_rate": round(peer_rate, 3)}))
    return flags


def _non_mystery_tags():
    from .models import TAG_VALUES
    return [t for t in TAG_VALUES if t != "mystery"]


def _rule_big_grade_flash(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    gap = cfg.get("big_grade_gap", 2)
    high = cfg.get("big_grade_high", 6)
    best = None
    for a in ascents:
        number = grade_number(a.climb.grade)
        if number is None or a.climb.tag == "mystery":
            continue
        if a.tries == 1:
            if best is None:
                if number >= high:
                    flags.append(_make(
                        user, RULE_BIG_GRADE_FLASH, "high", 25,
                        {"summary": (
                            f"Flashed V{number} on an early send "
                            f"(first-send flash at V{high}+ needs review)"),
                         "grade": f"V{number}",
                         "ascent_id": a.id,
                         "climb_id": a.climb_id,
                         "logged_at": a.logged_at.isoformat()},
                        ascent=a))
            elif number - best >= gap:
                flags.append(_make(
                    user, RULE_BIG_GRADE_FLASH,
                    "high" if number - best >= gap + 1 else "medium",
                    25 if number - best >= gap + 1 else 15,
                    {"summary": (
                        f"Flashed V{number}, "
                        f"previous best non-flash send V{best}"),
                     "grade": f"V{number}",
                     "previous_best": f"V{best}",
                     "ascent_id": a.id, "climb_id": a.climb_id,
                     "logged_at": a.logged_at.isoformat()},
                    ascent=a))
        else:
            best = number if best is None else max(best, number)
    return flags


def _rule_flash_vs_community(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    min_ascents = cfg.get("community_flash_min_ascents", 5)
    low_rate = cfg.get("community_flash_low_rate", 0.20)
    repeat = cfg.get("community_flash_repeat", 2)
    mine = [a for a in ascents
            if a.tries == 1 and a.climb.tag != "mystery"]
    hits = []
    for a in mine:
        total = Ascent.objects.filter(
            climb=a.climb, is_voided=False).count()
        if total < min_ascents:
            continue
        flashes = Ascent.objects.filter(
            climb=a.climb, is_voided=False, tries=1).count()
        rate = flashes / total
        if rate < low_rate:
            hits.append((a, rate, flashes, total))
    if len(hits) >= repeat:
        flags.append(_make(
            user, RULE_FLASH_VS_COMMUNITY, "medium", 15,
            {"summary": (
                f"{len(hits)} flashes on climbs where the community "
                f"flash rate is under {low_rate:.0%} "
                f"(min {min_ascents} ascents)"),
             "climbs": [
                 {"climb_id": a.climb_id,
                  "name": a.climb.name or a.climb.colour,
                  "community_rate": round(rate, 3),
                  "community_flashes": flashes,
                  "community_ascents": total,
                  "ascent_id": a.id,
                  "logged_at": a.logged_at.isoformat()}
                 for a, rate, flashes, total in hits]}))
    return flags


def _rule_tries_vs_peers(user, ascents, cfg, peers_ok):
    if not peers_ok:
        return []
    flags = []
    min_shared = cfg.get("tries_peer_min_shared", 3)
    margin = cfg.get("tries_peer_margin", 2.0)
    climb_ids = [a.climb_id for a in ascents]
    shared = [a for a in ascents if Ascent.objects.filter(
        climb_id=a.climb_id, is_voided=False).exclude(
        user=user).exists()]
    if len(shared) < min_shared:
        return []
    mine_avg = sum(a.tries for a in shared) / len(shared)
    others = list(Ascent.objects.filter(
        climb_id__in=climb_ids, is_voided=False).exclude(user=user))
    if not others:
        return []
    others_avg = sum(a.tries for a in others) / len(others)
    if others_avg - mine_avg >= margin:
        flags.append(_make(
            user, RULE_TRIES_VS_PEERS, "medium", 15,
            {"summary": (
                f"Avg {mine_avg:.1f} tries across {len(shared)} shared "
                f"climbs vs others' avg {others_avg:.1f}"),
             "mine_avg": round(mine_avg, 2),
             "others_avg": round(others_avg, 2),
             "shared_climbs": len(shared)}))
    return flags


def _rule_impossible_pace(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    min_gap = cfg.get("minutes_per_attempt", 2.0) * 60
    for prev, cur in zip(ascents, ascents[1:]):
        gap = (cur.logged_at - prev.logged_at).total_seconds()
        plausible = max(cur.tries, 1) * min_gap
        if 0 <= gap < plausible:
            flags.append(_make(
                user, RULE_IMPOSSIBLE_PACE,
                "high" if gap < 60 else "medium",
                25 if gap < 60 else 15,
                {"summary": (
                    f"{cur.tries} tries on "
                    f"{cur.climb.get_wall_display()} logged "
                    f"{_fmt_gap(gap)} after "
                    f"{prev.climb.get_wall_display()} "
                    f"(plausible minimum {_fmt_gap(plausible)})"),
                 "ascent_id": cur.id, "climb_id": cur.climb_id,
                 "previous_ascent_id": prev.id,
                 "gap_seconds": round(gap),
                 "plausible_seconds": round(plausible),
                 "logged_at": cur.logged_at.isoformat()},
                ascent=cur))
    count = cfg.get("pace_count", 5)
    window = cfg.get("pace_window_minutes", 30) * 60
    for i, start in enumerate(ascents):
        inside = [a for a in ascents[i:]
                  if (a.logged_at - start.logged_at).total_seconds()
                  <= window]
        if len(inside) >= count:
            flags.append(_make(
                user, RULE_IMPOSSIBLE_PACE, "medium", 15,
                {"summary": (
                    f"{len(inside)} ascents logged within "
                    f"{cfg.get('pace_window_minutes', 30)} minutes"),
                 "window_ascents": len(inside),
                 "window_start": start.logged_at.isoformat()}))
            break
    return flags


def _fmt_gap(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _rule_bulk_or_odd_hours(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    bulk = cfg.get("bulk_per_minute", 3)
    by_minute = {}
    for a in ascents:
        key = a.logged_at.replace(second=0, microsecond=0)
        by_minute.setdefault(key, []).append(a)
    for minute, group in sorted(by_minute.items()):
        if len(group) >= bulk:
            flags.append(_make(
                user, RULE_BULK_OR_ODD_HOURS, "medium", 10,
                {"summary": (
                    f"{len(group)} ascents logged within a single minute "
                    f"({minute.strftime('%d/%m/%Y %H:%M')})"),
                 "count": len(group),
                 "minute": minute.isoformat(),
                 "ascent_ids": [a.id for a in group]}))
    start = cfg.get("gym_open_hour_start", 6)
    end = cfg.get("gym_open_hour_end", 23)
    odd = [a for a in ascents
           if not start <= a.logged_at.astimezone(LONDON).hour < end]
    if odd:
        shown = odd[:10]
        flags.append(_make(
            user, RULE_BULK_OR_ODD_HOURS, "low", 5,
            {"summary": (
                f"{len(odd)} ascents logged outside gym hours "
                f"({start}:00–{end}:00)"),
             "count": len(odd),
             "ascents": [
                 {"ascent_id": a.id, "climb_id": a.climb_id,
                  "logged_at": a.logged_at.isoformat()}
                 for a in shown]}))
    return flags


def _rule_tries_edited_down(user, ascents, cfg, peers_ok):
    del ascents, peers_ok
    flags = []
    high_pts = cfg.get("edit_down_rank_points", 20)
    low_pts = cfg.get("edit_down_points", 10)
    edits = (AscentAudit.objects.filter(
        user=user, action="edit", new_tries__isnull=False,
        old_tries__isnull=False))
    for audit in edits:
        if audit.new_tries >= audit.old_tries:
            continue
        gained = (audit.new_points or 0) - (audit.old_points or 0)
        flags.append(_make(
            user, RULE_TRIES_EDITED_DOWN,
            "high" if gained > 0 else "medium",
            high_pts if gained > 0 else low_pts,
            {"summary": (
                f"Tries edited {audit.old_tries} → {audit.new_tries} "
                f"({gained:+d} pts) on climb {audit.climb_id}"),
             "ascent_id": audit.ascent_id,
             "climb_id": audit.climb_id,
             "old_tries": audit.old_tries,
             "new_tries": audit.new_tries,
             "points_delta": gained,
             "edited_at": audit.created_at.isoformat()},
            ascent_id=audit.ascent_id))
    return flags


def _rule_rapid_rank_jump(user, ascents, cfg, peers_ok):
    flags = []
    cutoff = now() - timedelta(hours=24)
    day_points = sum(a.points or 0 for a in ascents
                     if a.logged_at >= cutoff)
    threshold = cfg.get("rank_jump_points_24h", 1500)
    if day_points >= threshold:
        if peers_ok:
            others = (Ascent.objects.filter(
                is_voided=False, logged_at__gte=cutoff).exclude(user=user)
                .values("user_id").annotate(total=Sum("points")))
            best_peer = max([row["total"] or 0 for row in others] + [0])
            if day_points >= 2 * max(best_peer, 1):
                severity, points = "high", 30
            else:
                severity, points = "medium", 15
            summary = (f"{day_points} pts in 24h "
                       f"(best peer {best_peer} pts)")
        else:
            severity, points = "medium", 15
            summary = f"{day_points} pts in 24h"
        flags.append(_make(
            user, RULE_RAPID_RANK_JUMP, severity, points,
            {"summary": summary, "points_24h": day_points}))
    # New account (< N days) reaching the top 3 of any active board.
    age_days = (now() - user.date_joined).days
    if age_days < cfg.get("new_account_days", 7):
        from .models import ClimbSet
        from .views import _deal_positions, _leaderboard_rows
        from .models import WALL_CHOICES
        for wall, display in WALL_CHOICES:
            climb_set = ClimbSet.active(wall)
            if climb_set is None:
                continue
            board = _deal_positions(_leaderboard_rows(climb_set))
            mine = next(
                (r for r in board
                 if r["user__username"] == user.username), None)
            if mine is not None and mine["position"] <= 3 and len(board) >= 2:
                flags.append(_make(
                    user, RULE_RAPID_RANK_JUMP, "high", 30,
                    {"summary": (
                        f"Account {age_days}d old at "
                        f"#{mine['position']} of {len(board)} on {display}"),
                     "wall": wall, "position": mine["position"],
                     "of": len(board)}))
                break
    return flags


def _day_key(dt):
    return dt.astimezone(LONDON).date()


def _sessions_by_day(ascents):
    """Group ascents into per-day sessions (London time)."""
    by_day = {}
    for a in ascents:
        by_day.setdefault(_day_key(a.logged_at), []).append(a)
    return by_day


def _percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0
    k = min(max(math.ceil(pct * len(ordered)) - 1, 0),
            len(ordered) - 1)
    return ordered[k]


def _session_rate(group):
    span = (group[-1].logged_at - group[0].logged_at).total_seconds()
    return len(group) / max(span / 60.0, 1.0)


def _rule_send_rate_outlier(user, ascents, cfg, peers_ok):
    flags = []
    min_sends = cfg.get("send_rate_min_sends", 5)
    own = []
    for day, group in sorted(_sessions_by_day(ascents).items()):
        if len(group) < min_sends:
            continue
        span = (group[-1].logged_at - group[0].logged_at).total_seconds()
        own.append((day, _session_rate(group), len(group), span))
    if not own:
        return flags
    threshold = None
    if peers_ok:
        rows = (Ascent.objects.filter(is_voided=False)
                .values_list("user_id", "logged_at"))
        dist = {}
        for uid, ts in rows:
            dist.setdefault((uid, _day_key(ts)), []).append(ts)
        rates = [len(g) / max((max(g) - min(g)).total_seconds() / 60.0, 1.0)
                 for g in dist.values() if len(g) >= min_sends]
        if len(rates) >= cfg.get("send_rate_min_sessions", 10):
            threshold = _percentile(
                rates, cfg.get("send_rate_percentile", 0.95))
    if threshold is None:
        threshold = cfg.get("send_rate_absolute_per_min", 2.0)
    for day, rate, count, span in own:
        if rate > threshold:
            flags.append(_make(
                user, RULE_SEND_RATE_OUTLIER,
                "high" if rate > 2 * threshold else "medium",
                25 if rate > 2 * threshold else 15,
                {"summary": (
                    f"{count} sends on {day.strftime('%d/%m/%Y')} at "
                    f"{rate:.1f}/min over {_fmt_gap(span)} "
                    f"(95th percentile {threshold:.1f}/min)"),
                 "day": day.isoformat(), "sends": count,
                 "rate_per_min": round(rate, 2),
                 "threshold_per_min": round(threshold, 2)}))
    return flags


def _rule_grade_pace_floor(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    base = cfg.get("grade_pace_base_minutes", 1.5) * 60
    per_grade = cfg.get("grade_pace_per_grade_minutes", 0.5) * 60
    for prev, cur in zip(ascents, ascents[1:]):
        g = grade_number(cur.climb.grade)
        if g is None:
            continue
        floor = max(cur.tries, 1) * (base + g * per_grade)
        gap = (cur.logged_at - prev.logged_at).total_seconds()
        if 0 <= gap < floor:
            flags.append(_make(
                user, RULE_GRADE_PACE_FLOOR,
                "high" if gap < 60 else "medium",
                25 if gap < 60 else 15,
                {"summary": (
                    f"{cur.climb.grade} in {cur.tries} tries logged "
                    f"{_fmt_gap(gap)} after the previous send "
                    f"(grade floor {_fmt_gap(floor)})"),
                 "ascent_id": cur.id, "climb_id": cur.climb_id,
                 "grade": cur.climb.grade, "tries": cur.tries,
                 "gap_seconds": round(gap),
                 "floor_seconds": round(floor),
                 "logged_at": cur.logged_at.isoformat()},
                ascent=cur))
    return flags


def _rule_flash_drift(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    window = cfg.get("flash_drift_window_sends", 10)
    min_hist = cfg.get("flash_drift_min_history", 10)
    sends = [a for a in ascents if a.climb.tag != "mystery"]
    if len(sends) < window + min_hist:
        return flags
    recent = sends[-window:]
    baseline = sends[:-window]

    def rate(rows):
        return sum(1 for a in rows if a.tries == 1) / len(rows)
    base, rec = rate(baseline), rate(recent)
    margin = cfg.get("flash_drift_margin", 0.35)
    if rec - base >= margin:
        span_days = ((recent[-1].logged_at - recent[0].logged_at)
                     .total_seconds() / 86400.0)
        burst = span_days <= cfg.get("flash_drift_burst_days", 7)
        big = rec - base >= 0.5
        flags.append(_make(
            user, RULE_FLASH_DRIFT,
            "high" if burst and big else "medium",
            25 if burst and big else 15,
            {"summary": (
                f"Flash rate {base:.0%} over {len(baseline)} sends, "
                f"now {rec:.0%} over the last {len(recent)} sends"
                + (f" inside {span_days:.1f} days" if burst else "")),
             "baseline_rate": round(base, 3),
             "recent_rate": round(rec, 3),
             "baseline_sends": len(baseline),
             "recent_sends": len(recent),
             "recent_span_days": round(span_days, 1),
             "burst": burst}))
    return flags


def _rule_grade_jump_velocity(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    days = cfg.get("grade_jump_days", 7)
    need = cfg.get("grade_jump_grades", 3)
    min_hist = cfg.get("grade_jump_min_history", 5)
    cutoff = now() - timedelta(days=days)
    graded = [(a.logged_at, grade_number(a.climb.grade))
              for a in ascents]
    graded = [(t, g) for t, g in graded if g is not None]
    old = [g for t, g in graded if t < cutoff]
    new = [g for t, g in graded if t >= cutoff]
    if len(old) < min_hist or not new:
        return flags
    jump = max(new) - max(old)
    if jump >= need:
        flags.append(_make(
            user, RULE_GRADE_JUMP_VELOCITY, "high", 30,
            {"summary": (
                f"Max grade V{max(old)} → V{max(new)} inside {days} days "
                f"(+{jump} grades after {len(old)} earlier sends)"),
             "prior_max": max(old), "recent_max": max(new),
             "jump": jump, "days": days,
             "history_sends": len(old)}))
    return flags


def _rule_session_clustering(user, ascents, cfg, peers_ok):
    del peers_ok
    flags = []
    min_sends = cfg.get("cluster_min_sends", 8)
    max_gap = cfg.get("cluster_median_gap_seconds", 90)
    for day, group in sorted(_sessions_by_day(ascents).items()):
        if len(group) < min_sends:
            continue
        gaps = sorted(
            (b.logged_at - a.logged_at).total_seconds()
            for a, b in zip(group, group[1:]))
        median = gaps[len(gaps) // 2]
        if median < max_gap:
            flags.append(_make(
                user, RULE_SESSION_CLUSTERING,
                "high" if median < 30 else "medium",
                25 if median < 30 else 15,
                {"summary": (
                    f"{len(group)} sends on {day.strftime('%d/%m/%Y')} "
                    f"with a median {_fmt_gap(median)} between logs "
                    f"(batch-logged, not climbed live)"),
                 "day": day.isoformat(), "sends": len(group),
                 "median_gap_seconds": round(median),
                 "ascent_ids": [a.id for a in group]}))
    return flags


def _rule_peer_z_score(user, ascents, cfg, peers_ok):
    flags = []
    if not peers_ok:
        return flags
    min_days = cfg.get("z_min_days", 5)
    own_days = _sessions_by_day(ascents)
    if len(own_days) < min_days:
        return flags
    user_mean = (sum(len(g) for g in own_days.values())
                 / len(own_days))
    rows = (Ascent.objects.filter(is_voided=False)
            .values_list("user_id", "logged_at"))
    per_visit = {}
    for uid, ts in rows:
        key = (uid, _day_key(ts))
        per_visit[key] = per_visit.get(key, 0) + 1
    counts = list(per_visit.values())
    if len(counts) < 10:
        return flags
    mean = statistics.mean(counts)
    stdev = statistics.stdev(counts) if len(counts) >= 2 else 0
    if stdev <= 0:
        return flags
    z = (user_mean - mean) / stdev
    threshold = cfg.get("z_threshold", 2.0)
    if z >= threshold:
        flags.append(_make(
            user, RULE_PEER_Z_SCORE, "medium", 15,
            {"summary": (
                f"{user_mean:.1f} sends/visit over "
                f"{len(own_days)} visits vs gym "
                f"{mean:.1f} ± {stdev:.1f} (z={z:.1f})"),
             "sends_per_visit": round(user_mean, 2),
             "gym_mean": round(mean, 2),
             "gym_stdev": round(stdev, 2),
             "z": round(z, 2),
             "visits": len(own_days)}))
    return flags


RULES = (
    (RULE_HIGH_FLASH_RATE, _rule_high_flash_rate),
    (RULE_BIG_GRADE_FLASH, _rule_big_grade_flash),
    (RULE_FLASH_VS_COMMUNITY, _rule_flash_vs_community),
    (RULE_TRIES_VS_PEERS, _rule_tries_vs_peers),
    (RULE_IMPOSSIBLE_PACE, _rule_impossible_pace),
    (RULE_BULK_OR_ODD_HOURS, _rule_bulk_or_odd_hours),
    (RULE_TRIES_EDITED_DOWN, _rule_tries_edited_down),
    (RULE_RAPID_RANK_JUMP, _rule_rapid_rank_jump),
    (RULE_SEND_RATE_OUTLIER, _rule_send_rate_outlier),
    (RULE_GRADE_PACE_FLOOR, _rule_grade_pace_floor),
    (RULE_FLASH_DRIFT, _rule_flash_drift),
    (RULE_GRADE_JUMP_VELOCITY, _rule_grade_jump_velocity),
    (RULE_SESSION_CLUSTERING, _rule_session_clustering),
    (RULE_PEER_Z_SCORE, _rule_peer_z_score),
)


def run_all_rules(user):
    """Evaluate every rule for one user; returns unsaved Flags."""
    cfg = get_config()
    ascents = _live_ascents(user)
    if not ascents and not AscentAudit.objects.filter(user=user).exists():
        return []
    peers_ok = _peer_user_count() >= cfg.get(
        "min_users_for_peer_stats", 5)
    found = []
    for _code, rule in RULES:
        try:
            found.extend(rule(user, ascents, cfg, peers_ok))
        except Exception:
            continue
    return found


def refresh_user_flags(user):
    """Delete the user's open flags and regenerate them. Reviewed,
    dismissed and confirmed flags are history and stay untouched."""
    Flag.objects.filter(user=user, status="open").delete()
    found = run_all_rules(user)
    Flag.objects.bulk_create(found)
    return found


def recompute_all():
    """Refresh flags for every user with ascents or audit history."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user_ids = set(Ascent.objects.values_list("user_id", flat=True))
    user_ids.update(AscentAudit.objects.values_list("user_id", flat=True))
    checked, created = 0, 0
    for user in User.objects.filter(id__in=user_ids):
        checked += 1
        created += len(refresh_user_flags(user))
    return checked, created
