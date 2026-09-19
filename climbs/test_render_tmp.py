"""Throwaway batch render check (deleted after run)."""

import subprocess

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse


class BatchRenderTest(TestCase):
    def test_render(self):
        import weasyprint

        call_command("seed_demo", reset=True, verbosity=0)
        june = get_user_model().objects.get(username="june")
        june.is_staff = True
        june.save()
        self.client.force_login(june)
        with open(finders.find("climbs/style.css")) as f:
            css = f.read().replace(
                "/static/",
                "file:///home/andre/Documents/Projects/summit_map/climbs/static/")

        def pdf(url, dest, extra=""):
            html = self.client.get(url).content.decode().replace(
                "/static/",
                "file:///home/andre/Documents/Projects/summit_map/climbs/static/")
            html = html.replace(
                "</head>",
                "<style>@page{size:400px 900px;}" + css + extra + "</style></head>")
            pdf_path = dest.replace(".png", ".pdf")
            weasyprint.HTML(string=html).write_pdf(pdf_path)
            subprocess.run(
                ["pdftoppm", "-png", "-r", "60", "-singlefile",
                 pdf_path, dest.replace(".png", "")],
                check=True)
            print("wrote", dest)

        pdf(reverse("climbs:map"), "/tmp/b-phone-map.png")
        pdf(reverse("accounts:profile"), "/tmp/b-phone-profile.png")
        pdf(reverse("climbs:leaderboard"), "/tmp/b-phone-board.png")
