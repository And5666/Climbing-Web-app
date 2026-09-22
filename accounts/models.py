from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    email = models.EmailField(unique=True)
    # Exclude from public leaderboards while a review is open.
    # Staff-only flag; the user still sees their own totals.
    leaderboard_hidden = models.BooleanField(default=False)
    # Plain FileField (not ImageField): no server-side image work is
    # needed, so type/size are validated in the form by magic bytes
    # and the browser downscales uploads to 256px before sending.
    avatar = models.FileField(upload_to="avatars/", blank=True, null=True)