"""Template context processors."""

from datetime import timedelta

from django.utils.timezone import now

from summit_map_project import VERSION

INSTALL_PROMPT_COOLDOWN = timedelta(days=14)


def version(request):
    """Expose the release version in one place: Summit vX in footers."""
    return {"summit_version": VERSION}


def install_prompt(request):
    """Whether the PWA install nudge may appear on this page load:
    authenticated users only, never when opted out, at most once per
    14 days. The client adds its own checks (standalone mode, one
    appearance per session, admin/map exclusions)."""
    show = False
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        dismissed = getattr(user, "install_prompt_dismissed", False)
        last_shown = getattr(user, "install_prompt_last_shown", None)
        if not dismissed and (
                last_shown is None or now() - last_shown >= INSTALL_PROMPT_COOLDOWN):
            show = True
    return {"install_nudge_show": show}
