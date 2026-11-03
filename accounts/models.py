from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    email = models.EmailField(unique=True)
    # hides the user from public boards while staff look into their logs.
    # they still see there own totals
    leaderboard_hidden = models.BooleanField(default=False)
    # plain filefield not imagefield. the form checks the magic bytes and
    # the browser shrinks the image before upload
    avatar = models.FileField(upload_to="avatars/", blank=True, null=True)
    # install banner state. never is permanent, maybe later just stops it
    # showing again until the next day
    install_prompt_dismissed = models.BooleanField(default=False)
    install_prompt_last_shown = models.DateTimeField(null=True, blank=True)