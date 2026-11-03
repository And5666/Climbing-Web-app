"""stuff that gets added to every template"""

from datetime import timedelta

from django.utils.timezone import now

from summit_map_project import VERSION

INSTALL_PROMPT_COOLDOWN = timedelta(days=1)


def version(request):
    """puts the version number in the footer"""
    return {"summit_version": VERSION}


def install_prompt(request):
    """decides if the install banner can show. only for logged in
    users, not if they said never, and only once a day. the browser
    side does some checks too"""
    show = False
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        dismissed = getattr(user, "install_prompt_dismissed", False)
        last_shown = getattr(user, "install_prompt_last_shown", None)
        if not dismissed and (
                last_shown is None or now() - last_shown >= INSTALL_PROMPT_COOLDOWN):
            show = True
    return {"install_nudge_show": show}
