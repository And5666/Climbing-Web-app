import json
import re

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import Ascent, Climb, ClimbSet, Comment, NewsPost
from .scoring import MYSTERY_FIXED_POINTS, calculate_points

User = get_user_model()


class ClimbTestMixin:
    def make_user(self, username, staff=False):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com",
            password="test-pass-123")
        if staff:
            user.is_staff = True
            user.save()
        return user

    def make_climb(self, name="Problem 1", grade="V4", wall="main",
                   x=50.0, y=100.0, tag="white"):
        return Climb.objects.create(
            name=name, grade=grade, tag=tag, colour="red", wall=wall,
            climb_set=ClimbSet.active(wall),
            x_percent=x, y_percent=y)


class DetailApiTests(ClimbTestMixin, TestCase):
    def test_detail_returns_full_shape(self):
        climb = self.make_climb()
        response = self.client.get(reverse("climbs:climb-detail",
                                           args=[climb.id]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["grade"], "V4")
        self.assertEqual(data["tag"], "white")
        self.assertEqual(data["wall_display"], "Main Wall")
        self.assertEqual(data["ascent_count"], 0)
        self.assertEqual(data["rating_count"], 0)
        self.assertEqual(data["grade_votes"]["total"], 0)
        self.assertEqual(data["comments"], [])
        self.assertFalse(data["viewer"]["authenticated"])

    def test_detail_lists_every_sender_newest_first(self):
        # The popup names everyone who sent it, newest first, with
        # the tries and points each one earned.
        climb = self.make_climb(grade="V3")
        users = [self.make_user(f"recent{i}") for i in range(5)]
        for i, user in enumerate(users):
            climb.ascents.create(user=user, tries=1,
                                 points=calculate_points("V3", 1))
        data = self.client.get(
            reverse("climbs:climb-detail", args=[climb.id])).json()
        self.assertEqual(len(data["recent_ascents"]), 5)
        self.assertEqual(
            [a["user"] for a in data["recent_ascents"]],
            ["recent4", "recent3", "recent2", "recent1", "recent0"])
        self.assertTrue(all(
            a["points"] == calculate_points("V3", 1) and a["tries"] == 1
            for a in data["recent_ascents"]))

    def test_detail_includes_viewer_state(self):
        user = self.make_user("viewer")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=2,
                             points=calculate_points("V4", 2))
        climb.ratings.create(user=user, value=4)
        climb.grade_suggestions.create(user=user, suggested_grade="V5")
        self.client.force_login(user)
        data = self.client.get(reverse("climbs:climb-detail",
                                       args=[climb.id])).json()
        self.assertTrue(data["viewer"]["authenticated"])
        self.assertEqual(data["viewer"]["tries"], 2)
        self.assertEqual(data["viewer"]["points"],
                         calculate_points("V4", 2))
        self.assertEqual(data["viewer"]["my_rating"], 4)
        self.assertEqual(data["viewer"]["my_grade"], "V5")


class AscentApiTests(ClimbTestMixin, TestCase):
    def test_anonymous_cannot_log_ascent(self):
        climb = self.make_climb()
        response = self.client.post(
            reverse("climbs:climb-ascent", args=[climb.id]),
            json.dumps({"tries": 3}), content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_log_ascent_scores_points(self):
        user = self.make_user("sender")
        climb = self.make_climb(grade="V4")
        self.client.force_login(user)
        response = self.client.post(
            reverse("climbs:climb-ascent", args=[climb.id]),
            json.dumps({"tries": 3}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["points"],
                         calculate_points("V4", 3))
        self.assertEqual(Ascent.objects.count(), 1)

    def test_undo_send_cycle(self):
        user = self.make_user("undosender")
        climb = self.make_climb(grade="V4")
        self.client.force_login(user)
        url = reverse("climbs:climb-ascent", args=[climb.id])
        response = self.client.post(url, json.dumps({"tries": 2}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200)
        # The send joins the filter's sent set while it stands...
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, f"new Set([{climb.id}])")
        # ...and leaves it when the send is undone.
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Ascent.objects.count(), 0)
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, "new Set([])")
        # Nothing left to undo.
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 404)
        # Logged-out undo attempts are refused.
        self.client.logout()
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 403)

    def test_relog_is_rejected_and_keeps_first_record(self):
        # Logged sends are final: a second log must not change the record.
        user = self.make_user("resender")
        climb = self.make_climb(grade="V4")
        self.client.force_login(user)
        url = reverse("climbs:climb-ascent", args=[climb.id])
        self.client.post(url, json.dumps({"tries": 5}),
                         content_type="application/json")
        response = self.client.post(url, json.dumps({"tries": 1}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        ascent = Ascent.objects.get(user=user, climb=climb)
        self.assertEqual(ascent.tries, 5)
        self.assertEqual(ascent.points, calculate_points("V4", 5))

    def test_double_tap_race_returns_400_not_500(self):
        # Two taps in flight can both pass the exists() check; the
        # unique (user, climb) constraint is the backstop, reported
        # the same way instead of a 500.
        from unittest import mock

        from django.db import IntegrityError

        user = self.make_user("doubletap")
        climb = self.make_climb(grade="V4")
        self.client.force_login(user)
        url = reverse("climbs:climb-ascent", args=[climb.id])
        with mock.patch.object(
                Ascent.objects, "create",
                side_effect=IntegrityError("duplicate")):
            response = self.client.post(
                url, json.dumps({"tries": 2}),
                content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Already logged", response.json()["error"])

    def test_tries_cap_at_five_plus(self):
        # Tries run 1–5 and the top step means 5+: anything higher is
        # rejected. Scoring already floors there, so 5 scores the
        # same as any higher count ever stored.
        user = self.make_user("capped")
        climb = self.make_climb(grade="V4")
        self.client.force_login(user)
        response = self.client.post(
            reverse("climbs:climb-ascent", args=[climb.id]),
            json.dumps({"tries": 5}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tries"], 5)
        self.assertEqual(response.json()["points"],
                         calculate_points("V4", 5))
        self.assertEqual(calculate_points("V4", 5),
                         calculate_points("V4", 9))
        ascent = Ascent.objects.get(user=user, climb=climb)
        self.assertEqual(ascent.tries, 5)
        for bad in (6, 9, 100):
            with self.subTest(tries=bad):
                other = self.make_climb(name=f"Over {bad}")
                denied = self.client.post(
                    reverse("climbs:climb-ascent", args=[other.id]),
                    json.dumps({"tries": bad}),
                    content_type="application/json")
                self.assertEqual(denied.status_code, 400)

    def test_bad_tries_rejected(self):
        user = self.make_user("badtry")
        climb = self.make_climb()
        self.client.force_login(user)
        for bad in (0, -3, 6):
            with self.subTest(tries=bad):
                response = self.client.post(
                    reverse("climbs:climb-ascent", args=[climb.id]),
                    json.dumps({"tries": bad}),
                    content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(Ascent.objects.count(), 0)

    def test_high_legacy_tries_display_as_five_plus(self):
        # Rows logged above 5 before the cap still display as 5+.
        # (The user log is staff-only.)
        user = self.make_user("legacy", staff=True)
        climb = self.make_climb()
        self.client.force_login(user)
        climb.ascents.create(user=user, tries=9,
                             points=calculate_points("V4", 9))
        response = self.client.get(
            reverse("climbs:user-log", args=["legacy"]))
        self.assertContains(response, "<td>5+</td>")


class ScoringTests(ClimbTestMixin, TestCase):
    def test_example_checks(self):
        # Spec examples: a V4 flash is 400, a V4 send on 5+ tries 376.
        self.assertEqual(calculate_points("V4", 1), 400)
        self.assertEqual(calculate_points("V4", 5), 376)

    def test_tries_multipliers(self):
        self.assertEqual(calculate_points("V4", 1), 400)
        self.assertEqual(calculate_points("V4", 2), round(400 * 0.99))
        self.assertEqual(calculate_points("V4", 3), round(400 * 0.98))
        self.assertEqual(calculate_points("V4", 4), round(400 * 0.96))
        self.assertEqual(calculate_points("V4", 5), round(400 * 0.94))

    def test_harder_grade_beats_easier_flash(self):
        # Grade always beats tries: a V8 worked for 5+ tries outscores
        # a flash V7, and generally the 5+ floor of any grade beats a
        # flash one grade below it (gentle and linear modes).
        self.assertGreater(
            calculate_points("V8", 5), calculate_points("V7", 1))
        for mode in ("gentle", "linear"):
            for n in range(10):
                self.assertGreater(
                    calculate_points(f"V{n + 1}", 9, grade_mode=mode),
                    calculate_points(f"V{n}", 1, grade_mode=mode),
                    f"V{n + 1} at 5+ should beat a flash V{n} ({mode})")

    def test_tries_floor_never_lower(self):
        # Hard floor at 5+: 6 tries and 40 tries score the same.
        five = calculate_points("V4", 5)
        self.assertEqual(calculate_points("V4", 6), five)
        self.assertEqual(calculate_points("V4", 40), five)

    def test_grade_modes(self):
        self.assertEqual(
            calculate_points("V4", 1, grade_mode="flat"), 300)
        self.assertEqual(
            calculate_points("V0", 1, grade_mode="flat"), 300)
        self.assertEqual(
            calculate_points("V0", 1, grade_mode="gentle"), 200)
        self.assertEqual(
            calculate_points("V8", 1, grade_mode="gentle"), 600)
        self.assertEqual(
            calculate_points("V0", 1, grade_mode="linear"), 100)
        self.assertEqual(
            calculate_points("V4", 1, grade_mode="linear"), 500)
        # Unknown modes fall back to the gentle default.
        self.assertEqual(
            calculate_points("V4", 1, grade_mode="bogus"), 400)

    def test_mystery_and_unknown_grades_score_flat(self):
        self.assertEqual(
            calculate_points("V6", 1, tag="mystery"),
            MYSTERY_FIXED_POINTS)
        self.assertEqual(
            calculate_points("V6", 5, tag="mystery"),
            MYSTERY_FIXED_POINTS)
        # Unrated/'?' climbs keep the old shape: a flat 300 base with
        # the normal tries multiplier (not the old try penalty).
        self.assertEqual(
            calculate_points("?", 1), MYSTERY_FIXED_POINTS)
        self.assertEqual(
            calculate_points("bogus", 3), round(300 * 0.98))

    def test_points_use_official_grade_not_suggestions(self):
        # A community suggestion must not move the score: the ascent
        # scores from the climb's stored grade.
        user = self.make_user("official")
        other = self.make_user("suggester")
        climb = self.make_climb(grade="V4")
        climb.grade_suggestions.create(
            user=other, suggested_grade="V8")
        climb.ascents.create(user=user, tries=1)
        ascent = Ascent.objects.get(user=user, climb=climb)
        self.assertEqual(ascent.points, calculate_points("V4", 1))
        self.assertNotEqual(ascent.points, calculate_points("V8", 1))

    def test_recalc_points_recomputes_from_stored_grade_and_tries(self):
        from django.core.management import call_command

        user = self.make_user("stale")
        climb = self.make_climb(grade="V4")
        ascent = climb.ascents.create(user=user, tries=5, points=1)
        stamp = ascent.logged_at
        call_command("recalc_points")
        ascent.refresh_from_db()
        self.assertEqual(ascent.points, calculate_points("V4", 5))
        # Tries and timestamps are never touched.
        self.assertEqual(ascent.tries, 5)
        self.assertEqual(ascent.logged_at, stamp)

    def test_recalculated_values_feed_boards_stats_and_profile(self):
        # Leaderboard, stats and profile all aggregate live points,
        # so a recalc flows everywhere with no other change.
        from django.core.management import call_command
        alice = self.make_user("recevery")
        climb = self.make_climb(name="Every", grade="V4", wall="main")
        climb.ascents.create(user=alice, tries=5, points=1)
        call_command("recalc_points")
        board = self.client.get(reverse("climbs:leaderboard"))
        walls = {w["wall"]: w for w in board.context["walls"]}
        self.assertEqual(walls["main"]["rows"][0]["total_points"],
                         calculate_points("V4", 5))
        self.client.force_login(alice)
        profile = self.client.get(reverse("accounts:profile"))
        self.assertContains(
            profile,
            f'<span class="stat-num">{calculate_points("V4", 5)}</span>')
        stats = self.client.get(reverse("accounts:stats"))
        self.assertContains(stats, str(calculate_points("V4", 5)))

    def test_recalc_points_is_idempotent_and_reports(self):
        import io

        from django.core.management import call_command

        user = self.make_user("idempotent")
        climb = self.make_climb(grade="V4")
        climb.ascents.create(
            user=user, tries=2, points=calculate_points("V4", 2))
        out = io.StringIO()
        call_command("recalc_points", stdout=out)
        self.assertIn("0 changed", out.getvalue())
        Ascent.objects.filter(user=user).update(points=7)
        out = io.StringIO()
        call_command("recalc_points", stdout=out)
        self.assertIn("1 changed", out.getvalue())
        self.assertEqual(
            Ascent.objects.get(user=user).points,
            calculate_points("V4", 2))


class RatingAndGradeTests(ClimbTestMixin, TestCase):
    def test_rating_upsert_and_average(self):
        alice = self.make_user("alice")
        bob = self.make_user("bob")
        climb = self.make_climb()
        self.client.force_login(alice)
        url = reverse("climbs:climb-rate", args=[climb.id])
        self.client.post(url, json.dumps({"value": 5}),
                         content_type="application/json")
        self.client.force_login(bob)
        response = self.client.post(url, json.dumps({"value": 3}),
                                    content_type="application/json")
        self.assertEqual(response.json()["rating_avg"], 4.0)
        self.assertEqual(response.json()["rating_count"], 2)

    def test_rating_out_of_range_rejected(self):
        user = self.make_user("picky")
        climb = self.make_climb()
        self.client.force_login(user)
        response = self.client.post(
            reverse("climbs:climb-rate", args=[climb.id]),
            json.dumps({"value": 6}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_grade_votes_split_harder_at_softer(self):
        climb = self.make_climb(grade="V4")
        for username, grade in (("g1", "V5"), ("g2", "V3"), ("g3", "V4")):
            user = self.make_user(username)
            self.client.force_login(user)
            self.client.post(
                reverse("climbs:climb-grade", args=[climb.id]),
                json.dumps({"suggested_grade": grade}),
                content_type="application/json")
        data = self.client.get(reverse("climbs:climb-detail",
                                       args=[climb.id])).json()
        self.assertEqual(data["grade_votes"],
                         {"harder": 1, "at": 1, "softer": 1, "total": 3})

    def test_anonymous_cannot_rate(self):
        climb = self.make_climb()
        response = self.client.post(
            reverse("climbs:climb-rate", args=[climb.id]),
            json.dumps({"value": 5}), content_type="application/json")
        self.assertEqual(response.status_code, 403)


class CommentApiTests(ClimbTestMixin, TestCase):
    def test_anonymous_reads_but_cannot_post(self):
        user = self.make_user("chatter")
        climb = self.make_climb()
        climb.comments.create(user=user, text="Great beta!")
        data = self.client.get(reverse("climbs:climb-detail",
                                       args=[climb.id])).json()
        self.assertEqual(len(data["comments"]), 1)
        response = self.client.post(
            reverse("climbs:climb-comment", args=[climb.id]),
            json.dumps({"text": "hi"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_post_and_delete_own_comment(self):
        user = self.make_user("author")
        climb = self.make_climb()
        self.client.force_login(user)
        response = self.client.post(
            reverse("climbs:climb-comment", args=[climb.id]),
            json.dumps({"text": "  Nice one  "}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        comment_id = response.json()["id"]
        self.assertEqual(Comment.objects.get(id=comment_id).text, "Nice one")
        delete_url = reverse("climbs:climb-comment-delete",
                             args=[climb.id, comment_id])
        self.assertEqual(self.client.delete(delete_url).status_code, 200)
        self.assertEqual(Comment.objects.count(), 0)

    def test_cannot_delete_someone_elses_comment(self):
        author = self.make_user("author2")
        other = self.make_user("other")
        climb = self.make_climb()
        comment = climb.comments.create(user=author, text="mine")
        self.client.force_login(other)
        response = self.client.delete(
            reverse("climbs:climb-comment-delete", args=[climb.id, comment.id]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Comment.objects.count(), 1)


class LeaderboardTests(ClimbTestMixin, TestCase):
    def test_current_board_sums_points_per_wall(self):
        alice = self.make_user("board_alice")
        bob = self.make_user("board_bob")
        main = self.make_climb(name="M1", grade="V4", wall="main")
        cave = self.make_climb(name="C1", grade="V2", wall="cave")
        main.ascents.create(user=alice, tries=1,
                            points=calculate_points("V4", 1))
        main.ascents.create(user=bob, tries=5,
                            points=calculate_points("V4", 5))
        cave.ascents.create(user=bob, tries=1,
                            points=calculate_points("V2", 1))
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertEqual(response.status_code, 200)
        walls = {w["wall"]: w for w in response.context["walls"]}
        self.assertEqual(
            [r["user__username"] for r in walls["main"]["rows"]],
            ["board_alice", "board_bob"])
        self.assertEqual(walls["main"]["rows"][0]["total_points"],
                         calculate_points("V4", 1))
        self.assertEqual(
            [r["user__username"] for r in walls["cave"]["rows"]],
            ["board_bob"])

    def test_board_reports_viewer_rank_and_points(self):
        alice = self.make_user("rank_alice")
        bob = self.make_user("rank_bob")
        climb = self.make_climb(name="R1", grade="V4", wall="main")
        climb.ascents.create(user=alice, tries=1,
                             points=calculate_points("V4", 1))
        climb.ascents.create(user=bob, tries=5,
                             points=calculate_points("V4", 5))
        self.client.force_login(bob)
        response = self.client.get(reverse("climbs:leaderboard"))
        walls = {w["wall"]: w for w in response.context["walls"]}
        self.assertEqual(walls["main"]["me"]["rank"], 2)
        self.assertEqual(walls["main"]["me"]["of"], 2)
        self.assertEqual(walls["main"]["me"]["points"],
                         calculate_points("V4", 5))
        self.assertContains(response, "You: #2 of 2")

    def test_climber_card_api_returns_shape(self):
        # Tapping a name on a board or comment loads this: picture,
        # totals, hardest grade and current ranks. Unknown names 404.
        user = self.make_user("carduser")
        user.avatar = "avatars/carduser.png"
        user.save()
        easy = self.make_climb(name="Easy", grade="V2", wall="main")
        hard = self.make_climb(name="Hard", grade="V5", wall="main")
        easy.ascents.create(user=user, tries=1,
                            points=calculate_points("V2", 1))
        hard.ascents.create(user=user, tries=2,
                            points=calculate_points("V5", 2))
        data = self.client.get(
            reverse("climbs:climber-detail", args=["carduser"])).json()
        self.assertEqual(data["username"], "carduser")
        self.assertIn("avatars/carduser.png", data["avatar"])
        self.assertEqual(data["sends"], 2)
        self.assertEqual(data["points"], calculate_points("V2", 1)
                         + calculate_points("V5", 2))
        self.assertEqual(data["best_grade"], "V5")
        walls = {r["wall"]: r for r in data["ranks"]}
        self.assertEqual(walls["Main Wall"]["position"], 1)
        self.assertEqual(walls["Main Wall"]["of"], 1)
        self.assertEqual(
            self.client.get(
                reverse("climbs:climber-detail", args=["ghost"])).status_code,
            404)

    def test_leaderboard_names_open_climber_card(self):
        user = self.make_user("clickable")
        climb = self.make_climb(name="Board", grade="V3")
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V3", 1))
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, 'data-username="clickable"')
        self.assertContains(response, 'id="user-card"')
        self.assertContains(response, "/climbers/")

    def test_map_comments_open_climber_card(self):
        # Comment authors render as chips wired to the same card.
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, 'id="user-card"')
        self.assertContains(response, "data-username=")

    def test_admin_walls_are_buttons(self):
        # Walls switch via form buttons, never hyperlinks.
        staff = self.make_user("wallbtn", staff=True)
        self.client.force_login(staff)
        for tab in ("climb-admin", "board-admin"):
            with self.subTest(tab=tab):
                content = self.client.get(
                    reverse(f"climbs:{tab}")).content.decode()
                region = content.split("insp-wall")[1].split("</form>")[0]
                self.assertIn('<button type="submit" name="wall"', region)
                self.assertNotIn("<a ", region)

    def test_hold_colours_run_rainbow_bold_to_dull(self):
        # Rainbow rows: the bold shades run across the top and each
        # row below gets duller, neutrals last, then the Custom hex
        # picker underneath. Stored values stay plain CSS colours in
        # a hidden field.
        from climbs.models import TAPE_COLOURS, TAPE_FAMILIES
        self.assertGreaterEqual(len(TAPE_COLOURS), 40)
        flat = [n for _, names in TAPE_FAMILIES for n in names]
        self.assertEqual(
            sorted(flat), sorted(n for n, _ in TAPE_COLOURS))
        staff = self.make_user("swatches", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:climb-admin"))
        content = response.content.decode()
        self.assertContains(response, 'id="f-swatches"')
        self.assertContains(response, "HOLD_COLOURS")
        import re
        names = re.search(
            r"const HOLD_COLOURS = \[(.*?)\];", content,
            re.DOTALL).group(1)
        self.assertEqual(re.findall(r"'([a-z]+)'", names), [
            'red', 'orange', 'yellow', 'lime', 'blue', 'magenta',
            'hotpink',
            'indianred', 'coral', 'khaki', 'green',
            'cornflowerblue', 'orchid', 'pink',
            'maroon', 'brown', 'olive', 'darkgreen', 'navy', 'purple',
            'palevioletred',
            'white', 'grey', 'black'])
        # Tape presets with no CSS name ride along as hexes: bright
        # orange and tape cyan in the bold row, wood beside brown.
        for hex_value in ("'#ff7a00'", "'#22b8cf'", "'#a06a35'"):
            self.assertIn(hex_value, names)
        self.assertIn("wood", flat)
        self.assertIn("bright orange", flat)
        # The custom hex picker stays for one-offs.
        self.assertContains(response, 'type="color"')
        self.assertContains(response, 'id="f-colour"')
        # Big boxes in a grid filling the panel width — no inner
        # scroll column.
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        grid = css.split("#f-swatches {")[1].split("}")[0]
        self.assertIn("display: grid", grid)
        self.assertIn("minmax(54px", grid)
        self.assertNotIn("overflow", grid)

    def test_seed_demo_builds_five_months(self):
        from django.core.management import call_command

        from climbs.models import GradeSuggestion, Rating
        from climbs.views import MAP_HEIGHT, MAP_WIDTH

        # Fresh databases already hold the migration's empty placeholder
        # sets, so the first build goes through --reset.
        call_command("seed_demo", reset=True)
        for wall in ("main", "cave"):
            sets = list(ClimbSet.objects.filter(wall=wall)
                        .order_by("created_at"))
            self.assertEqual(len(sets), 5)
            self.assertTrue(sets[-1].is_active)
            self.assertFalse(any(s.is_active for s in sets[:-1]))
            self.assertTrue(all(s.climbs.count() > 0 for s in sets))
        self.assertGreater(Ascent.objects.count(), 20)
        self.assertGreater(Rating.objects.count(), 0)
        self.assertGreater(Comment.objects.count(), 0)
        self.assertGreater(GradeSuggestion.objects.count(), 0)
        for climb in Climb.objects.all():
            self.assertTrue(0 <= climb.x_percent <= MAP_WIDTH)
            self.assertTrue(0 <= climb.y_percent <= MAP_HEIGHT)

    def test_seed_demo_refuses_and_resets(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        call_command("seed_demo", reset=True)
        before = Climb.objects.count()
        with self.assertRaises(CommandError):
            call_command("seed_demo")
        self.assertEqual(Climb.objects.count(), before)
        call_command("seed_demo", reset=True)
        self.assertEqual(Climb.objects.count(), before)

    def test_draw_shares_position_and_medal(self):
        # Equal points share a rank (1, 1, 3): both leaders wear gold.
        a = self.make_user("tie_a")
        b = self.make_user("tie_b")
        c = self.make_user("tie_c")
        climb = self.make_climb(name="Tie", grade="V3")
        climb.ascents.create(user=a, tries=1,
                             points=calculate_points("V3", 1))
        climb.ascents.create(user=b, tries=1,
                             points=calculate_points("V3", 1))
        climb.ascents.create(user=c, tries=5,
                             points=calculate_points("V3", 5))
        response = self.client.get(reverse("climbs:leaderboard"))
        content = response.content.decode()
        self.assertEqual(content.count('class="pos-1"'), 2)
        self.assertContains(response, '<span class="pos-3">3</span>')

    def test_top_three_wear_medals(self):
        climb = self.make_climb(name="Medals", grade="V3")
        for i in range(4):
            user = self.make_user(f"medal{i}")
            climb.ascents.create(
                user=user, tries=1,
                points=calculate_points("V3", 1) - i)
        response = self.client.get(reverse("climbs:leaderboard"))
        content = response.content.decode()
        self.assertIn('class="pos-1"', content)
        self.assertIn('class="pos-2"', content)
        self.assertIn('class="pos-3"', content)
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn(".pos-1", css)
        self.assertIn(".pos-2", css)
        self.assertIn(".pos-3", css)
        self.assertIn("border-radius: 50%", css)

    def test_tag_bands_narrow_at_the_ends(self):
        # The tag picks the grade shortlist: white is V0-V1, black
        # V1-V2, red V2-V3, green V3-V4, blue V4-V5, yellow V5-V6,
        # orange V6-V7, project V8+. V2 offers black and red.
        staff = self.make_user("bands", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:climb-admin"))
        self.assertContains(response, "GRADES_BY_TAG")
        self.assertContains(response, "white: ['V0', 'V1']")
        self.assertContains(response, "black: ['V1', 'V2']")
        self.assertContains(response, "red: ['V2', 'V3']")
        self.assertContains(response, "green: ['V3', 'V4']")
        self.assertContains(response, "blue: ['V4', 'V5']")
        self.assertContains(response, "yellow: ['V5', 'V6']")
        self.assertContains(response, "orange: ['V6', 'V7']")
        self.assertContains(response, "project: ['V8', 'V9', 'V10']")

    def test_admin_markers_wear_text_halos(self):
        # Admin grade labels carry the same dark outline as the map.
        staff = self.make_user("halo", staff=True)
        self.client.force_login(staff)
        climb = self.make_climb(name="Halo", grade="V3")
        response = self.client.get(reverse("climbs:climb-admin"))
        content = response.content.decode()
        marker = f'<g class="climb-marker" data-id="{climb.id}"'
        pos = content.index(marker)
        window = content[pos:pos + 1200]
        self.assertIn('paint-order="stroke"', window)

    def test_board_shows_top_ten_with_expand(self):
        climb = self.make_climb(name="Big", grade="V2", wall="main")
        for i in range(12):
            user = self.make_user(f"crowd{i:02d}")
            climb.ascents.create(
                user=user, tries=1, points=calculate_points("V2", 1) - i)
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "Show all 12")
        self.assertContains(response, 'class="extra-row"')

    def test_new_set_drafts_quietly_until_made_current(self):
        # Starting a set must not touch the live one: the old
        # deactivate-on-start contract is gone (rotation now happens
        # explicitly through "Make current"). The board doesn't reset.
        user = self.make_user("rotator")
        climb = self.make_climb(grade="V3", wall="main")
        climb.ascents.create(user=user, tries=2,
                             points=calculate_points("V3", 2))
        old_set = ClimbSet.active("main")
        staff = self.make_user(" staffer".strip(), staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-set-start"),
            json.dumps({"wall": "main", "label": "Main Wall — next month"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        new_set = ClimbSet.objects.get(id=response.json()["id"])
        self.assertFalse(new_set.is_active)
        old_set.refresh_from_db()
        self.assertTrue(old_set.is_active)
        climb.refresh_from_db()
        self.assertTrue(climb.is_active)
        # The live board is untouched by the draft.
        current = self.client.get(reverse("climbs:leaderboard"))
        walls = {w["wall"]: w for w in current.context["walls"]}
        self.assertEqual(len(walls["main"]["rows"]), 1)
        self.assertEqual(walls["main"]["rows"][0]["total_points"],
                         calculate_points("V3", 2))

    def test_first_set_on_a_wall_goes_live(self):
        # With nothing to dethrone, a wall's first set must be live or
        # nothing on the wall is loggable.
        staff = self.make_user("firstsetter", staff=True)
        self.client.force_login(staff)
        ClimbSet.objects.filter(wall="cave").delete()
        response = self.client.post(
            reverse("climbs:climb-set-start"),
            json.dumps({"wall": "cave"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        new_set = ClimbSet.objects.get(id=response.json()["id"])
        self.assertTrue(new_set.is_active)
        self.assertEqual(ClimbSet.active("cave").id, new_set.id)

    def test_map_hides_set_switcher_with_no_sets(self):
        # No sets means no Sets panel element; the tap-to-dismiss
        # script still ships (it guards for the missing element).
        ClimbSet.objects.all().delete()
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="set-switcher"')
        self.assertContains(response, "switcher.open = false")

    def test_nav_and_title_say_leaderboard(self):
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, ">Leaderboard<")
        self.assertNotContains(response, ">Boards<")

    def test_current_board_titled_by_wall_only(self):
        active = ClimbSet.active("main")
        self.assertTrue(active.label.startswith("Main Wall"))
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "<h3>Main Wall</h3>")
        self.assertNotContains(response, active.label)

    def test_short_label_strips_wall_prefix(self):
        self.assertEqual(
            ClimbSet(wall="main",
                     label="Main Wall — September 2026").short_label,
            "September 2026")
        self.assertEqual(
            ClimbSet(wall="cave",
                     label="The Cave — October 2026").short_label,
            "October 2026")
        self.assertEqual(
            ClimbSet(wall="main", label="September 2026").short_label,
            "September 2026")

    def test_new_set_label_has_no_wall_prefix(self):
        from django.utils.timezone import now as tz_now
        staff = self.make_user("labeler", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-set-start"),
            json.dumps({"wall": "cave"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        label = ClimbSet.objects.get(id=response.json()["id"]).label
        self.assertEqual(label, f"{tz_now():%B %Y}")
        self.assertNotIn("Cave", label)

    def test_tag_class_comes_from_tag_not_grade(self):
        for tag in ("white", "black", "red", "green", "blue",
                    "yellow", "orange", "project", "mystery"):
            with self.subTest(tag=tag):
                self.assertEqual(
                    self.make_climb(grade="V3", tag=tag).tag_class,
                    f"tag-{tag}")
        # A V3 can wear red or green depending on how it was set.
        self.assertEqual(
            self.make_climb(grade="V3", tag="red").tag_class, "tag-red")
        self.assertEqual(
            self.make_climb(grade="V3", tag="green").tag_class,
            "tag-green")
        # Unknown tag values fall back to rainbow.
        climb = self.make_climb(grade="V3", tag="red")
        climb.tag = "bogus"
        self.assertEqual(climb.tag_class, "tag-mystery")

    def test_empty_set_shows_no_sends_note(self):
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "No sends logged on this set yet.")

    def test_past_boards_list_and_archive_view(self):
        from datetime import timedelta
        from django.utils import dateformat
        active = ClimbSet.active("main")
        old = ClimbSet.objects.create(
            wall="main", label="Main Wall — August 2026", is_active=False)
        ClimbSet.objects.filter(id=old.id).update(
            created_at=active.created_at - timedelta(days=30))
        old.refresh_from_db()
        user = self.make_user("historian")
        old_climb = Climb.objects.create(
            name="Oldie", grade="V2", colour="red", wall="main",
            climb_set=old, x_percent=10.0, y_percent=10.0)
        old_climb.ascents.create(user=user, tries=1,
                                 points=calculate_points("V2", 1))
        day = dateformat.format(old.created_at, "j M Y")

        current = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(current, "Past boards")
        self.assertContains(current, 'name="main_set"')
        self.assertContains(current, f'value="{old.id}"')
        self.assertNotContains(current, "Archived")

        history = self.client.get(
            reverse("climbs:leaderboard") + f"?main_set={old.id}")
        self.assertContains(history, "Archived")
        self.assertContains(history, "Back to current")
        self.assertContains(history, "viewing")
        self.assertContains(history, day)
        self.assertContains(history, "1 climbs · 1 sends")

    def test_past_boards_are_buttons(self):
        # Archived boards open through form buttons, never links.
        user = self.make_user("archiver")
        old = ClimbSet.objects.create(
            wall="main", label="Old", is_active=False)
        old_climb = Climb.objects.create(
            name="Oldie", grade="V2", colour="red", wall="main",
            climb_set=old, x_percent=10.0, y_percent=10.0)
        old_climb.ascents.create(user=user, tries=1,
                                 points=calculate_points("V2", 1))
        content = self.client.get(
            reverse("climbs:leaderboard")).content.decode()
        region = content.split('past-boards">')[1].split("</details>")[0]
        self.assertIn('<button type="submit" name="main_set"', region)
        self.assertNotIn("<a ", region)
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn(".past-list button", css)

    def test_nav_stays_on_one_row(self):
        # Phones keep the whole bar on a single sideways-scrolling
        # row so the profile chip never wraps onto the map controls.
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        nav = css.split("nav {")[1].split("}")[0]
        self.assertIn("flex-wrap: nowrap", nav)
        self.assertIn("overflow-x: auto", css)

    def test_set_stats_and_grade_graph(self):
        users = [self.make_user(f"stat{i}") for i in range(3)]
        techy = self.make_climb(name="Techy", grade="V3", wall="main")
        power = self.make_climb(name="Power", grade="V4", wall="main")
        techy.ascents.create(user=users[0], tries=1,
                             points=calculate_points("V3", 1))
        techy.ascents.create(user=users[1], tries=1,
                             points=calculate_points("V3", 1))
        techy.ascents.create(user=users[2], tries=2,
                             points=calculate_points("V3", 2))
        power.ascents.create(user=users[0], tries=3,
                             points=calculate_points("V4", 3))
        techy.ratings.create(user=users[0], value=4)
        techy.ratings.create(user=users[1], value=4)
        power.ratings.create(user=users[0], value=5)
        techy.grade_suggestions.create(user=users[0], suggested_grade="V2")
        techy.grade_suggestions.create(user=users[1], suggested_grade="V2")
        power.grade_suggestions.create(user=users[0], suggested_grade="V5")
        power.grade_suggestions.create(user=users[1], suggested_grade="V5")
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "Best rated")
        self.assertContains(response, "Power</button>")
        self.assertContains(response, "5.0 (1)")
        self.assertContains(response, "Most popular")
        self.assertContains(response, "Techy</button>")
        self.assertContains(response, "3 sends")
        self.assertContains(response, "Softest")
        self.assertContains(response, "2 softer")
        self.assertContains(response, "Hardest")
        self.assertContains(response, "2 harder")
        self.assertNotContains(response, "Most flashed")
        self.assertContains(response, "4 sends · 3 climbers")
        self.assertContains(response, "Sends by grade")
        self.assertContains(response, "width:100%")
        self.assertContains(response, "width:33%")
        # Stat climbs open the map through GET-form buttons, never
        # hyperlinks: no ?climb= links anywhere on the boards.
        self.assertNotContains(response, "?climb=")
        self.assertContains(response, 'class="stat-go"')
        self.assertContains(response, 'name="climb"')
        self.assertContains(response, f'value="{techy.id}"')

    def test_stats_collapse_empty_rows_and_swatch_unnamed(self):
        # Sends but no ratings or votes: empty stat rows collapse
        # instead of showing "—", unnamed climbs show a colour
        # swatch plus the colour NAME (never a raw hex), and the
        # graph lists only grades that actually have sends.
        from climbs.models import ClimbSet
        user = self.make_user("sparse")
        sent = Climb.objects.create(
            name="", grade="V2", tag="black", colour="#c9265f",
            wall="main", climb_set=ClimbSet.active("main"),
            x_percent=10, y_percent=10)
        Climb.objects.create(
            name="Untouched", grade="V5", tag="blue", colour="blue",
            wall="main", climb_set=ClimbSet.active("main"),
            x_percent=20, y_percent=20)
        sent.ascents.create(user=user, tries=1,
                            points=calculate_points("V2", 1))
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "Most popular")
        self.assertNotContains(response, "Best rated")
        self.assertNotContains(response, "Softest")
        self.assertNotContains(response, "Hardest")
        self.assertNotContains(response, '<span class="muted">—</span>')
        self.assertContains(response, 'class="colour-dot"')
        self.assertContains(response, 'style="background:#c9265f"')
        # The swatch keeps the true colour; the text names it.
        self.assertContains(response, ">magenta</button>")
        self.assertNotContains(response, "#c9265f</button>")
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn("--dot-radius", css)
        self.assertContains(response, '<span class="graph-grade">V2</span>')
        self.assertNotContains(
            response, '<span class="graph-grade">V5</span>')

    def test_leaderboard_dots_match_admin_colour(self):
        # Stat dots render the stored colour raw, like the map and
        # admin markers — not the remapped tape hex.
        user = self.make_user("dotfan")
        climb = self.make_climb(name="Dot", grade="V4")
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, 'style="background:red"')
        self.assertNotContains(response, 'background:#e03131')


class AdminApiTests(ClimbTestMixin, TestCase):
    def test_create_on_empty_wall_shows_on_map_and_admin(self):
        # Regression: with no climbs on a wall, adding one through the
        # staff API must land it on the map and the admin page.
        staff = self.make_user("emptywall", staff=True)
        self.client.force_login(staff)
        self.assertEqual(
            Climb.objects.filter(wall="main").count(), 0)
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"name": "First", "grade": "V2", "tag": "red",
                        "colour": "red", "wall": "main",
                        "x_percent": 10, "y_percent": 20}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        climb = Climb.objects.get(id=response.json()["id"])
        self.assertTrue(climb.is_active)
        self.assertEqual(climb.climb_set, ClimbSet.active("main"))
        self.assertContains(self.client.get(reverse("climbs:map")),
                            f'data-climb-id="{climb.id}"')
        self.assertContains(self.client.get(reverse("climbs:climb-admin")),
                            f'data-id="{climb.id}"')

    def test_staff_can_create_climb_in_active_set(self):
        staff = self.make_user("admin1", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"name": "Fresh", "grade": "V5", "colour": "blue",
                        "wall": "cave", "x_percent": 10.5, "y_percent": 20.5}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        climb = Climb.objects.get(id=response.json()["id"])
        self.assertEqual(climb.climb_set, ClimbSet.active("cave"))
        self.assertEqual(climb.x_percent, 10.5)

    def test_create_rejects_bad_grade(self):
        staff = self.make_user("admin2", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"grade": 5, "colour": "blue",
                        "wall": "MAIN", "x_percent": 1, "y_percent": 2}),
            content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Climb.objects.count(), 0)

    def test_create_and_update_tag(self):
        staff = self.make_user("tagadmin", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"name": "Tagged", "grade": "V3", "tag": "green",
                        "colour": "green", "wall": "main",
                        "x_percent": 5, "y_percent": 6}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        climb = Climb.objects.get(id=response.json()["id"])
        self.assertEqual(climb.tag, "green")
        # Unknown tags are rejected on create and update.
        bad = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"grade": "V3", "tag": "pink", "wall": "main",
                        "x_percent": 5, "y_percent": 6}),
            content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        denied = self.client.post(
            reverse("climbs:climb-update", args=[climb.id]),
            json.dumps({"tag": "pink"}), content_type="application/json")
        self.assertEqual(denied.status_code, 400)
        ok = self.client.post(
            reverse("climbs:climb-update", args=[climb.id]),
            json.dumps({"tag": "red"}), content_type="application/json")
        self.assertEqual(ok.status_code, 200)
        climb.refresh_from_db()
        self.assertEqual(climb.tag, "red")
        # Junk coordinates are a 400, not a 500 on save; numeric
        # strings still land as floats.
        junk = self.client.post(
            reverse("climbs:climb-update", args=[climb.id]),
            json.dumps({"x_percent": "far left"}),
            content_type="application/json")
        self.assertEqual(junk.status_code, 400)
        move = self.client.post(
            reverse("climbs:climb-update", args=[climb.id]),
            json.dumps({"x_percent": "12.5", "y_percent": 44}),
            content_type="application/json")
        self.assertEqual(move.status_code, 200)
        climb.refresh_from_db()
        self.assertEqual(climb.x_percent, 12.5)
        self.assertEqual(climb.y_percent, 44.0)

    def test_tag_grade_pairs_follow_the_bands(self):
        # V2 wears black or red — never white or green. Mystery takes
        # any grade; project starts at V8.
        staff = self.make_user("pairadmin", staff=True)
        self.client.force_login(staff)
        url = reverse("climbs:climb-create")
        base = {"name": "Banded", "colour": "red", "wall": "main",
                "x_percent": 5, "y_percent": 6}

        def create(grade, tag):
            return self.client.post(
                url, json.dumps({"grade": grade, "tag": tag, **base}),
                content_type="application/json")

        self.assertEqual(create("V2", "black").status_code, 200)
        self.assertEqual(create("V2", "red").status_code, 200)
        bad_white = create("V2", "white")
        self.assertEqual(bad_white.status_code, 400)
        self.assertIn("V1", bad_white.json()["error"])
        self.assertEqual(create("V2", "green").status_code, 400)
        self.assertEqual(create("V5", "mystery").status_code, 200)
        self.assertEqual(create("V7", "project").status_code, 400)
        self.assertEqual(create("V8", "project").status_code, 200)
        # Grade-only posts wear the grade's own band.
        response = self.client.post(
            url, json.dumps({"grade": "V5", "colour": "blue",
                             "wall": "main",
                             "x_percent": 1, "y_percent": 2}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Climb.objects.get(id=response.json()["id"]).tag, "blue")
        # Mismatched updates are rejected; matched ones land.
        climb = Climb.objects.get(name="Banded", tag="black")
        update_url = reverse("climbs:climb-update", args=[climb.id])
        denied = self.client.post(
            update_url, json.dumps({"grade": "V5"}),
            content_type="application/json")
        self.assertEqual(denied.status_code, 400)
        ok = self.client.post(
            update_url, json.dumps({"tag": "red", "grade": "V3"}),
            content_type="application/json")
        self.assertEqual(ok.status_code, 200)
        climb.refresh_from_db()
        self.assertEqual((climb.tag, climb.grade), ("red", "V3"))

    def test_mystery_scores_flat_and_locks_votes(self):
        mystery = Climb.objects.create(
            name="Mystery", grade="V6", tag="mystery", colour="white",
            wall="main", climb_set=ClimbSet.active("main"),
            x_percent=10, y_percent=10)
        flash = self.make_user("mflash")
        grinder = self.make_user("mgrind")
        for user, tries in ((flash, 1), (grinder, 5)):
            self.client.force_login(user)
            response = self.client.post(
                reverse("climbs:climb-ascent", args=[mystery.id]),
                json.dumps({"tries": tries}),
                content_type="application/json")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["points"],
                             MYSTERY_FIXED_POINTS)
        # No harder/softer while the grade is hidden.
        self.client.force_login(flash)
        vote = self.client.post(
            reverse("climbs:climb-grade", args=[mystery.id]),
            json.dumps({"suggested_grade": "V6"}),
            content_type="application/json")
        self.assertEqual(vote.status_code, 400)
        self.assertIn("Mystery", vote.json()["error"])
        # The hidden grade reads back as '?' everywhere public.
        detail = self.client.get(
            reverse("climbs:climb-detail", args=[mystery.id])).json()
        self.assertTrue(detail["is_mystery"])
        self.assertEqual(detail["grade"], "?")
        self.assertEqual(detail["mystery_points"], MYSTERY_FIXED_POINTS)
        self.assertEqual(detail["grade_votes"]["total"], 0)
        content = self.client.get(reverse("climbs:map")).content.decode()
        pos = content.index(f'data-climb-id="{mystery.id}"')
        self.assertIn(">?</text>", content[pos:pos + 900])
        board = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(board, '<span class="graph-grade">?</span>')
        # ...and it can't leak through a climber's best grade either.
        easy = self.make_climb(name="Easy", grade="V2", wall="main")
        easy.ascents.create(user=flash, tries=1,
                            points=calculate_points("V2", 1))
        card = self.client.get(
            reverse("climbs:climber-detail", args=["mflash"])).json()
        self.assertEqual(card["best_grade"], "V2")

    def test_revealing_a_mystery_unlocks_voting(self):
        mystery = Climb.objects.create(
            name="Mystery", grade="V4", tag="mystery", colour="white",
            wall="main", climb_set=ClimbSet.active("main"),
            x_percent=10, y_percent=10)
        staff = self.make_user("revealer", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-update", args=[mystery.id]),
            json.dumps({"tag": "green", "grade": "V4"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        voter = self.make_user("revoter")
        self.client.force_login(voter)
        vote = self.client.post(
            reverse("climbs:climb-grade", args=[mystery.id]),
            json.dumps({"suggested_grade": "V5"}),
            content_type="application/json")
        self.assertEqual(vote.status_code, 200)
        detail = self.client.get(
            reverse("climbs:climb-detail", args=[mystery.id])).json()
        self.assertFalse(detail["is_mystery"])
        self.assertEqual(detail["grade"], "V4")
        self.assertEqual(detail["grade_votes"]["harder"], 1)

    def test_mystery_tag_is_horizontal_stripes(self):
        # Five simple stripes: red, yellow, blue, green, purple.
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        block = css.split(".tag-mystery")[1].split("}")[0]
        self.assertIn("to bottom", block)
        for band in ("#b3402f", "#d9b62c", "#2978a0",
                     "#43914e", "#6a5a9e"):
            self.assertIn(band, block)
        self.assertNotIn("#e6e4da", block)

    def test_grade_bar_segments_use_each_climbs_tag(self):
        # One grade, two tags: the bar splits into a red segment and
        # a green one, each wearing exactly its climbs' tag colour.
        crew = [self.make_user("seg1"), self.make_user("seg2")]
        red = self.make_climb(name="Red V3", grade="V3", wall="main",
                              tag="red")
        green = self.make_climb(name="Green V3", grade="V3", wall="main",
                                tag="green")
        red.ascents.create(user=crew[0], tries=1,
                           points=calculate_points("V3", 1))
        red.ascents.create(user=crew[1], tries=1,
                           points=calculate_points("V3", 1))
        green.ascents.create(user=crew[0], tries=1,
                             points=calculate_points("V3", 1))
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(
            response, '<span class="gbar tag-red" style="width:67%">')
        self.assertContains(
            response, '<span class="gbar tag-green" style="width:33%">')

    def test_nav_marks_the_current_page(self):
        # Each page highlights exactly one nav link (accent pill plus
        # aria-current) so the active section is always visible.
        for url_name, label in (("map", "Map"),
                                ("leaderboard", "Leaderboard")):
            with self.subTest(page=url_name):
                content = self.client.get(
                    reverse(f"climbs:{url_name}")).content.decode()
                self.assertEqual(
                    content.count('aria-current="page"'), 1)
                pos = content.index('class="nav-link active"')
                self.assertIn(label, content[pos:pos + 400])
        staff = self.make_user("navactive", staff=True)
        self.client.force_login(staff)
        content = self.client.get(
            reverse("climbs:climb-admin")).content.decode()
        self.assertEqual(content.count('aria-current="page"'), 1)
        pos = content.index('class="nav-link active"')
        self.assertIn("Admin", content[pos:pos + 1200])

    def test_admin_button_shows_for_staff_only(self):
        admin_url = reverse("climbs:climb-admin")
        response = self.client.get(reverse("climbs:map"))
        self.assertNotContains(response, f'href="{admin_url}"')
        self.client.force_login(self.make_user("member"))
        response = self.client.get(reverse("climbs:map"))
        self.assertNotContains(response, f'href="{admin_url}"')
        self.client.force_login(self.make_user("staffer", staff=True))
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, f'href="{admin_url}"')

    def test_map_shows_current_sets_and_can_view_archived(self):
        old = ClimbSet.objects.create(
            wall="main", label="Old set", is_active=False)
        old_climb = Climb.objects.create(
            name="Oldie", grade="V2", tag="red", colour="red",
            wall="main", climb_set=old, x_percent=10, y_percent=10,
            is_active=False)
        new_climb = self.make_climb(name="Newbie", grade="V3")
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, f'data-climb-id="{new_climb.id}"')
        self.assertNotContains(response, f'data-climb-id="{old_climb.id}"')
        self.assertNotContains(response, "Viewing an archived set")
        archived = self.client.get(
            reverse("climbs:map") + f"?main_set={old.id}")
        self.assertContains(archived, f'data-climb-id="{old_climb.id}"')
        self.assertNotContains(archived, f'data-climb-id="{new_climb.id}"')
        self.assertContains(archived, "Viewing an archived set")
        # A deep link jumps to the climb's own set.
        jump = self.client.get(
            reverse("climbs:map") + f"?climb={old_climb.id}")
        self.assertContains(jump, f'data-climb-id="{old_climb.id}"')
        # Archived climbs are read-only in the detail API.
        detail = self.client.get(
            reverse("climbs:climb-detail", args=[old_climb.id]))
        self.assertTrue(detail.json()["archived"])
        live = self.client.get(
            reverse("climbs:climb-detail", args=[new_climb.id]))
        self.assertFalse(live.json()["archived"])

    def test_admin_page_scopes_climbs_to_selected_set(self):
        staff = self.make_user("scopeadmin", staff=True)
        self.client.force_login(staff)
        old = ClimbSet.objects.create(
            wall="main", label="Old set", is_active=False)
        old_climb = Climb.objects.create(
            name="Oldie", grade="V2", tag="red", colour="red",
            wall="main", climb_set=old, x_percent=10, y_percent=10,
            is_active=False)
        new_climb = self.make_climb(name="Newbie", grade="V3")
        response = self.client.get(reverse("climbs:climb-admin"))
        self.assertContains(response, f'data-id="{new_climb.id}"')
        # The map itself shows only the selected set (the inspector
        # file list still names the archived climb).
        self.assertContains(
            response, f'<g class="climb-marker" data-id="{new_climb.id}"')
        self.assertNotContains(
            response, f'<g class="climb-marker" data-id="{old_climb.id}"')
        # Grade labels ride on the admin markers.
        self.assertContains(response, f">{new_climb.grade}</text>")
        # Panel speaks setter language; moderation lives on its own tab.
        self.assertContains(response, "Proposed grade")
        self.assertContains(response, ">Tag<")
        self.assertContains(response, "Hold colour")
        self.assertNotContains(response, "data-remove-user")
        # Top choice bar plus the folder inspector.
        self.assertContains(response, "Climb edit")
        self.assertContains(response, "Leaderboards")
        self.assertContains(response, "insp-set")
        self.assertContains(response, "data-add-climb")
        self.assertContains(response, "insp-file")
        # Folders carry their saved date, whatever the name says.
        self.assertContains(
            response,
            f"{old.created_at.day} {old.created_at.strftime('%b %Y')}")
        # The tag drives the grade shortlist; colours come from
        # family-grouped swatches, Custom keeping the hex picker.
        self.assertContains(response, "GRADES_BY_TAG")
        self.assertContains(response, "blue: ['V4', 'V5']")
        self.assertContains(response, 'id="f-swatches"')
        self.assertContains(response, 'type="color"')
        selected = self.client.get(
            reverse("climbs:climb-admin") + f"?wall=main&set={old.id}")
        self.assertContains(
            selected, f'<g class="climb-marker" data-id="{old_climb.id}"')
        self.assertNotContains(
            selected, f'<g class="climb-marker" data-id="{new_climb.id}"')

    def test_climb_panel_fits_small_screens(self):
        # The edit panel is a bottom sheet: docked to the bottom
        # edge, capped below full height (vh fallback first for
        # browsers without dvh) with the form scrolling inside, so
        # every swipe on it scrolls the sheet — never the map behind
        # it. A sticky grabber drags it back down.
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        block = css.split("#climb-edit-panel {")[1].split("}")[0]
        self.assertIn("position: fixed", block)
        self.assertIn("bottom: 0", block)
        self.assertIn("85vh", block)
        self.assertIn("85dvh", block)
        self.assertIn("overflow-y: auto", block)
        self.assertIn("touch-action: pan-y", block)
        self.assertIn("overscroll-behavior: contain", block)
        self.assertIn("translateY(103%)", block)
        self.assertIn("safe-area-inset-bottom", block)
        opened = css.split("#climb-edit-panel.open {")[1].split("}")[0]
        self.assertIn("transform: none", opened)
        grabber = css.split(
            "#climb-edit-panel .grabber {")[1].split("}")[0]
        self.assertIn("touch-action: none", grabber)
        # The grabber scrolls away with the sheet content — it must
        # never pin itself over the fields below it.
        self.assertNotIn("sticky", grabber)
        self.assertNotIn("fixed", grabber)

    def test_panel_opens_as_bottom_sheet(self):
        # Phones: the edit sheet slides up into view when it opens
        # and drags down to dismiss, so reaching it never means
        # dragging the map.
        staff = self.make_user("panelview", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:climb-admin"))
        self.assertContains(response, 'id="panel-grabber"')
        self.assertContains(response, 'class="grab-pill"')
        # Critical positioning rides inline so the sheet shows even
        # when the stylesheet is stale or missing; the animation and
        # details enhance from CSS.
        self.assertContains(
            response, "position:fixed; left:0; right:0; bottom:0")
        self.assertContains(response, 'panel.classList.add("open")')
        self.assertContains(response, 'panel.classList.remove("open")')
        self.assertContains(response, "if (dy > 90) closePanel();")

    def test_admin_map_supports_zoom_for_fine_placement(self):
        # The admin wall zooms (wheel/pinch) so markers can be placed
        # precisely, through the same shared wall-view.js module as
        # the main map: markers hold their on-screen size while
        # zoomed, and marker drags pause the pan layer instead of
        # fighting it. Zoom is progressive enhancement: without the
        # module the editor still works unzoomed. Double-tap zoom
        # stays off so hurried taps never reframe the wall mid-edit.
        # No on-map zoom buttons: they went unused.
        staff = self.make_user("zoomadmin", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:climb-admin"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "climbs/wall-view.js")
        self.assertContains(response, "window.WallView ? window.WallView.create")
        self.assertContains(response, "WallView.create")
        self.assertContains(response, "map-content")
        self.assertContains(response, "doubleTap: false")
        self.assertNotContains(response, "panzoom")
        self.assertNotContains(response, "data-zoom-in")
        self.assertNotContains(response, "data-zoom-out")
        self.assertNotContains(response, "data-zoom-reset")
        self.assertNotContains(response, "zoom-controls")
        self.assertContains(response, "WallView.rescaleMarkers")
        self.assertContains(response, "wallZoom.pause")
        self.assertContains(response, "wallZoom.resume")
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertNotIn(".zoom-controls", css)

    def test_unarmed_create_defaults_to_viewed_set(self):
        # Regression: clicking the map without arming "Add climb" sent
        # no set, so the climb silently landed in the live set instead
        # of the draft set being viewed — it never appeared on the map
        # and turned up in the old set. The viewed set must be the
        # payload default (armed placements still override it).
        staff = self.make_user("viewplacer", staff=True)
        self.client.force_login(staff)
        draft = ClimbSet.objects.create(
            wall="main", label="Draft", is_active=False)
        response = self.client.get(
            reverse("climbs:climb-admin") + f"?wall=main&set={draft.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "payload.climb_set = SELECTED_SET_ID")

    def test_admin_tabs_preserve_selected_set(self):
        # Switching tabs dropped ?set=, bouncing the admin back to the
        # live set mid-draft. Both tabs must keep the viewed set.
        staff = self.make_user("tabkeeper", staff=True)
        self.client.force_login(staff)
        draft = ClimbSet.objects.create(
            wall="main", label="Draft", is_active=False)
        # The topbar cross-links are the ones that must carry it (the
        # inspector's own set links already do).
        response = self.client.get(
            reverse("climbs:climb-admin") + f"?wall=main&set={draft.id}")
        self.assertContains(
            response, f"/admin-tools/boards/?wall=main&set={draft.id}")
        response = self.client.get(
            reverse("climbs:board-admin") + f"?wall=main&set={draft.id}")
        self.assertContains(
            response, f"/admin-tools/climbs/?wall=main&set={draft.id}")

    def test_markers_clamp_inside_the_wall(self):
        # Dragging past the photo edge clamps back inside (minus the
        # marker radius) instead of stranding the marker off-map.
        from climbs.models import clamp_to_wall
        self.assertEqual(clamp_to_wall(-50, 600), (7, 505))
        self.assertEqual(clamp_to_wall(500, -20), (249, 7))
        self.assertEqual(clamp_to_wall(30.5, 40.5), (30.5, 40.5))
        staff = self.make_user("clamper", staff=True)
        self.client.force_login(staff)
        create = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"grade": "V2", "wall": "main",
                        "x_percent": 999, "y_percent": -5}),
            content_type="application/json")
        self.assertEqual(create.status_code, 200)
        climb = Climb.objects.get(id=create.json()["id"])
        self.assertEqual((climb.x_percent, climb.y_percent), (249, 7))
        move = self.client.post(
            reverse("climbs:climb-update", args=[climb.id]),
            json.dumps({"x_percent": -30, "y_percent": 999}),
            content_type="application/json")
        self.assertEqual(move.status_code, 200)
        climb.refresh_from_db()
        self.assertEqual((climb.x_percent, climb.y_percent), (7, 505))
        # The coordinate system is unchanged: plain viewBox units.
        self.assertTrue(0 <= climb.x_percent <= 256)
        self.assertTrue(0 <= climb.y_percent <= 512)

    def test_clamp_markers_command_lists_and_fixes(self):
        import io

        from django.core.management import call_command

        stray = Climb.objects.create(
            name="Stray", grade="V2", tag="red", colour="red",
            wall="main", climb_set=ClimbSet.active("main"),
            x_percent=400.0, y_percent=-40.0)
        out = io.StringIO()
        call_command("clamp_markers", stdout=out)
        stray.refresh_from_db()
        self.assertEqual((stray.x_percent, stray.y_percent), (249, 7))
        self.assertIn("Stray", out.getvalue())
        self.assertIn("Clamped 1 climbs", out.getvalue())
        out = io.StringIO()
        call_command("clamp_markers", stdout=out)
        self.assertIn("Clamped 0 climbs", out.getvalue())

    def test_move_climb_round_trips_to_map(self):
        # Dragging a marker in admin saves coordinates the main map
        # renders verbatim: admin placement always matches the map.
        staff = self.make_user("mover", staff=True)
        self.client.force_login(staff)
        climb = self.make_climb(name="Mover", x=10.0, y=20.0)
        response = self.client.post(
            reverse("climbs:climb-update", args=[climb.id]),
            json.dumps({"x_percent": 30.5, "y_percent": 40.5}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        content = self.client.get(reverse("climbs:map")).content.decode()
        pos = content.index(f'data-climb-id="{climb.id}"')
        window = content[pos:pos + 600]
        self.assertIn('cx="30.5"', window)
        self.assertIn('cy="40.5"', window)

    def test_create_into_named_set(self):
        # Armed placement from the inspector lands the climb in that
        # exact set (even an archived one), wall following the set.
        staff = self.make_user("placer", staff=True)
        self.client.force_login(staff)
        old = ClimbSet.objects.create(
            wall="cave", label="Old cave set", is_active=False)
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"name": "Placed", "grade": "V2", "tag": "red",
                        "colour": "red", "wall": "main",
                        "climb_set": old.id,
                        "x_percent": 11, "y_percent": 22}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        climb = Climb.objects.get(id=response.json()["id"])
        self.assertEqual(climb.climb_set_id, old.id)
        self.assertEqual(climb.wall, "cave")
        # Unknown sets are rejected.
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"grade": "V2", "wall": "main", "climb_set": 99999,
                        "x_percent": 1, "y_percent": 2}),
            content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_admin_empty_wall_invites_new_set(self):
        staff = self.make_user("emptysetter", staff=True)
        self.client.force_login(staff)
        ClimbSet.objects.filter(wall="cave").delete()
        response = self.client.get(
            reverse("climbs:climb-admin") + "?wall=cave")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No sets")

    def test_boards_tab_shows_moderation(self):
        cheater = self.make_user("boardcheater")
        climb = self.make_climb(name="Target", grade="V3")
        climb.ascents.create(user=cheater, tries=1,
                             points=calculate_points("V3", 1))
        staff = self.make_user("boardmod", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:board-admin"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "boardcheater")
        self.assertContains(response, "data-remove-user")
        self.assertContains(response, "Climb edit")
        # The climbs tab no longer carries the board table.
        climbs_page = self.client.get(reverse("climbs:climb-admin"))
        self.assertNotContains(climbs_page, "data-remove-user")
        # Signed-out visitors and members are turned away.
        self.client.logout()
        response = self.client.get(reverse("climbs:board-admin"))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.make_user("member2"))
        response = self.client.get(reverse("climbs:board-admin"))
        self.assertEqual(response.status_code, 302)

    def test_set_activate_rename_delete(self):
        staff = self.make_user("setadmin", staff=True)
        self.client.force_login(staff)
        new_climb = self.make_climb(name="Newbie", grade="V3")
        old = ClimbSet.objects.create(
            wall="main", label="Old set", is_active=False)
        old_climb = Climb.objects.create(
            name="Oldie", grade="V2", tag="red", colour="red",
            wall="main", climb_set=old, x_percent=10, y_percent=10,
            is_active=False)
        # Rename, with blanks rejected.
        response = self.client.post(
            reverse("climbs:climb-set-update", args=[old.id]),
            json.dumps({"label": "Renamed"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        old.refresh_from_db()
        self.assertEqual(old.label, "Renamed")
        response = self.client.post(
            reverse("climbs:climb-set-update", args=[old.id]),
            json.dumps({"label": "  "}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        # Activating swaps which climbs are live.
        response = self.client.post(
            reverse("climbs:climb-set-activate", args=[old.id]),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClimbSet.active("main").id, old.id)
        old_climb.refresh_from_db()
        new_climb.refresh_from_db()
        self.assertTrue(old_climb.is_active)
        self.assertFalse(new_climb.is_active)
        # Deleting a set takes its climbs with it; deleting the live
        # set hands liveness to the newest set left standing.
        response = self.client.post(
            reverse("climbs:climb-set-delete", args=[old.id]),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Climb.objects.filter(id=old_climb.id).exists())
        self.assertFalse(ClimbSet.objects.filter(id=old.id).exists())
        self.assertEqual(ClimbSet.active("main").id,
                         new_climb.climb_set_id)

    def test_remove_user_from_set_board(self):
        cheater = self.make_user("cheater")
        honest = self.make_user("honest")
        climb = self.make_climb(name="Target", grade="V3")
        climb.ascents.create(user=cheater, tries=1,
                             points=calculate_points("V3", 1))
        climb.ascents.create(user=honest, tries=1,
                             points=calculate_points("V3", 1))
        climb.ratings.create(user=cheater, value=5)
        staff = self.make_user("moderator", staff=True)
        self.client.force_login(staff)
        set_id = climb.climb_set_id
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "cheater")
        response = self.client.post(
            reverse("climbs:climb-set-remove-user", args=[set_id]),
            json.dumps({"username": "cheater"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], 1)
        self.assertFalse(Ascent.objects.filter(user=cheater).exists())
        # Ratings stay; only the sends go.
        self.assertTrue(climb.ratings.filter(user=cheater).exists())
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertNotContains(response, "cheater")
        self.assertContains(response, "honest")
        # Unknown usernames 404; anonymous callers are turned away.
        response = self.client.post(
            reverse("climbs:climb-set-remove-user", args=[set_id]),
            json.dumps({"username": "ghost"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 404)
        self.client.logout()
        response = self.client.post(
            reverse("climbs:climb-set-remove-user", args=[set_id]),
            json.dumps({"username": "honest"}),
            content_type="application/json")
        self.assertIn(response.status_code, (302, 403))

    def test_leaderboard_shows_avatars(self):
        pic = self.make_user("picuser")
        pic.avatar = "avatars/picuser.png"
        pic.save()
        plain = self.make_user("plainuser")
        climb = self.make_climb(name="Board", grade="V3")
        climb.ascents.create(user=pic, tries=1,
                             points=calculate_points("V3", 1))
        climb.ascents.create(user=plain, tries=1,
                             points=calculate_points("V3", 1))
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "avatars/picuser.png")
        self.assertContains(response, "avatar-xs avatar-fallback")

    def test_delete_climb_updates_everywhere(self):
        # A climb with the full works (sends, rating, vote, comment)
        # deletes cleanly: every page stays up and the numbers follow.
        sender = self.make_user("doomed-sender")
        climb = self.make_climb(name="Doomed", grade="V3")
        climb.ascents.create(user=sender, tries=2,
                             points=calculate_points("V3", 2))
        climb.ratings.create(user=sender, value=4)
        climb.grade_suggestions.create(user=sender, suggested_grade="V4")
        climb.comments.create(user=sender, text="beta")
        staff = self.make_user("deleter", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-delete", args=[climb.id]),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Ascent.objects.count(), 0)
        self.assertEqual(
            self.client.get(reverse("climbs:map")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("climbs:leaderboard")).status_code, 200)
        self.client.force_login(sender)
        profile = self.client.get(reverse("accounts:profile"))
        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, '<span class="stat-num">0</span>')
        self.assertEqual(
            self.client.get(reverse("climbs:climb-detail",
                                    args=[climb.id])).status_code, 404)

    def test_anonymous_cannot_use_admin_apis(self):
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({}), content_type="application/json")
        # user_passes_test redirects anonymous users to login
        self.assertIn(response.status_code, (302, 403))

    def test_map_shows_active_climb_markers(self):
        climb = self.make_climb()
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-climb-id="{climb.id}"')
        self.assertContains(response, str(climb.x_percent))

    def test_map_serves_bottom_sheet_skeleton(self):
        # The climb popup is a phone-first bottom sheet: static skeleton
        # (scrim + grabber + body) ships in the HTML, content renders into
        # the body on tap.
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="sheet-body"')
        self.assertContains(response, 'class="sheet-scrim"')
        self.assertContains(response, 'id="sheet-grabber"')
        # Finger-sized invisible hit area around every marker: owned
        # by the shared module, constant on-screen size at any zoom.
        module = finders.find("climbs/wall-view.js")
        with open(module) as f:
            js = f.read()
        self.assertIn("climb-hit", js)
        self.assertIn("HIT_R = 20", js)
        # Sheet header names the wall in text; comments collapse.
        self.assertContains(response, "wall-line")
        self.assertContains(response, "comments-box")
        # Grade labels stay on at every zoom level (no zoom-out gate).
        self.assertNotContains(response, ">= 1.3")

    def test_map_has_watermark_and_dismisses_sets(self):
        # Every page carries the quiet summit_map mark, and tapping
        # the map closes the open Sets panel without a manual toggle.
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="watermark"')
        self.assertContains(response, "switcher.open = false")

    def test_tag_bands_cover_every_tag(self):
        # Pills and graph bars wear the climb's own tag: seed one
        # climb per tag (deliberately cross-wired grades, proving the
        # colour no longer follows the grade) and check every band
        # renders on the leaderboard.
        tag_grades = [("white", "V5"), ("black", "V6"), ("red", "V3"),
                      ("green", "V2"), ("blue", "V7"), ("yellow", "V4"),
                      ("orange", "V8"), ("project", "V0"),
                      ("mystery", "V1")]
        for tag, grade in tag_grades:
            self.make_climb(name=f"Circuit {tag}", grade=grade,
                            wall="main", tag=tag)
        # The stats block (and its graph) renders once a set has sends;
        # every climb needs one so every tag band appears as a segment.
        grapher = self.make_user("grapher")
        for tag, grade in tag_grades:
            Climb.objects.get(name=f"Circuit {tag}").ascents.create(
                user=grapher, tries=1,
                points=calculate_points(grade, 1))
        response = self.client.get(reverse("climbs:leaderboard"))
        for tag, _ in tag_grades:
            self.assertContains(response, f"tag-{tag}")
        # The map's tag mapper knows the same nine tags.
        map_response = self.client.get(reverse("climbs:map"))
        for tag, _ in tag_grades:
            self.assertContains(map_response, f"'{tag}'")
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn(".grade-pill, .gbar).tag-project", css)
        self.assertIn(".grade-pill, .gbar).tag-mystery", css)
        # The popup pill rule is layout-only: it must not set a fill
        # or text colour, or it would override the bands above and
        # every popup grade would render flat gold.
        popup_block = css.split("#climb-popup .grade-pill")[1].split("}")[0]
        self.assertNotIn("background", popup_block)
        self.assertNotIn("color", popup_block)

    def test_map_markers_show_sent_state_and_grade_search(self):
        # Every climb always shows; the Show buttons only dim the
        # other group, and a grade search narrows the wall. All of
        # it is fed by the viewer's sent ids and remembered on
        # the device.
        user = self.make_user("sender")
        sent = self.make_climb(name="Sent", grade="V4")
        unsent = self.make_climb(name="Fresh", grade="V0")
        sent.ascents.create(
            user=user, tries=1, points=calculate_points("V4", 1))
        self.client.force_login(user)
        response = self.client.get(reverse("climbs:map"))
        # Inner circles keep the stored colour rendered raw, exactly
        # as the admin wall paints it (not the remapped tape hex).
        self.assertContains(response, 'fill="red"')
        self.assertNotContains(response, 'fill="#e03131"')
        content = response.content.decode()
        sent_pos = content.index(f'data-climb-id="{sent.id}"')
        sent_window = content[sent_pos:sent_pos + 1200]
        self.assertIn('class="climb-dot"', sent_window)
        self.assertIn('fill="#fff"', sent_window)
        self.assertIn('paint-order="stroke"', sent_window)
        self.assertNotIn('#3fa34d', content)
        self.assertNotIn('sent-halo', content)
        self.assertNotIn('sent-tick', content)
        self.assertNotIn('sent-ring', content)
        # Status only ever dims; nothing is hidden by it.
        self.assertContains(response, "m.style.opacity")
        # The filter offers all/sent/unsent, knows this send, and
        # persists the choice; grades are searchable per marker.
        self.assertContains(response, 'data-marker-filter="all"')
        self.assertContains(response, 'data-marker-filter="sent"')
        self.assertContains(response, 'data-marker-filter="unsent"')
        self.assertContains(response, f"new Set([{sent.id}])")
        self.assertContains(response, "summit-filter")
        self.assertContains(response, "summit-grade")
        self.assertContains(response, 'id="grade-filter"')
        self.assertContains(response, f'data-grade="V4"')
        self.assertContains(response, "applyMarkerFilter")
        # Markers shrink a little when zoomed out (the shared
        # module's softer counter-scale, constant-size tap target).
        self.assertContains(response, "WallView.rescaleMarkers")
        module = finders.find("climbs/wall-view.js")
        with open(module) as f:
            js = f.read()
        self.assertIn("Math.pow(scale, 0.55)", js)
        self.assertIn("climb-hit", js)
        # The grade label scales in lockstep with the ring (same factor)
        # and is sized so the widest grade always sits inside it.
        self.assertIn("BASE_FONT", js)
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn(".climb-marker text", css)
        self.assertIn("ui-monospace",
                      css.split(".climb-marker text")[1][:500])

    def test_sheet_copy_is_plain_spoken(self):
        # No nag to rate first, and the grade question names the
        # proposed grade and asks whether you agree. The comments
        # form sits in a padded inner box.
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, "No ratings yet.")
        self.assertNotContains(response, "tap a star")
        self.assertNotContains(response, "be the first")
        self.assertContains(response, "This climb has been proposed")
        self.assertContains(response, "do you agree?")
        self.assertNotContains(response, "What grade is it really?")
        self.assertContains(response, "comments-inner")

    def test_climber_since_reads_day_month_year(self):
        user = self.make_user("dated")
        data = self.client.get(
            reverse("climbs:climber-detail", args=["dated"])).json()
        self.assertRegex(data["since"], r"^\d{2}/\d{2}/\d{4}$")

    def test_admin_set_picker_is_a_button(self):
        # Choosing a set submits a form button, never a hyperlink.
        import re

        staff = self.make_user("setbtn", staff=True)
        self.client.force_login(staff)
        ClimbSet.objects.create(
            wall="main", label="Old set", is_active=False)
        for tab in ("climb-admin", "board-admin"):
            with self.subTest(tab=tab):
                content = self.client.get(
                    reverse(f"climbs:{tab}")).content.decode()
                summaries = re.findall(
                    r"<summary>(.*?)</summary>", content, re.S)
                picks = [s for s in summaries if "insp-set-pick" in s]
                self.assertTrue(picks)
                for pick in picks:
                    self.assertIn('<button type="submit" name="set"', pick)
                    self.assertNotIn("<a ", pick)

    def test_admin_topbar_has_no_underlines(self):
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        block = css.split(".admin-topbar a")[1].split("}")[0]
        self.assertIn("text-decoration: none", block)

    def test_set_switcher_is_a_side_panel(self):
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, 'id="set-switcher"')
        self.assertContains(response, "set-panel")
        self.assertContains(response, "set-tab-text")
        # The tab pulls across: drag opens/shuts, a clean tap toggles.
        self.assertContains(response, "Edge slider")
        self.assertContains(response, "switcher.open = x > startX")
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        panel = css.split("#set-switcher {")[1].split("}")[0]
        self.assertIn("position: fixed", panel)
        self.assertIn("left: 0", panel)

    def test_sheet_copy_is_trimmed(self):
        response = self.client.get(reverse("climbs:map"))
        content = response.content.decode()
        # Rating average/count stay beside the stars; nag lines and
        # vote receipts are gone.
        self.assertIn("rating_count", content)
        self.assertNotIn("you voted", content)
        self.assertNotContains(response, "can't be changed")
        self.assertNotContains(response, "tap to change")
        # Middle grade chip proposes the set grade.
        self.assertIn("· proposed", content)
        self.assertNotIn("· set", content)

    def test_nav_profile_matches_link_sizing(self):
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertRegex(css, r"nav a\s*\{[^}]*min-height\s*:\s*44px")
        self.assertRegex(css, r"\.nav-profile\s*\{[^}]*min-height\s*:\s*44px")
        self.assertRegex(css, r"\.nav-profile\s*\{[^}]*white-space\s*:\s*nowrap")
        self.assertRegex(
            css, r"\.sheet-x\s*\{[^}]*border-radius\s*:\s*var\(--r-btn\)")

    def test_stat_rating_gold_and_map_deep_link(self):
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertRegex(
            css, r"\.rating-num\s*\{[^}]*color\s*:\s*var\(--star\)")
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, "[?&]climb=")

    def test_anonymous_map_has_empty_sent_filter(self):
        self.make_climb()
        response = self.client.get(reverse("climbs:map"))
        content = response.content.decode()
        start = content.index('id="climb-layer"')
        layer = content[start:content.index('</svg>', start)]
        self.assertNotIn("#3fa34d", layer)
        self.assertIn("#ffffff", layer)
        self.assertContains(response, "new Set([])")

    def test_sheet_drops_nag_lines(self):
        # The "can't be changed" and "you gave it N stars" notes are
        # gone; the sheet states facts (logged box, lit stars) instead.
        response = self.client.get(reverse("climbs:map"))
        self.assertNotContains(response, "can't be changed")
        self.assertNotContains(response, "tap to change")
        # Unlit rate stars render hollow for the abstract look.
        self.assertContains(response, "☆")

    def test_theme_vars_and_nav_brand(self):
        css_path = finders.find("climbs/style.css")
        self.assertIsNotNone(css_path)
        with open(css_path) as f:
            css = f.read()
        # Five sharp themes: abyss (deep trench cyan), blueprint,
        # ember (amber), forest (green serif), clay (warm paper serif) —
        # plus modern (soft light) and midnight (soft dark).
        for theme in ("abyss", "blueprint", "ember", "forest", "clay",
                      "modern", "midnight"):
            self.assertIn(f'[data-theme="{theme}"]', css)
        # The modern pair is soft: smooth sans type, rounded corners.
        modern = css.split('[data-theme="modern"]')[1].split("}")[0]
        self.assertIn("system-ui", modern)
        self.assertIn("--r-btn: 12px", modern)
        midnight = css.split('[data-theme="midnight"]')[1].split("}")[0]
        self.assertIn("system-ui", midnight)
        self.assertIn("color-scheme: dark", midnight)
        self.assertIn("--font-body", css)
        self.assertIn("--font-display", css)
        self.assertNotIn("--font-mono", css)
        self.assertIn("#070c13", css)
        self.assertIn("#5cc3e6", css)
        self.assertIn("#e09b2d", css)
        self.assertIn("#9fb87a", css)
        self.assertIn("#b35c1e", css)
        self.assertIn("--sand", css)
        self.assertIn("#bcab79", css)
        self.assertNotIn("#9fc131", css)
        # Old palette fully retired: blue accent, slate borders, 999px pills.
        self.assertNotIn("#8ab4ff", css)
        self.assertNotIn("#2a2b3a", css)
        self.assertNotRegex(css, r"border-radius\s*:\s*999px")
        # Pre-overhaul abyss tokens are gone too.
        self.assertNotIn("#0c1014", css)
        self.assertNotIn("#4da3d8", css)
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, "summit-theme")
        self.assertContains(response, "brand-mark")
        # Fresh visitors start on modern (midnight when the OS asks
        # for dark); the no-JS shell matches the light default.
        self.assertContains(response, 'data-theme="modern"')
        self.assertContains(response, "(prefers-color-scheme: dark)")
        self.assertContains(response, "dark ? 'midnight' : 'modern'")

    def test_background_tile_exists_and_is_wired(self):
        # Each theme brings its own generated grid tile; the old
        # navy bg.jpg is retired.
        tiles = ["bg-grid-abyss.jpg", "bg-grid-blueprint.jpg",
                 "bg-grid-ember.jpg", "bg-grid-forest.jpg",
                 "bg-grid-clay.jpg"]
        for tile in tiles:
            with self.subTest(tile=tile):
                self.assertIsNotNone(finders.find(f"climbs/{tile}"))
        self.assertIsNone(finders.find("climbs/bg.jpg"))
        self.assertIsNone(finders.find("climbs/bg-grid.jpg"))
        self.assertIsNone(finders.find("climbs/bg-grid-light.jpg"))
        with open(finders.find("climbs/style.css")) as f:
            css = f.read()
        for tile in tiles:
            with self.subTest(tile=tile):
                self.assertIn(tile, css)

    def test_sheet_collects_input_without_typing(self):
        # Tap-only controls: tries use a -/+ stepper and grades use chips,
        # so the sheet must not contain a number field or a dropdown.
        # (The page-level set switcher may use dropdowns; scope the
        # check to the popup region.)
        response = self.client.get(reverse("climbs:map"))
        self.assertNotContains(response, 'type="number"')
        sheet = response.content.decode().split('id="climb-popup"')[1]
        self.assertNotIn("<select", sheet)
        self.assertContains(response, "t-minus")
        self.assertContains(response, "Math.min(5, tries + 1)")
        self.assertContains(response, "5+ tries")
        self.assertContains(response, "tries >= 5 ? '5+'")
        self.assertNotContains(response, "Math.min(99")
        self.assertContains(response, "data-dir")
        self.assertContains(response, "p-more-comments")
        self.assertContains(response, "set-switcher")

    def test_pages_keep_their_working_zoom(self):
        # The main map zooms through the wall-view.js module: single
        # translate+scale transform (origin 0 0) on the one container
        # holding photo + overlay, zoom around the pointer/pinch
        # centre, pan clamped after every update, pointer events,
        # non-passive wheel, rAF batching, no CSS transitions while
        # gesturing, bounds recomputed on resize / orientation /
        # image load. The admin editor zooms through the same
        # shared module on its own viewport instead of panzoom.
        module = finders.find("climbs/wall-view.js")
        self.assertIsNotNone(module)
        with open(module) as f:
            js = f.read()
        for token in ("transform-origin", "requestAnimationFrame",
                      "zoomAt", "clampPan", "touchAction",
                      "passive: false", "orientationchange",
                      "MAX_SCALE = 8", "Double-tap zoom",
                      "pinch centre", "OVERSCROLL"):
            self.assertIn(token, js)
        self.client.logout()
        content = self.client.get(reverse("climbs:map")).content.decode()
        self.assertIn("climbs/wall-view.js", content)
        self.assertIn("WallView.create", content)
        self.assertIn("map-content", content)
        self.assertNotIn("panzoom.min.js", content)
        self.assertNotIn("settleNudge", content)
        self.assertNotIn("sharpenAfterZoom", content)
        staff = self.make_user("sharedzoom", staff=True)
        self.client.force_login(staff)
        admin = self.client.get(
            reverse("climbs:climb-admin")).content.decode()
        self.assertIn("climbs/wall-view.js", admin)
        self.assertIn("WallView.create", admin)
        self.assertIn("map-content", admin)
        self.assertNotIn("panzoom", admin)

    def test_marker_geometry_has_no_css_transition(self):
        # A stroke-width transition animates marker geometry on every
        # zoom frame and leaves soft edges; hover smoothness belongs
        # on filter instead.
        css_path = finders.find("climbs/style.css")
        self.assertIsNotNone(css_path)
        with open(css_path) as f:
            css = f.read()
        self.assertNotRegex(css, r"transition\s*:[^;}]*stroke-width")
        self.assertRegex(css,
                         r"\.climb-marker\s*\{[^}]*transition\s*:[^}]*filter")

    def test_wall_artwork_is_stable_and_themed(self):
        # The wall is inlined vector (an <img> rasterizes once, then
        # smears as a bitmap under pinch-zoom): straight,
        # low-precision geometry with no echo contour, colours per
        # theme through variables.
        import re
        import xml.etree.ElementTree as ET
        from pathlib import Path

        partial = Path(__file__).parent / "templates" / "climbs" / "_wall.html"
        body = partial.read_text()
        # The header note must use {% comment %}: {# #} cannot span
        # lines, so a multi-line note would leak onto the page as text.
        # Beyond that the partial stays plain parseable SVG.
        body = re.sub(r"{% comment %}.*?{% endcomment %}", "",
                      body, flags=re.DOTALL)
        self.assertNotIn("{%", body)
        self.assertNotIn("{#", body)
        root = ET.fromstring(body)
        ids = [el.get("id") for el in root.iter()]
        self.assertNotIn("path5", ids)
        # Four fills tile the wall with no overlap, exactly one path
        # draws every feature line, and one draws the frame.
        for wanted in ("fill-body", "fill-upper-left", "fill-right-wall",
                       "fill-top-right-box", "wall-lines", "frame"):
            self.assertIn(wanted, ids)
        self.assertEqual(ids.count("wall-lines"), 1)
        # The data-space mapping is frozen: the overlay positions
        # climbs in its own 256x512 viewBox, so the wall viewBox
        # must never move under it.
        self.assertIn('viewBox="0 0 164.42926 491.13571"', body)
        # Two widths, defined once as variables: no per-path
        # stroke-width attrs, a single near-opaque stroke on the one
        # line path so neither side can thin against its backdrop, and
        # zoom-proof strokes throughout.
        self.assertNotIn("stroke-width=", body)
        self.assertEqual(body.count("stroke-opacity"), 1)
        self.assertEqual(body.count('vector-effect="non-scaling-stroke"'), 2)
        numbers = []
        for el in root.iter():
            for attr in ("d", "x", "y", "width", "height"):
                value = el.get(attr)
                if value:
                    numbers += re.findall(r"-?\d+(?:\.\d+)?", value)
        self.assertTrue(numbers)
        for raw in numbers:
            self.assertEqual(float(raw), round(float(raw), 1), raw)
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, 'class="wall-svg"')
        self.assertNotContains(response, "wall.svg")
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn(".wall-svg .wall-panel", css)
        self.assertIn("fill: var(--wall-panel)", css)
        self.assertIn("stroke: var(--wall-line)", css)
        # Feature lines keep their own dimmer stroke: the frame stays
        # the brightest line, exactly as in the supplied artwork.
        self.assertIn("stroke: var(--wall-line-dim)", css)
        for var in ("--wall-wash:", "--wall-panel:", "--wall-line:",
                    "--wall-line-dim:"):
            self.assertEqual(css.count(var), 7, var)
        self.assertEqual(css.count("--wall-edge:"), 1)
        self.assertEqual(css.count("--wall-feature:"), 1)
        self.assertIn("--wall-edge: 4.5", css)
        self.assertIn("--wall-feature: 3.0", css)

    def test_marker_layer_overlays_wall_image(self):
        # Regression: with no positioning, the SVG sat below the <img> in
        # normal flow, so markers were off-screen and map clicks hit the
        # image and did nothing.
        css_path = finders.find("climbs/style.css")
        self.assertIsNotNone(css_path)
        with open(css_path) as f:
            css = f.read()
        self.assertRegex(css, r"#climb-layer\s*\{[^}]*position\s*:\s*absolute")

    def test_tap_opens_the_tapped_marker(self):
        # Each marker's collider is exactly its visible dot — the old
        # fat invisible halo opened climbs on near-miss taps and
        # flashed a tap box around itself in Chromium. Now the release
        # point must sit inside a dot (real on-screen box, so zoom/pan
        # are accounted for); overlapping dots resolve to whichever
        # sits on top, and a tap inside no dot opens nothing. No
        # chooser, no tap box, no focus ring.
        first = self.make_climb(name="Near One", x=50.0, y=100.0)
        second = self.make_climb(name="Near Two", x=50.5, y=100.5)
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="marker-picker"')
        self.assertNotContains(response, "nearbyMarkers")
        self.assertNotContains(response, "showPicker")
        self.assertNotContains(response, "PICK_RADIUS")
        self.assertNotContains(response, "climb-hit")
        self.assertContains(response, "const target = climbAt(e.clientX, e.clientY)")
        self.assertContains(response, "if (target) await openClimb(target.dataset.climbId)")
        self.assertContains(response, "if (!hit || !hit.inside) return;")
        import re
        climb_fn = re.search(
            r"function climbAt\(x, y\) \{.*?\n        \}",
            response.content.decode(), re.DOTALL).group(0)
        self.assertNotIn("nearest", climb_fn)
        self.assertNotIn("fallback", climb_fn)
        self.assertNotIn("bestD", climb_fn)
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        marker = css.split(".climb-marker {")[1].split("}")[0]
        self.assertIn("-webkit-tap-highlight-color: transparent", marker)
        self.assertIn("outline: none", marker)
        layer = css.split("#climb-layer {")[-1].split("}")[0]
        self.assertIn("-webkit-tap-highlight-color: transparent", layer)
        self.assertContains(response, "getBoundingClientRect")
        self.assertContains(response, "querySelector('.climb-dot')")
        self.assertContains(response, "bringToFront")
        for climb in (first, second):
            self.assertContains(response, f'data-climb-id="{climb.id}"')
        css_path = finders.find("climbs/style.css")
        self.assertIsNotNone(css_path)
        with open(css_path) as f:
            css = f.read()
        self.assertNotIn("#marker-picker", css)
        module = finders.find("climbs/wall-view.js")
        with open(module) as f:
            js = f.read()
        self.assertIn("bringToFront", js)
        self.assertIn("climb-hit", js)

    def test_pages_survive_zoom_module_failure(self):
        # If wall-view.js fails to load, the main map still works
        # unzoomed: marker taps and the sheet never depend on it. The
        # admin editor likewise works unzoomed without the module:
        # taps, the panel, placement and dragging never depend on it.
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, "window.WallView ? window.WallView.create")
        self.assertContains(response, "if (!window.WallView) return;")
        staff = self.make_user("fallback", staff=True)
        self.client.force_login(staff)
        admin = self.client.get(reverse("climbs:climb-admin"))
        self.assertContains(admin, "window.WallView ? window.WallView.create")

    def test_colour_picker_cannot_recurse(self):
        # Regression: opening the panel with an unknown stored colour
        # re-rendered the swatch grid until the page hung and every
        # marker tap died. setColour only ever writes the hidden
        # value and toggles selection — it never rebuilds the grid.
        import re
        src = open("climbs/templates/climbs/climb_admin.html").read()
        script = re.search(
            r"<script>\nconst svg =[\s\S]*?</script>", src).group(0)
        body = script.split("function setColour", 1)[1]
        body = body.split(
            "document.getElementById('f-picker')", 1)[0]
        code = re.sub(r"//[^\n]*", "", body)
        self.assertNotIn("renderSwatches", code)
        self.assertNotIn("innerHTML", code)
        self.assertIn("getElementById('f-colour').value = c", code)

    def test_review_tables_stay_on_screen(self):
        # Wide flag evidence and action buttons wrap in their cells
        # and the tables scroll sideways: nothing pushes the page
        # off-screen on phones.
        staff = self.make_user("tablesize", staff=True)
        self.client.force_login(staff)
        user = self.make_user("tableuser")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        board = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(board, 'class="table-wrap"')
        log = self.client.get(reverse("climbs:user-log", args=["tableuser"]))
        self.assertContains(log, 'class="table-wrap"')
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        wrap = css.split(".table-wrap {")[1].split("}")[0]
        self.assertIn("overflow-x: auto", wrap)
        actions = css.split(".flag-actions {")[1].split("}")[0]
        self.assertIn("flex-wrap: wrap", actions)
        evidence = css.split(".flag-list li p {")[1].split("}")[0]
        self.assertIn("overflow-wrap: anywhere", evidence)

    def test_board_actions_live_inside_the_table(self):
        # The Remove / Hide-from-board buttons sit in the row's own
        # Actions cell (labelled header, wrapping button group), not
        # floating outside the table.
        import re
        staff = self.make_user("boardbuttons", staff=True)
        self.client.force_login(staff)
        user = self.make_user("buttonuser")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        response = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(response, "<th>Actions</th>")
        content = response.content.decode()
        row = re.search(
            r"<tr>[\s\S]*?buttonuser[\s\S]*?</tr>",
            content).group(0)
        cell = re.search(
            r"<td>\s*<div class=\"flag-actions\">[\s\S]*?</div>\s*</td>",
            row).group(0)
        self.assertIn("data-hide-user", cell)
        self.assertIn("data-remove-user", cell)

    def test_board_admin_reads_at_a_glance(self):
        # The review board scans fast: a rank column, a header line
        # with climber/send/review totals, filters that apply on
        # change, and a clear-filters escape when filtered.
        staff = self.make_user("reviewux", staff=True)
        self.client.force_login(staff)
        user = self.make_user("glanceuser")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        response = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(response, '<th class="rank">#</th>')
        self.assertContains(response, "1 climber")
        self.assertContains(response, "1 send")
        self.assertContains(response, "1 climb")
        self.assertContains(response, "need review")
        self.assertContains(response, "e.target.form.submit()")
        flagged = self.client.get(
            reverse("climbs:board-admin") + "?flagged=1")
        self.assertContains(flagged, "Clear")

    def test_suspicion_filter_narrows_rows(self):
        # The board filters by the suspicion badge, not severity: a
        # threshold keeps only rows whose shown open flags add up to
        # at least that many points. Badges still total what is shown.
        from climbs.models import Flag
        staff = self.make_user("suspstaff", staff=True)
        self.client.force_login(staff)
        climb = self.make_climb()
        mild = self.make_user("milduser")
        climb.ascents.create(user=mild, tries=1,
                             points=calculate_points("V4", 1))
        Flag.objects.create(
            user=mild, rule_code="low_rule", severity="low",
            points=5, details={"summary": "mild"})
        wild = self.make_user("wilduser")
        climb.ascents.create(user=wild, tries=1,
                             points=calculate_points("V4", 1))
        Flag.objects.create(
            user=wild, rule_code="high_rule_a", severity="high",
            points=30, details={"summary": "spicy"})
        Flag.objects.create(
            user=wild, rule_code="high_rule_b", severity="high",
            points=30, details={"summary": "extra spicy"})
        plain = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(plain, "milduser")
        self.assertContains(plain, "wilduser")
        self.assertContains(plain, "⚑ 60")
        strict = self.client.get(
            reverse("climbs:board-admin") + "?suspicion=50")
        self.assertContains(strict, "wilduser")
        self.assertNotContains(strict, "milduser")
        self.assertContains(strict, "⚑ 60")
        loose = self.client.get(
            reverse("climbs:board-admin") + "?suspicion=20")
        self.assertContains(loose, "wilduser")
        self.assertNotContains(loose, "milduser")
        self.assertContains(loose, "Clear")

    def test_board_shows_only_viewed_set_flags(self):
        # Ascent-tied flags from other sets stay off this board;
        # user-level flags (no ascent: whole-history patterns) stay
        # visible wherever the climber boards.
        from climbs.models import Climb, ClimbSet, Flag
        staff = self.make_user("scopestaff", staff=True)
        self.client.force_login(staff)
        user = self.make_user("scopeuser")
        climb_a = self.make_climb(name="Here", grade="V4")
        set_a = climb_a.climb_set
        set_b = ClimbSet.objects.create(
            wall="main", label="Other", is_active=False)
        climb_b = Climb.objects.create(
            name="There", grade="V4", tag="white", colour="blue",
            wall="main", climb_set=set_b, x_percent=10, y_percent=10)
        ascent_a = climb_a.ascents.create(
            user=user, tries=1, points=calculate_points("V4", 1))
        ascent_b = climb_b.ascents.create(
            user=user, tries=1, points=calculate_points("V4", 1))
        Flag.objects.create(
            user=user, ascent=ascent_a, rule_code="this_set_rule",
            severity="high", points=25,
            details={"summary": "suspicious here"})
        Flag.objects.create(
            user=user, ascent=ascent_b, rule_code="other_set_rule",
            severity="high", points=25,
            details={"summary": "suspicious there"})
        Flag.objects.create(
            user=user, rule_code="whole_history_rule", severity="low",
            points=5, details={"summary": "pattern everywhere"})
        response = self.client.get(
            reverse("climbs:board-admin") + f"?wall=main&set={set_a.id}")
        self.assertContains(response, "this_set_rule")
        self.assertContains(response, "whole_history_rule")
        self.assertNotContains(response, "other_set_rule")
        self.assertContains(response, "Needs review (2)")
        self.assertContains(response, "⚑ 30")

    def test_same_rule_flags_group_into_one_line(self):
        # Ten flashes then five more read as one grouped line with a
        # combined points total, not fifteen separate entries.
        from climbs.models import Flag
        staff = self.make_user("groupstaff", staff=True)
        self.client.force_login(staff)
        user = self.make_user("groupuser")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        for n in range(3):
            Flag.objects.create(
                user=user, rule_code="repeat_rule", severity="high",
                points=10, details={"summary": f"flash {n}"})
        Flag.objects.create(
            user=user, rule_code="other_rule", severity="low",
            points=5, details={"summary": "one off"})
        response = self.client.get(reverse("climbs:board-admin"))
        content = response.content.decode()
        self.assertContains(response, "repeat_rule")
        self.assertContains(response, "×3")
        self.assertContains(response, "+30 pts")
        self.assertEqual(content.count("repeat_rule"), 1)
        self.assertContains(response, "Needs review (4)")

    def test_leaderboard_shows_total_climbs(self):
        # The board names how many climbs the set holds, and your
        # line counts your sends against it.
        user = self.make_user("counter")
        for name in ("One", "Two", "Three"):
            climb = self.make_climb(name=name)
            if name == "One":
                climb.ascents.create(
                    user=user, tries=1,
                    points=calculate_points("V4", 1))
        self.client.force_login(user)
        response = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(response, "3 climbs")
        self.assertContains(response, "1 of 3 sends")

    def test_flag_numbers_explain_themselves(self):
        # The badge total and each flag's weight are labelled: the
        # badge title says what suspicion adds up to, every flag
        # shows its own +N pts, and a legend line states the cap.
        from climbs.models import Flag
        staff = self.make_user("ptsstaff", staff=True)
        self.client.force_login(staff)
        user = self.make_user("ptsuser")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        Flag.objects.create(
            user=user, rule_code="high_flash_rate", severity="high",
            points=30, details={"summary": "9/10 sends were flashes"})
        response = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(response, "⚑ 30")
        self.assertContains(response, "+30 pts")
        self.assertContains(response, "capped at 100")
        self.assertContains(response, "Suspicion 30")

    def test_review_many_marks_open_reviewed(self):
        # One action clears a row's open flags with a single note;
        # already-closed flags are skipped and reported. Staff
        # only, like the single-flag API.
        from climbs.models import Flag, ModerationLog
        user = self.make_user("bulkuser")
        open_one = Flag.objects.create(
            user=user, rule_code="rule_a", severity="low",
            points=5, details={"summary": "a"})
        open_two = Flag.objects.create(
            user=user, rule_code="rule_b", severity="high",
            points=30, details={"summary": "b"})
        closed = Flag.objects.create(
            user=user, rule_code="rule_c", severity="low",
            points=5, details={"summary": "c"}, status="reviewed")
        url = reverse("climbs:flags-review-many")
        payload = {"ids": [open_one.id, open_two.id, closed.id],
                   "note": "looks fine"}
        logged_out = self.client.post(
            url, json.dumps(payload), content_type="application/json")
        self.assertEqual(logged_out.status_code, 404)
        staff = self.make_user("bulkstaff", staff=True)
        self.client.force_login(staff)
        bad = self.client.post(
            url, json.dumps({"ids": []}),
            content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        response = self.client.post(
            url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "reviewed": 2,
                                           "skipped": 1})
        for flag_id in (open_one.id, open_two.id):
            flag = Flag.objects.get(id=flag_id)
            self.assertEqual(flag.status, "reviewed")
            self.assertEqual(flag.note, "looks fine")
            self.assertEqual(flag.reviewed_by, staff)
        self.assertTrue(ModerationLog.objects.filter(
            action="flag_reviewed",
            details__flag_id=open_one.id).exists())
        board = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(board, "data-review-all")

    def test_admin_page_clamps_drags_in_script(self):
        # Drags and placements clamp inside the wall photo (the same
        # bounds the server enforces), so a marker can never be
        # stranded off-map where it can't be recovered.
        staff = self.make_user("clampscript", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:climb-admin"))
        self.assertContains(response, "clampPt(svgPoint(moveEvt))")
        self.assertContains(response, "clampPt(svgPoint(e))")

    def test_admin_tap_opens_panel_not_drag_save(self):
        # Marker taps open the edit panel, never a stray save: past
        # 4px of movement the gesture becomes a drag (the pan layer
        # paused, the new position saved on release), otherwise the
        # release opens the panel. Wall taps use 12px / 600ms.
        import re
        src = open("climbs/templates/climbs/climb_admin.html").read()
        script = re.search(
            r"<script>\nconst svg =[\s\S]*?</script>", src).group(0)
        self.assertIn("marker.addEventListener('pointerdown'", script)
        self.assertIn("moveEvt.clientX - startX", script)
        self.assertIn("< 4) return;", script)
        self.assertIn("wallZoom.pause();", script)
        self.assertIn("wallZoom.resume();", script)
        self.assertIn("openPanel(id, x, y, marker.dataset);", script)
        self.assertIn("if (moved > 12 || held > 600) return;", script)

    def test_admin_inline_script_parses(self):
        # Regression: a dropped brace in the panel code failed the
        # whole script block to parse, silently killing zoom, taps,
        # drags and placement together. The rendered page script
        # must parse (skipped where node is unavailable).
        import os
        import re
        import shutil
        import subprocess
        import tempfile
        if shutil.which("node") is None:
            self.skipTest("node unavailable")
        staff = self.make_user("jsparse", staff=True)
        self.client.force_login(staff)
        content = self.client.get(
            reverse("climbs:climb-admin")).content.decode()
        script = re.search(
            r"<script>\nconst svg =.*?</script>", content,
            re.DOTALL).group(0)
        script = script.replace("<script>", "").replace(
            "</script>", "")
        with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False) as f:
            f.write(script)
            path = f.name
        try:
            proc = subprocess.run(
                ["node", "--check", path], capture_output=True,
                text=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_taps_reach_markers_and_sends_stay_put(self):
        # Regression: the shared module grabbed the pointer on every
        # pointerdown, retargeting pointerup to the viewport so marker
        # taps did nothing. Capture now waits for a real drag, and
        # logging a send updates the sheet in place (no reload).
        module = finders.find("climbs/wall-view.js")
        with open(module) as f:
            js = f.read()
        pointerdown = js.split("addEventListener('pointerdown'")[1]
        self.assertNotIn("setPointerCapture", pointerdown.split(
            "addEventListener('pointermove'")[0])
        self.assertIn("moved > 12", js)
        response = self.client.get(reverse("climbs:map"))
        self.assertNotContains(response, "location.reload()")
        self.assertContains(response, "SENT_IDS.add(parseInt(id, 10))")
        self.assertContains(response, "SENT_IDS.delete(parseInt(id, 10))")
        self.assertContains(response, "await refreshPopup()")

    def test_admin_map_comes_first_on_phones(self):
        # The climb list pushed the map far down on phones: below
        # 900px the map block orders first, inspector below, lifted
        # clear of the screen edge with system-bar clearance.
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        block = css.split("@media (max-width: 899px)")[1].split("}")[0]
        self.assertIn(".admin-main", block)
        self.assertIn("order", block)
        self.assertIn("-1", block)
        phone = css.split("@media (max-width: 899px)")[1].split(
            "@media")[0]
        self.assertIn(".admin-layout", phone)
        self.assertIn("padding-bottom", phone)
        self.assertIn("safe-area-inset-bottom", phone)

    def test_deep_zoom_stays_put(self):
        # Zooming into a corner once jumped the image off-screen: the
        # old stack transform, stale bounds and a re-entrant nudge
        # mid-gesture fought the pointer. The shared module clamps
        # the pan after every update (smaller axes centred, larger
        # axes kept inside), zooms around the pointer/pinch centre,
        # and never re-enters a transform mid-gesture.
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "WallView.create")
        self.assertContains(response, "map-content")
        self.assertNotContains(response, "panzoom")
        module = finders.find("climbs/wall-view.js")
        with open(module) as f:
            js = f.read()
        self.assertIn("clampPan", js)
        self.assertIn("keep the edges inside", js)
        # Intended behaviour stays: bounded pan/zoom at up to 8x.
        self.assertIn("MAX_SCALE = 8", js)
        css_path = finders.find("climbs/style.css")
        self.assertIsNotNone(css_path)
        with open(css_path) as f:
            css = f.read()
        stack_block = css.split("#map-stack")[1].split("}")[0]
        self.assertIn("touch-action: none", stack_block)
        content_block = css.split("#map-content")[1].split("}")[0]
        self.assertIn("transform-origin: 0 0", content_block)
        self.assertIn("transition: none", content_block)

    def test_map_zooms_out_past_fit(self):
        # The main map zooms out a little past the fit-to-wall scale
        # so the whole wall sits in view with space around it.
        module = finders.find("climbs/wall-view.js")
        with open(module) as f:
            js = f.read()
        self.assertIn("0.8 * Math.min(1", js)


class AntiCheatTests(ClimbTestMixin, TestCase):
    def make_climbs(self, n, grade="V3", wall="main"):
        return [self.make_climb(name=f"AC{i}", grade=grade, wall=wall)
                for i in range(n)]

    def test_log_and_undo_write_audit_rows(self):
        from climbs.models import AscentAudit
        user = self.make_user("audited")
        climb = self.make_climb()
        self.client.force_login(user)
        url = reverse("climbs:climb-ascent", args=[climb.id])
        self.client.post(url, json.dumps({"tries": 2}),
                         content_type="application/json")
        create = AscentAudit.objects.get(user=user, action="create")
        self.assertEqual(create.new_tries, 2)
        self.assertEqual(create.new_grade, "V4")
        self.assertEqual(create.actor, user)
        self.assertIsNotNone(create.created_at)
        self.client.delete(url)
        delete = AscentAudit.objects.get(user=user, action="delete")
        self.assertEqual(delete.old_tries, 2)
        self.assertEqual(delete.actor, user)

    def test_high_flash_rate_flags(self):
        from climbs.anticheat import suspicion_score
        user = self.make_user("flasher")
        for climb in self.make_climbs(8):
            climb.ascents.create(user=user, tries=1,
                                 points=calculate_points("V3", 1))
        from django.core.management import call_command
        call_command("recompute_flags")
        flags = user.flags.filter(status="open",
                                  rule_code="high_flash_rate")
        self.assertTrue(flags.exists())
        self.assertEqual(flags.first().severity, "high")
        self.assertLessEqual(suspicion_score(user), 100)

    def test_tries_edited_down_flags_from_audit(self):
        from climbs.models import Ascent, AscentAudit
        user = self.make_user("editor")
        climb = self.make_climb()
        ascent = climb.ascents.create(
            user=user, tries=8, points=calculate_points("V4", 8))
        AscentAudit.objects.create(
            user=user, ascent=ascent, climb=climb, actor=user,
            action="edit", old_tries=8,
            new_tries=2, old_grade="V4", new_grade="V4",
            old_points=calculate_points("V4", 8),
            new_points=calculate_points("V4", 2))
        self.assertEqual(Ascent.objects.count(), 1)
        from django.core.management import call_command
        call_command("recompute_flags")
        self.assertTrue(user.flags.filter(
            status="open", rule_code="tries_edited_down").exists())

    def test_rate_limit_blocks_bulk_logging(self):
        from django.test import override_settings
        user = self.make_user("speedy")
        self.client.force_login(user)
        with override_settings(
                ANTI_CHEAT={"ascent_rate_limit_per_hour": 2,
                            "min_users_for_peer_stats": 5}):
            for climb in self.make_climbs(2):
                response = self.client.post(
                    reverse("climbs:climb-ascent", args=[climb.id]),
                    json.dumps({"tries": 1}),
                    content_type="application/json")
                self.assertEqual(response.status_code, 200)
            third = self.make_climb(name="Third")
            response = self.client.post(
                reverse("climbs:climb-ascent", args=[third.id]),
                json.dumps({"tries": 1}), content_type="application/json")
            self.assertEqual(response.status_code, 429)

    def test_void_excludes_from_board_but_stays_in_log(self):
        from climbs.models import Ascent, ModerationLog
        user = self.make_user("voided")
        climb = self.make_climb()
        ascent = climb.ascents.create(
            user=user, tries=1, points=calculate_points("V4", 1))
        staff = self.make_user("voidstaff", staff=True)
        self.client.force_login(staff)
        # Reason required.
        no_reason = self.client.post(
            reverse("climbs:ascent-void", args=[ascent.id]),
            json.dumps({"void": True}), content_type="application/json")
        self.assertEqual(no_reason.status_code, 400)
        response = self.client.post(
            reverse("climbs:ascent-void", args=[ascent.id]),
            json.dumps({"void": True, "reason": "Needs review"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        ascent.refresh_from_db()
        self.assertTrue(ascent.is_voided)
        # Off the public board and out of totals...
        board = self.client.get(reverse("climbs:leaderboard"))
        self.assertNotContains(board, "voided")
        self.client.force_login(user)
        profile = self.client.get(reverse("accounts:profile"))
        self.assertContains(profile, '<span class="stat-num">0</span>')
        # ...but still in logs, and the action is logged.
        self.assertTrue(Ascent.objects.filter(id=ascent.id).exists())
        self.assertTrue(ModerationLog.objects.filter(
            action="void", username="voided").exists())
        # Restore puts the points back.
        self.client.force_login(staff)
        self.client.post(
            reverse("climbs:ascent-void", args=[ascent.id]),
            json.dumps({"void": False}), content_type="application/json")
        ascent.refresh_from_db()
        self.assertFalse(ascent.is_voided)

    def test_void_and_flag_apis_are_staff_only(self):
        user = self.make_user("sneaky")
        staff = self.make_user("sneakstaff", staff=True)
        climb = self.make_climb()
        ascent = climb.ascents.create(
            user=user, tries=1, points=calculate_points("V4", 1))
        from climbs.models import Flag
        flag = Flag.objects.create(
            user=user, rule_code="high_flash_rate", severity="high",
            points=30, details={"summary": "x"})
        for url, payload in (
                (reverse("climbs:ascent-void", args=[ascent.id]),
                 {"void": True, "reason": "x"}),
                (reverse("climbs:flag-review", args=[flag.id]),
                 {"status": "dismissed", "note": "x"}),
                (reverse("climbs:user-hide", args=["sneaky"]),
                 {"hidden": True}),
                (reverse("climbs:user-log", args=["sneaky"]), None)):
            with self.subTest(url=url):
                self.client.logout()
                response = (self.client.get(url) if payload is None
                            else self.client.post(
                                url, json.dumps(payload),
                                content_type="application/json"))
                self.assertEqual(response.status_code, 404)
                self.client.force_login(user)
                response = (self.client.get(url) if payload is None
                            else self.client.post(
                                url, json.dumps(payload),
                                content_type="application/json"))
                self.assertEqual(response.status_code, 404)
        self.client.force_login(staff)
        self.assertEqual(
            self.client.get(
                reverse("climbs:user-log", args=["sneaky"])).status_code,
            200)

    def test_flag_review_needs_note_and_logs(self):
        from climbs.models import Flag, ModerationLog
        user = self.make_user("reviewed")
        flag = Flag.objects.create(
            user=user, rule_code="high_flash_rate", severity="high",
            points=30, details={"summary": "9/10 flashes"})
        staff = self.make_user("reviewer", staff=True)
        self.client.force_login(staff)
        no_note = self.client.post(
            reverse("climbs:flag-review", args=[flag.id]),
            json.dumps({"status": "dismissed"}),
            content_type="application/json")
        self.assertEqual(no_note.status_code, 400)
        response = self.client.post(
            reverse("climbs:flag-review", args=[flag.id]),
            json.dumps({"status": "dismissed",
                        "note": "Checked footage, fine"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        flag.refresh_from_db()
        self.assertEqual(flag.status, "dismissed")
        self.assertEqual(flag.reviewed_by, staff)
        self.assertTrue(ModerationLog.objects.filter(
            action="flag_dismissed", username="reviewed").exists())

    def test_hide_user_from_board_and_restore(self):
        user = self.make_user("limbo")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        staff = self.make_user("hider", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:user-hide", args=["limbo"]),
            json.dumps({"hidden": True, "note": "Reviewing"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        board = self.client.get(reverse("climbs:leaderboard"))
        self.assertNotContains(board, "limbo")
        self.client.post(
            reverse("climbs:user-hide", args=["limbo"]),
            json.dumps({"hidden": False}),
            content_type="application/json")
        board = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(board, "limbo")

    def test_board_admin_shows_flags_and_filters(self):
        from climbs.models import Flag
        user = self.make_user("flagged")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        Flag.objects.create(
            user=user, rule_code="high_flash_rate", severity="high",
            points=30, details={"summary": "9/10 sends were flashes"})
        staff = self.make_user("flagstaff", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(response, "Flags")
        self.assertContains(response, "⚑ 30")
        self.assertContains(response, "Needs review")
        self.assertContains(response, "9/10 sends were flashes")
        self.assertContains(response, "data-flag-dismiss")
        # The climber opens their per-set log in the card, not the
        # staff user-log page (staff reach that from the card).
        self.assertContains(response, 'data-username="flagged"')
        self.assertContains(response, "data-set-id=")
        flagged = self.client.get(
            reverse("climbs:board-admin") + "?flagged=1")
        self.assertContains(flagged, "flagged")
        susp = self.client.get(
            reverse("climbs:board-admin") + "?suspicion=20")
        self.assertContains(susp, "flagged")

    def test_set_user_sends_returns_set_log(self):
        # Tapping a board name shows every climb of theirs on that
        # set: climb, grade, tries, points and time, newest first.
        # Voided sends stay out; mystery grades stay '?'.
        user = self.make_user("logger")
        first = self.make_climb(name="First", grade="V2")
        first.ascents.create(user=user, tries=2,
                             points=calculate_points("V2", 2))
        second = self.make_climb(name="Second", grade="V5")
        second.ascents.create(user=user, tries=1,
                              points=calculate_points("V5", 1))
        gone = self.make_climb(name="Gone", grade="V1")
        bad = gone.ascents.create(user=user, tries=1,
                                  points=calculate_points("V1", 1))
        bad.is_voided = True
        bad.save()
        secret = self.make_climb(name="Secret", grade="V6",
                                 tag="mystery")
        secret.ascents.create(
            user=user, tries=3,
            points=calculate_points("V6", 3, tag="mystery"))
        set_id = first.climb_set.id
        response = self.client.get(reverse(
            "climbs:set-user-sends", args=[set_id, "logger"]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["username"], "logger")
        got = [(s["climb"], s["grade"], s["tries"], s["points"])
               for s in data["sends"]]
        self.assertEqual(got, [
            ("Secret", "?", 3, MYSTERY_FIXED_POINTS),
            ("Second", "V5", 1, calculate_points("V5", 1)),
            ("First", "V2", 2, calculate_points("V2", 2)),
        ])
        self.assertTrue(
            all("time" in s and s["time"] for s in data["sends"]))
        self.assertEqual(self.client.get(reverse(
            "climbs:set-user-sends",
            args=[999999, "logger"])).status_code, 404)
        self.assertEqual(self.client.get(reverse(
            "climbs:set-user-sends",
            args=[set_id, "nobody"])).status_code, 404)

    def test_set_user_sends_hides_hidden_users(self):
        # Board-hidden climbers stay private to everyone but staff.
        user = self.make_user("shy")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        user.leaderboard_hidden = True
        user.save()
        url = reverse("climbs:set-user-sends",
                      args=[climb.climb_set.id, "shy"])
        self.assertEqual(self.client.get(url).status_code, 404)
        staff = self.make_user("shystaff", staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_board_chips_carry_set_context(self):
        # Both leaderboards open the per-set log: chips carry the
        # viewed set, the card fetches it, staff get a full-log
        # link inside the card.
        user = self.make_user("chippy")
        climb = self.make_climb()
        climb.ascents.create(user=user, tries=1,
                             points=calculate_points("V4", 1))
        board = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(board, 'data-username="chippy"')
        self.assertContains(
            board, f'data-set-id="{climb.climb_set.id}"')
        self.assertContains(board, "chip.dataset.setId")
        staff = self.make_user("chipstaff", staff=True)
        self.client.force_login(staff)
        admin = self.client.get(reverse("climbs:board-admin"))
        self.assertContains(admin, 'data-username="chippy"')
        self.assertContains(
            admin, f'data-set-id="{climb.climb_set.id}"')
        self.assertContains(admin, "Full log")

    def test_user_log_page_and_csv(self):
        from climbs.models import Flag
        user = self.make_user("logged")
        climb = self.make_climb(name="Logged Climb", grade="V4")
        ascent = climb.ascents.create(
            user=user, tries=2, points=calculate_points("V4", 2))
        Flag.objects.create(
            user=user, ascent=ascent, rule_code="big_grade_flash",
            severity="medium", points=15,
            details={"summary": "Flashed V4"})
        staff = self.make_user("logstaff", staff=True)
        self.client.force_login(staff)
        response = self.client.get(
            reverse("climbs:user-log", args=["logged"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Logged Climb")
        self.assertContains(response, "Europe/London")
        self.assertContains(response, "red")
        self.assertContains(response, "V4")
        self.assertContains(response, "big_grade_flash")
        self.assertContains(response, "Edit history")
        csv_response = self.client.get(
            reverse("climbs:user-log", args=["logged"]) + "?format=csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response["Content-Type"])
        self.assertIn("Logged Climb",
                      csv_response.content.decode())
        flagged = self.client.get(
            reverse("climbs:user-log", args=["logged"])
            + "?flagged=flagged")
        self.assertContains(flagged, "Logged Climb")
        unflagged = self.client.get(
            reverse("climbs:user-log", args=["logged"])
            + "?flagged=unflagged")
        self.assertNotContains(unflagged, "Logged Climb")

    def test_odd_hours_follow_gym_opening_times(self):
        # The gym opens 10:00–22:00: a 3am send needs review, a
        # midday send does not.
        import datetime

        from django.utils.timezone import make_aware
        user = self.make_user("nightowl")
        climb = self.make_climb()
        ascent = climb.ascents.create(
            user=user, tries=2, points=calculate_points("V4", 2))
        Ascent.objects.filter(id=ascent.id).update(
            logged_at=make_aware(datetime.datetime(2026, 1, 15, 3, 0)))
        from climbs.anticheat import refresh_user_flags
        refresh_user_flags(user)
        self.assertTrue(user.flags.filter(
            status="open", rule_code="bulk_or_odd_hours").exists())
        Ascent.objects.filter(id=ascent.id).update(
            logged_at=make_aware(datetime.datetime(2026, 1, 15, 12, 0)))
        refresh_user_flags(user)
        self.assertFalse(user.flags.filter(
            status="open", rule_code="bulk_or_odd_hours").exists())

    def test_recompute_flags_command(self):
        import io

        from django.core.management import call_command

        user = self.make_user("recomputed")
        for climb in self.make_climbs(8):
            climb.ascents.create(user=user, tries=1,
                                 points=calculate_points("V3", 1))
        out = io.StringIO()
        call_command("recompute_flags", stdout=out)
        self.assertIn("Checked", out.getvalue())
        self.assertTrue(user.flags.filter(status="open").exists())
        # Re-running regenerates open flags without duplicating
        # reviewed history.
        flag = user.flags.filter(status="open").first()
        flag.status = "dismissed"
        flag.save()
        call_command("recompute_flags", stdout=out)
        self.assertEqual(
            user.flags.filter(status="dismissed").count(), 1)

    def _log_series(self, user, climbs, start, step_seconds, tries=2):
        import datetime

        from django.utils.timezone import make_aware
        for i, climb in enumerate(climbs):
            ascent = climb.ascents.create(
                user=user, tries=tries,
                points=calculate_points(climb.grade, tries))
            ts = make_aware(
                start + datetime.timedelta(seconds=i * step_seconds))
            Ascent.objects.filter(id=ascent.id).update(logged_at=ts)

    def test_send_rate_outlier_flags_burst_day(self):
        # 8 sends in 4 minutes (2/min) against slow peers flags;
        # a slow day does not.
        import datetime

        from climbs.anticheat import refresh_user_flags
        fast = self.make_user("bursty")
        day = datetime.datetime(2026, 2, 10, 12, 0)
        self._log_series(fast, self.make_climbs(8), day, 34)
        for n in range(10):
            peer = self.make_user(f"slowrate{n}")
            for d in (10, 11):
                self._log_series(
                    peer, self.make_climbs(5),
                    datetime.datetime(2026, 2, d, 12, 0), 2000)
        refresh_user_flags(fast)
        self.assertTrue(fast.flags.filter(
            status="open", rule_code="send_rate_outlier").exists())
        slow = self.make_user("slowday")
        self._log_series(
            slow, self.make_climbs(6),
            datetime.datetime(2026, 2, 12, 12, 0), 2000)
        refresh_user_flags(slow)
        self.assertFalse(slow.flags.filter(
            status="open", rule_code="send_rate_outlier").exists())

    def test_grade_pace_floor_weights_hard_grades(self):
        # V6 in 3 tries a minute after the previous send is under its
        # grade floor; a V0 single try with room to breathe is not.
        import datetime

        from django.utils.timezone import make_aware
        from climbs.anticheat import refresh_user_flags
        user = self.make_user("pacefloor")
        climbs = self.make_climbs(2, grade="V6")
        first = climbs[0].ascents.create(
            user=user, tries=3, points=calculate_points("V6", 3))
        second = climbs[1].ascents.create(
            user=user, tries=3, points=calculate_points("V6", 3))
        Ascent.objects.filter(id=first.id).update(
            logged_at=make_aware(datetime.datetime(2026, 3, 1, 12, 0)))
        Ascent.objects.filter(id=second.id).update(
            logged_at=make_aware(datetime.datetime(2026, 3, 1, 12, 1)))
        refresh_user_flags(user)
        self.assertTrue(user.flags.filter(
            status="open", rule_code="grade_pace_floor").exists())
        easy = self.make_user("easydoesit")
        pair = self.make_climbs(2, grade="V0")
        one = pair[0].ascents.create(
            user=easy, tries=1, points=calculate_points("V0", 1))
        two = pair[1].ascents.create(
            user=easy, tries=1, points=calculate_points("V0", 1))
        Ascent.objects.filter(id=one.id).update(
            logged_at=make_aware(datetime.datetime(2026, 3, 1, 12, 0)))
        Ascent.objects.filter(id=two.id).update(
            logged_at=make_aware(datetime.datetime(2026, 3, 1, 12, 2)))
        refresh_user_flags(easy)
        self.assertFalse(easy.flags.filter(
            status="open", rule_code="grade_pace_floor").exists())

    def test_flash_drift_flags_sudden_spike(self):
        # Ten project-free grinds then ten flashes in two days: a
        # flash-rate spike paired with a burst. Steady climbers pass.
        import datetime

        from climbs.anticheat import refresh_user_flags
        user = self.make_user("spiky")
        self._log_series(
            user, self.make_climbs(10),
            datetime.datetime(2026, 1, 1, 12, 0), 86400, tries=3)
        self._log_series(
            user, self.make_climbs(10),
            datetime.datetime(2026, 4, 1, 12, 0),
            int(2 * 86400 / 9), tries=1)
        refresh_user_flags(user)
        self.assertTrue(user.flags.filter(
            status="open", rule_code="flash_drift").exists())
        steady = self.make_user("steady")
        self._log_series(
            steady, self.make_climbs(20),
            datetime.datetime(2026, 1, 1, 12, 0), 86400, tries=2)
        refresh_user_flags(steady)
        self.assertFalse(steady.flags.filter(
            status="open", rule_code="flash_drift").exists())

    def test_grade_jump_velocity_flags_fast_progression(self):
        # V2 history then V6 inside a week flags; V2 to V3 does not.
        import datetime

        from django.utils.timezone import make_aware
        from climbs.anticheat import refresh_user_flags
        user = self.make_user("rocketeer")
        for i, climb in enumerate(self.make_climbs(6, grade="V2")):
            ascent = climb.ascents.create(
                user=user, tries=2, points=calculate_points("V2", 2))
            Ascent.objects.filter(id=ascent.id).update(
                logged_at=make_aware(
                    datetime.datetime(2026, 1, 5, 12, 0)
                    + datetime.timedelta(days=i)))
        for grade in ("V5", "V6"):
            climb = self.make_climb(name=f"Jump {grade}", grade=grade)
            ascent = climb.ascents.create(
                user=user, tries=2,
                points=calculate_points(grade, 2))
            Ascent.objects.filter(id=ascent.id).update(
                logged_at=make_aware(
                    datetime.datetime.now().replace(
                        hour=12, minute=0, second=0,
                        microsecond=0)))
        refresh_user_flags(user)
        self.assertTrue(user.flags.filter(
            status="open",
            rule_code="grade_jump_velocity").exists())
        gradual = self.make_user("gradual")
        for i, climb in enumerate(self.make_climbs(6, grade="V2")):
            ascent = climb.ascents.create(
                user=gradual, tries=2,
                points=calculate_points("V2", 2))
            Ascent.objects.filter(id=ascent.id).update(
                logged_at=make_aware(
                    datetime.datetime(2026, 1, 5, 12, 0)
                    + datetime.timedelta(days=i)))
        third = self.make_climb(name="Step", grade="V3")
        ascent = third.ascents.create(
            user=gradual, tries=2, points=calculate_points("V3", 2))
        Ascent.objects.filter(id=ascent.id).update(
            logged_at=make_aware(
                datetime.datetime.now().replace(
                    hour=12, minute=0, second=0, microsecond=0)))
        refresh_user_flags(gradual)
        self.assertFalse(gradual.flags.filter(
            status="open",
            rule_code="grade_jump_velocity").exists())

    def test_session_clustering_flags_batch_logging(self):
        # Eight sends ten seconds apart reads as logged after the
        # fact; eight sends across a day reads as a real session.
        import datetime

        from climbs.anticheat import refresh_user_flags
        batch = self.make_user("batchlogger")
        self._log_series(
            batch, self.make_climbs(8),
            datetime.datetime(2026, 5, 1, 12, 0), 10)
        refresh_user_flags(batch)
        self.assertTrue(batch.flags.filter(
            status="open",
            rule_code="session_clustering").exists())
        live = self.make_user("livesession")
        self._log_series(
            live, self.make_climbs(8),
            datetime.datetime(2026, 5, 1, 10, 0), 1800)
        refresh_user_flags(live)
        self.assertFalse(live.flags.filter(
            status="open",
            rule_code="session_clustering").exists())

    def test_peer_z_score_needs_a_pattern(self):
        # Five 8-send visits against 1-send peers is a pattern, not
        # one great day; the peers themselves stay clean.
        import datetime

        from django.utils.timezone import make_aware
        from climbs.anticheat import refresh_user_flags
        for n in range(12):
            peer = self.make_user(f"zpeer{n}")
            for d in (1, 2):
                climbs = self.make_climbs(1)
                ascent = climbs[0].ascents.create(
                    user=peer, tries=2,
                    points=calculate_points("V3", 2))
                Ascent.objects.filter(id=ascent.id).update(
                    logged_at=make_aware(
                        datetime.datetime(2026, 6, d, 12, 0)))
        star = self.make_user("zstar")
        for d in range(1, 6):
            self._log_series(
                star, self.make_climbs(8),
                datetime.datetime(2026, 6, d, 10, 0), 1800)
        refresh_user_flags(star)
        self.assertTrue(star.flags.filter(
            status="open", rule_code="peer_z_score").exists())
        peer = self.make_user("zoneoff")
        self._log_series(
            peer, self.make_climbs(2),
            datetime.datetime(2026, 6, 1, 12, 0), 1800)
        refresh_user_flags(peer)
        self.assertFalse(peer.flags.filter(
            status="open", rule_code="peer_z_score").exists())


class ColourTests(ClimbTestMixin, TestCase):
    def test_preset_names_and_hexes_resolve(self):
        from climbs.models import colour_display_name
        self.assertEqual(colour_display_name("orange"), "orange")
        self.assertEqual(colour_display_name("#f08c00"), "orange")
        self.assertEqual(colour_display_name("Sky Blue"), "sky blue")

    def test_unknown_hex_resolves_to_nearest_never_raw(self):
        from climbs.models import colour_display_name
        self.assertEqual(colour_display_name("#c9265f"), "magenta")
        self.assertNotIn(
            "#", colour_display_name("#123456").replace("sky blue", ""))

    def test_grade_text_contrasts(self):
        from climbs.models import colour_text_on
        self.assertEqual(colour_text_on("white"), "#000000")
        self.assertEqual(colour_text_on("yellow"), "#000000")
        self.assertEqual(colour_text_on("black"), "#ffffff")
        self.assertEqual(colour_text_on("navy"), "#ffffff")

    def test_map_and_admin_markers_match(self):
        # Same climb, same marker on both pages: the stored colour
        # rendered raw, white halo grade text, and no contrast
        # re-toning on the public map.
        self.make_climb(name="Same", grade="V4")
        response = self.client.get(reverse("climbs:map"))
        self.assertContains(response, 'fill="red"')
        self.assertNotContains(response, 'fill="#e03131"')
        self.assertContains(response, "WallView.rescaleMarkers")
        self.assertNotContains(response, "applyMarkerContrast")
        staff = self.make_user("parity", staff=True)
        self.client.force_login(staff)
        admin = self.client.get(reverse("climbs:climb-admin"))
        self.assertContains(admin, 'fill="red"')

    def test_admin_saves_preset_name(self):
        staff = self.make_user("colourist", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:climb-create"),
            json.dumps({"name": "", "grade": "V2", "tag": "red",
                        "colour": "orange", "wall": "main",
                        "x_percent": 10, "y_percent": 20}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        climb = Climb.objects.get(id=response.json()["id"])
        self.assertEqual(climb.colour, "orange")
        self.assertEqual(climb.colour_name, "orange")
        # Once sent, the stats name the tape; no raw hex in labels.
        climber = self.make_user("tangerine")
        climb.ascents.create(user=climber, tries=1,
                             points=calculate_points("V2", 1))
        board = self.client.get(reverse("climbs:leaderboard"))
        self.assertContains(board, ">orange</button>")
        self.assertNotContains(board, "#e03131</button>")


class NewsTests(ClimbTestMixin, TestCase):
    def make_post(self, title="Set date", body="Fresh plastic Monday."):
        return NewsPost.objects.create(title=title, body=body)

    def test_news_lists_newest_first_with_dates(self):
        first = self.make_post(title="First post")
        second = self.make_post(title="Second post")
        response = self.client.get(reverse("climbs:news"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(
            content.index("Second post"), content.index("First post"))
        for post in (first, second):
            self.assertContains(
                response,
                f"{post.created_at.day} {post.created_at.strftime('%b %Y')}")
        # The News tab marks itself current, exactly once.
        self.assertEqual(content.count('aria-current="page"'), 1)
        pos = content.index('class="nav-link active"')
        self.assertIn("News", content[pos:pos + 1200])

    def test_news_paginates_back(self):
        for i in range(7):
            self.make_post(title=f"Post {i:02d}")
        first = self.client.get(reverse("climbs:news"))
        self.assertEqual(first.content.decode().count("<article"), 5)
        self.assertContains(first, "Page 1 of 2")
        self.assertContains(first, "Older →")
        second = self.client.get(reverse("climbs:news") + "?page=2")
        self.assertEqual(second.content.decode().count("<article"), 2)
        self.assertContains(second, "Page 2 of 2")
        self.assertContains(second, "← Newer")

    def test_staff_can_post_edit_delete(self):
        staff = self.make_user("editor", staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("climbs:news-create"),
            json.dumps({"title": "  Comp night  ",
                        "body": "Friday."}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        post = NewsPost.objects.get(id=response.json()["id"])
        self.assertEqual(post.title, "Comp night")
        self.assertEqual(post.author, staff)
        bad = self.client.post(
            reverse("climbs:news-create"),
            json.dumps({"title": "  ", "body": "x"}),
            content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        edit = self.client.post(
            reverse("climbs:news-update", args=[post.id]),
            json.dumps({"title": "Comp night!", "body": "Friday!"}),
            content_type="application/json")
        self.assertEqual(edit.status_code, 200)
        post.refresh_from_db()
        self.assertEqual(post.title, "Comp night!")
        delete = self.client.post(
            reverse("climbs:news-delete", args=[post.id]),
            content_type="application/json")
        self.assertEqual(delete.status_code, 200)
        self.assertFalse(NewsPost.objects.filter(id=post.id).exists())

    def test_non_staff_cannot_use_news_apis(self):
        post = self.make_post()
        member = self.make_user("member")
        payload = json.dumps({"title": "Hi", "body": "There"})
        for url in (reverse("climbs:news-create"),
                    reverse("climbs:news-update", args=[post.id]),
                    reverse("climbs:news-delete", args=[post.id])):
            with self.subTest(url=url):
                response = self.client.post(
                    url, payload, content_type="application/json")
                self.assertIn(response.status_code, (302, 403))
                self.client.force_login(member)
                response = self.client.post(
                    url, payload, content_type="application/json")
                self.assertIn(response.status_code, (302, 403))
                self.client.logout()

    def test_post_form_shows_for_staff_only(self):
        self.make_post(title="Hello")
        member = self.make_user("reader")
        self.client.force_login(member)
        response = self.client.get(reverse("climbs:news"))
        self.assertNotContains(response, 'id="news-publish"')
        self.assertNotContains(response, "data-edit-post")
        self.assertNotContains(response, "data-del-post")
        staff = self.make_user("writer", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:news"))
        self.assertContains(response, 'id="news-publish"')
        self.assertContains(response, "data-edit-post")
        self.assertContains(response, "data-del-post")
