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

    def test_tries_capped_at_five(self):
        user = self.make_user("capped")
        climb = self.make_climb(grade="V4")
        self.client.force_login(user)
        response = self.client.post(
            reverse("climbs:climb-ascent", args=[climb.id]),
            json.dumps({"tries": 9}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tries"], 5)
        self.assertEqual(response.json()["points"],
                         calculate_points("V4", 5))

    def test_points_cap_at_five_tries(self):
        self.assertEqual(calculate_points("V4", 5),
                         calculate_points("V4", 9))
        self.assertEqual(calculate_points("V4", 5), round(400 * 0.6))

    def test_bad_tries_rejected(self):
        user = self.make_user("badtry")
        climb = self.make_climb()
        self.client.force_login(user)
        response = self.client.post(
            reverse("climbs:climb-ascent", args=[climb.id]),
            json.dumps({"tries": 0}), content_type="application/json")
        self.assertEqual(response.status_code, 400)


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

    def test_quick_colours_cover_more_tape(self):
        staff = self.make_user("swatches", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:climb-admin"))
        for colour in ("teal", "cyan", "magenta", "brown",
                       "grey", "navy", "maroon", "coral"):
            self.assertContains(response, f"'{colour}'")
        # The custom picker stays alongside the swatches.
        self.assertContains(response, 'type="color"')

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
        # swatch instead of a raw hex code, and the graph lists
        # only grades that actually have sends.
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
        self.assertContains(response, "#c9265f</button>")
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn("--dot-radius", css)
        self.assertContains(response, '<span class="graph-grade">V2</span>')
        self.assertNotContains(
            response, '<span class="graph-grade">V5</span>')


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
        # The tag drives the grade shortlist; colours are picked.
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
        # The bottom-docked edit panel caps itself to the viewport
        # and scrolls inside, so it never runs off the top of a
        # phone screen.
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        block = css.split("#climb-edit-panel")[1].split("}")[0]
        self.assertIn("max-height", block)
        self.assertIn("100dvh", block)
        self.assertIn("overflow-y: auto", block)

    def test_admin_map_supports_zoom_for_fine_placement(self):
        # The admin wall zooms (wheel/pinch) so markers can be placed
        # precisely: pan/zoom wiring, markers that hold their
        # on-screen size while zoomed, and marker drags that pause
        # the pan layer instead of fighting it. No on-map zoom
        # buttons: they went unused.
        staff = self.make_user("zoomadmin", staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("climbs:climb-admin"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "panzoom")
        self.assertNotContains(response, "data-zoom-in")
        self.assertNotContains(response, "data-zoom-out")
        self.assertNotContains(response, "data-zoom-reset")
        self.assertNotContains(response, "zoom-controls")
        self.assertContains(response, "Math.pow(s, 0.55)")
        self.assertContains(response, "pz.pause")
        self.assertContains(response, "pz.resume")
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
        # Finger-sized invisible hit area around every marker.
        self.assertContains(response, "climb-hit")
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
        # Inner circles keep the setter's tape colour.
        self.assertContains(response, 'fill="red"')
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
        # Markers shrink a little when zoomed out (softer counter-scale).
        self.assertContains(response, "Math.pow(s, 0.55)")
        # The grade label scales in lockstep with the ring (same factor)
        # and is sized so the widest grade always sits inside it.
        self.assertContains(response, "BASE_FONT = 7")
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
        self.assertContains(response, "data-dir")
        self.assertContains(response, "p-more-comments")
        self.assertContains(response, "set-switcher")

    def test_zoom_settle_resharpens_markers(self):
        # Regression: after a wheel/pinch zoom the markers stayed soft
        # until the next pan committed a fresh transform. Wheel/pinch
        # fire no end event, so the template debounces the transform
        # stream and replays the cure automatically: a sub-pixel snap
        # through panzoom's own moveTo, aligned to device pixels.
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sharpenAfterZoom")
        self.assertContains(response, "devicePixelRatio")
        self.assertContains(response, "pz.moveTo(x, y)")

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

    def test_overlapping_markers_offer_picker(self):
        # Close-together markers overlap, so the topmost one always won
        # the tap. The map must ship a chooser: markers carry the name
        # and colour it needs, and a tap near several markers lists
        # every candidate instead of opening just one.
        first = self.make_climb(name="Near One", x=50.0, y=100.0)
        second = self.make_climb(name="Near Two", x=50.5, y=100.5)
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="marker-picker"')
        self.assertContains(response, 'id="picker-list"')
        self.assertContains(response, 'id="picker-close"')
        self.assertContains(response, "nearbyMarkers")
        self.assertContains(response, "showPicker")
        self.assertContains(response, "PICK_RADIUS")
        for climb in (first, second):
            self.assertContains(response, f'data-climb-id="{climb.id}"')
            self.assertContains(
                response, f'data-name="{climb.name}"')
            self.assertContains(response, 'data-colour="red"')
        css_path = finders.find("climbs/style.css")
        self.assertIsNotNone(css_path)
        with open(css_path) as f:
            css = f.read()
        self.assertIn("#marker-picker", css)
        self.assertIn("#marker-picker .pick", css)

    def test_deep_zoom_stays_put(self):
        # Zooming deep flung the map across the screen and dropped
        # the zoom on phones and desktops: panzoom got a re-entrant
        # nudge mid-gesture, full-speed pinches on chaotic touch
        # spans, and the browser fighting it for two-finger touches.
        response = self.client.get(reverse("climbs:map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pinchSpeed")
        self.assertContains(response, "settleNudge")
        self.assertContains(response, "mapPointerDown")
        self.assertContains(response, "lastTransformAt")
        # Intended behaviour stays: bounded pan/zoom at up to 8x.
        self.assertContains(response, "bounds: true")
        self.assertContains(response, "maxZoom: 8")
        css_path = finders.find("climbs/style.css")
        self.assertIsNotNone(css_path)
        with open(css_path) as f:
            css = f.read()
        stack_block = css.split("#map-stack")[1].split("}")[0]
        self.assertIn("touch-action: none", stack_block)


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
