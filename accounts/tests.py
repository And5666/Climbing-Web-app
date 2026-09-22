from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class LoginPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="login_user",
            email="login@example.com",
            password="test-pass-123",
        )

    def test_failed_login_shows_error_and_preserves_username(self):
        response = self.client.post(
            reverse("login"),
            {"username": "login_user", "password": "wrong"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "correct username and password")
        self.assertContains(response, 'value="login_user"')

    def test_next_parameter_survives_get(self):
        response = self.client.get(reverse("login") + "?next=/leaderboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="next"')

    def test_valid_login_redirects_and_authenticates(self):
        response = self.client.post(
            reverse("login"),
            {"username": "login_user", "password": "test-pass-123"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)


class SignupPageTests(TestCase):
    def test_signup_renders_styled_form(self):
        response = self.client.get(reverse("signup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "auth-form")
        self.assertContains(response, 'name="password1"')

    def test_valid_signup_creates_user_and_logs_in(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "new_user",
                "email": "new@example.com",
                "password1": "complex-pass-123",
                "password2": "complex-pass-123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            get_user_model().objects.filter(username="new_user").exists()
        )
        self.assertIn("_auth_user_id", self.client.session)


User = get_user_model()


class ProfilePageTests(TestCase):
    def make_user(self, username, password="test-pass-123"):
        return User.objects.create_user(
            username=username, email=f"{username}@example.com",
            password=password)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_profile_shows_sections_and_stats(self):
        self.make_user("prof")
        self.client.force_login(User.objects.get(username="prof"))
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Change password")
        self.assertContains(response, "Delete account")
        self.assertContains(response, '<span class="stat-num">0</span>')
        self.assertContains(response, "Leaderboard")
        # One solid sheet, not a stack of cards.
        self.assertContains(response, "profile-sheet")
        self.assertEqual(
            response.content.decode().count('<section class="card'), 1)

    def test_profile_hides_progress_graph(self):
        # The Progress graph lives on Full stats only; the profile
        # keeps totals, ranks and the link, with or without sends.
        from climbs.models import Climb, ClimbSet

        user = self.make_user("progress")
        wall_set = ClimbSet.objects.create(
            wall="main", label="Progress set", is_active=True)
        climb = Climb.objects.create(
            name="Easy", grade="V2", tag="red", colour="red",
            wall="main", climb_set=wall_set,
            x_percent=10.0, y_percent=10.0)
        climb.ascents.create(user=user, tries=1, points=10)
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Progress")
        self.assertNotContains(response, "progress-graph")
        self.assertContains(response, "Full stats")
        stats = self.client.get(reverse("accounts:stats"))
        self.assertContains(stats, "progress-graph")

    def test_profile_password_has_no_autofocus(self):
        # Django autofocuses the current-password field, which yanked
        # the page down to Change password on every visit.
        user = self.make_user("focus")
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertNotContains(response, "autofocus")
        self.assertContains(response, 'autocomplete="current-password"')
        self.assertContains(response, 'autocomplete="new-password"')
        self.assertContains(response, 'autocomplete="username"')

    def test_stats_page_full_set(self):
        # The stats page: totals plus best, a customizable monthly
        # graph, sends by grade, current ranks and recent sends.
        from datetime import timedelta

        from django.utils.timezone import now

        from climbs.models import Ascent, Climb, ClimbSet

        user = self.make_user("statty")
        wall_set = ClimbSet.objects.create(
            wall="main", label="Stats set", is_active=True)
        easy = Climb.objects.create(
            name="Easy", grade="V2", tag="red", colour="red",
            wall="main", climb_set=wall_set,
            x_percent=10.0, y_percent=10.0)
        hard = Climb.objects.create(
            name="Hard", grade="V4", tag="blue", colour="blue",
            wall="main", climb_set=wall_set,
            x_percent=20.0, y_percent=20.0)
        easy.ascents.create(user=user, tries=1, points=10)
        hard.ascents.create(user=user, tries=1, points=20)
        Ascent.objects.filter(user=user, climb=easy).update(
            logged_at=now() - timedelta(days=40))
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:stats"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "V4")
        self.assertContains(response, "Sends by grade")
        self.assertContains(response, "Recent sends")
        self.assertContains(response, "Hard")
        self.assertContains(response, "progress-graph")
        self.assertContains(response, 'name="metric"')
        self.assertContains(response, 'name="wall"')
        self.assertContains(response, 'name="range"')
        # Dates read day/month/year.
        self.assertContains(
            response, (now() - timedelta(days=40)).strftime("%d/%m/%Y"))
        # Sends metric shows counts, not grades.
        counted = self.client.get(
            reverse("accounts:stats") + "?metric=sends")
        self.assertContains(counted, 'progress-grade">1<')
        # Wall and range filters narrow the graph; junk falls back.
        walled = self.client.get(
            reverse("accounts:stats") + "?wall=cave")
        self.assertContains(walled, "Nothing here for these filters")
        six = self.client.get(
            reverse("accounts:stats") + "?range=6").content.decode().count(
                "progress-col")
        twelve = self.client.get(
            reverse("accounts:stats") + "?range=12").content.decode().count(
                "progress-col")
        self.assertEqual(six, 6)
        self.assertEqual(twelve, 12)
        fallback = self.client.get(
            reverse("accounts:stats") + "?metric=bogus&range=bogus")
        self.assertContains(fallback, "progress-graph")

    def test_stats_requires_login(self):
        response = self.client.get(reverse("accounts:stats"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_profile_links_to_stats(self):
        user = self.make_user("linky")
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, reverse("accounts:stats"))

    def test_logout_lives_on_profile_not_nav(self):
        # Log out is a POST form with CSRF on the profile page
        # (LogoutView needs POST); the nav keeps just the profile
        # link so it stays reachable on mobile.
        user = self.make_user("quitter")
        self.client.force_login(user)
        profile = self.client.get(reverse("accounts:profile"))
        self.assertContains(profile, 'action="/accounts/logout/"')
        self.assertContains(profile, "csrfmiddlewaretoken")
        self.assertContains(profile, ">Log out</button>")
        nav = self.client.get(reverse("climbs:map")).content.decode()
        self.assertIn(reverse("accounts:profile"), nav)
        self.assertNotIn("/accounts/logout/", nav)
        # The form actually logs out.
        self.client.post(reverse("logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_profile_footer_has_version_and_contact(self):
        # One version source, stamped in the footer with a working
        # mailto contact.
        from summit_map_project import VERSION
        user = self.make_user("footy")
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, f"Summit v{VERSION}")
        self.assertContains(response, "mailto:ac0566@proton.me")
        self.assertContains(response, "ac0566@proton.me")
        self.assertContains(response, "profile-footer")
        # Styled footer: version stamp plus a button contact.
        self.assertContains(response, 'class="ver"')
        self.assertContains(response, "btn btn-sm btn-secondary")
        from django.contrib.staticfiles import finders
        with open(finders.find("climbs/style.css")) as f:
            css = f.read()
        footer = css.split(".profile-footer {")[1].split("}")[0]
        self.assertIn("background", footer)
        self.assertIn("border", footer)

    def test_avatar_upload_uses_pick_button(self):
        # The raw file input hides behind a real button that shows
        # the chosen filename. A <label> pointing at an off-canvas
        # input swallowed first taps on mobile and stayed dead after
        # a cancelled pick, so the button opens the input in the
        # same tap instead.
        user = self.make_user("picker")
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, 'id="avatar-pick"')
        self.assertContains(response, 'for="id_avatar"')
        self.assertContains(response, "Choose picture")
        self.assertContains(response, "showPicker")
        self.assertContains(response, "input.click()")
        # One remove control: a hidden button (no stored photo) plus
        # a hidden removal flag for it to toggle.
        self.assertContains(response, 'id="avatar-remove"')
        self.assertContains(response, 'hidden>Remove photo')
        self.assertContains(response, 'type="hidden" name="remove_avatar"')
        # No Django Currently/Clear/Change widget alongside it.
        self.assertNotContains(response, "avatar-clear")

    def test_avatar_picker_recovers_from_bad_picks(self):
        # Cancelling or mis-picking must leave the button usable:
        # stale async downscales never overwrite a newer pick, an
        # unreadable file resets the control with a message, and
        # gallery images with an empty type still count as images.
        user = self.make_user("repick")
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, "pickSeq")
        self.assertContains(response, "looksLikeImage")
        self.assertContains(response, "Reading picture")
        self.assertContains(response, "Could not read that picture")

    def test_file_input_hides_in_place(self):
        # The native input hides inside its row at 16px type, never
        # off-canvas: mobile browsers unreliably open pickers from
        # labels pointing at off-screen inputs (first taps swallowed,
        # dead control after a cancel). Taps reach the button
        # instead; the input keeps no pointer events.
        from django.contrib.staticfiles import finders

        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        block = css.split(
            '.avatar-pick-row input[type="file"]')[1].split("}")[0]
        self.assertIn("position: absolute", block)
        self.assertNotIn("-9999px", block)
        self.assertIn("pointer-events: none", block)
        self.assertIn("font-size: 16px", block)

    def test_profile_has_theme_toggle(self):
        self.make_user("themer")
        self.client.force_login(User.objects.get(username="themer"))
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "theme-toggle")
        for theme in ("abyss", "blueprint", "ember", "forest", "clay",
                      "modern", "midnight"):
            self.assertContains(response, f'data-theme-set="{theme}"')
        self.assertContains(response, "summit-theme")

    def test_username_change(self):
        self.make_user("oldname")
        self.client.force_login(User.objects.get(username="oldname"))
        response = self.client.post(
            reverse("accounts:profile-update"), {"username": "newname"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="newname").exists())

    def test_username_taken_is_rejected(self):
        self.make_user("taken")
        me = self.make_user("me")
        self.client.force_login(me)
        response = self.client.post(
            reverse("accounts:profile-update"), {"username": "taken"},
            follow=True)
        self.assertContains(response, "That username is taken.")
        me.refresh_from_db()
        self.assertEqual(me.username, "me")

    def test_non_image_avatar_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        me = self.make_user("pic")
        self.client.force_login(me)
        bad = SimpleUploadedFile("evil.txt", b"not an image",
                                 content_type="text/plain")
        response = self.client.post(
            reverse("accounts:profile-update"),
            {"username": "pic", "avatar": bad}, follow=True)
        self.assertContains(response, "JPG, PNG or GIF")
        me.refresh_from_db()
        self.assertFalse(bool(me.avatar))

    def test_png_avatar_accepted(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        me = self.make_user("pic2")
        self.client.force_login(me)
        png = SimpleUploadedFile("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
                                 content_type="image/png")
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                response = self.client.post(
                    reverse("accounts:profile-update"),
                    {"username": "pic2", "avatar": png})
        self.assertEqual(response.status_code, 302)
        me.refresh_from_db()
        self.assertTrue(me.avatar.name.startswith("avatars/"))

    def test_saved_avatar_is_served(self):
        # The live container runs DEBUG=False, where static() adds no
        # routes: a saved picture must still come back, not 404 (the
        # profile then shows only the alt text).
        import tempfile

        from django.core.files.base import ContentFile
        from django.test import override_settings

        me = self.make_user("served")
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                me.avatar.save(
                    "me.png",
                    ContentFile(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64))
                response = self.client.get(me.avatar.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("image/", response["Content-Type"])

    def test_remove_avatar_clears_photo(self):
        # "Remove photo" deletes the stored file and clears
        # the field; a fresh upload in the same save wins instead.
        import os
        import tempfile

        from django.core.files.base import ContentFile
        from django.test import override_settings

        me = self.make_user("clearer")
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                me.avatar.save("old.png",
                               ContentFile(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64))
                old_path = me.avatar.path
                self.client.force_login(me)
                shown = self.client.get(reverse("accounts:profile"))
                self.assertContains(shown, 'id="avatar-remove"')
                self.assertContains(shown, "Remove photo")
                self.assertNotContains(shown, "Remove current photo")
                self.client.post(
                    reverse("accounts:profile-update"),
                    {"username": "clearer", "remove_avatar": "true"})
                me.refresh_from_db()
                self.assertFalse(bool(me.avatar))
                self.assertFalse(os.path.exists(old_path))

    def test_avatar_replace_deletes_orphan(self):
        # Uploading a new picture removes the previous file so stale
        # avatars never pile up in storage.
        import os
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        me = self.make_user("replacer")
        self.client.force_login(me)
        first = SimpleUploadedFile("a.png", b"\x89PNG\r\n\x1a\n" + b"\x01" * 64,
                                   content_type="image/png")
        second = SimpleUploadedFile("b.png", b"\x89PNG\r\n\x1a\n" + b"\x02" * 64,
                                    content_type="image/png")
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.client.post(
                    reverse("accounts:profile-update"),
                    {"username": "replacer", "avatar": first})
                me.refresh_from_db()
                old_path = me.avatar.path
                self.assertTrue(os.path.exists(old_path))
                self.client.post(
                    reverse("accounts:profile-update"),
                    {"username": "replacer", "avatar": second})
                me.refresh_from_db()
                self.assertTrue(me.avatar.name.startswith("avatars/"))
                self.assertNotEqual(me.avatar.path, old_path)
                self.assertFalse(os.path.exists(old_path))

    def test_avatar_urls_are_absolute(self):
        # Avatar URLs must work from every page: a relative media
        # URL only resolves on the site root and 404s everywhere
        # else (profile, leaderboard, climber card).
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        me = self.make_user("absolute")
        png = SimpleUploadedFile("a.png", b"\x89PNG\r\n\x1a\n" + b"\x01" * 64,
                                 content_type="image/png")
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                self.client.force_login(me)
                self.client.post(
                    reverse("accounts:profile-update"),
                    {"username": "absolute", "avatar": png})
                me.refresh_from_db()
                self.assertTrue(me.avatar.url.startswith("/media/"))
                shown = self.client.get(reverse("accounts:profile"))
                self.assertContains(shown, me.avatar.url)

    def test_remove_missing_avatar_file_does_not_500(self):
        # The database row can outlive the file (wiped volume):
        # removing or replacing a ghost avatar must still save.
        me = self.make_user("ghost")
        me.avatar = "avatars/gone.png"
        me.save()
        self.client.force_login(me)
        response = self.client.post(
            reverse("accounts:profile-update"),
            {"username": "ghost", "remove_avatar": "true"})
        self.assertEqual(response.status_code, 302)
        me.refresh_from_db()
        self.assertFalse(bool(me.avatar))

    def test_password_change_needs_current_password(self):
        self.make_user("pw", password="old-pass-123")
        self.client.force_login(User.objects.get(username="pw"))
        url = reverse("accounts:password-change")
        bad = self.client.post(url, {
            "old_password": "wrong",
            "new_password1": "brand-new-pass-456",
            "new_password2": "brand-new-pass-456",
        }, follow=True)
        self.assertContains(bad, "old password was entered incorrectly")
        good = self.client.post(url, {
            "old_password": "old-pass-123",
            "new_password1": "brand-new-pass-456",
            "new_password2": "brand-new-pass-456",
        }, follow=True)
        self.assertContains(good, "Password changed.")
        self.client.logout()
        self.assertTrue(self.client.login(username="pw",
                                          password="brand-new-pass-456"))

    def test_delete_needs_password_and_confirmation(self):
        me = self.make_user("doomed", password="bye-pass-123")
        self.client.force_login(me)
        url = reverse("accounts:account-delete")
        wrong = self.client.post(url, {
            "password": "nope", "confirm": True,
        }, follow=True)
        self.assertContains(wrong, "Wrong password.")
        self.assertTrue(User.objects.filter(username="doomed").exists())
        good = self.client.post(url, {
            "password": "bye-pass-123", "confirm": True,
        }, follow=True)
        self.assertContains(good, "deleted")
        self.assertFalse(User.objects.filter(username="doomed").exists())
        self.assertNotIn("_auth_user_id", self.client.session)


class PasswordPeekTests(TestCase):
    def test_every_password_field_gets_an_eye(self):
        # The peek enhancer ships in the base template, so login,
        # signup and the profile's password forms all grow a toggle:
        # fields stay type=password, the script and its styles load.
        from django.contrib.staticfiles import finders

        User.objects.create_user(username="peeker",
                                 email="peeker@example.com",
                                 password="test-pass-123")
        self.client.force_login(User.objects.get(username="peeker"))
        for url in (reverse("login"), reverse("signup"),
                    reverse("accounts:profile")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'type="password"')
                self.assertContains(response, "pw-peek")
                self.assertContains(response, "Show password")
        css_path = finders.find("climbs/style.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn(".pw-wrap", css)
        self.assertIn(".pw-peek", css)
