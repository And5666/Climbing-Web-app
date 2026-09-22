"""Template context processors."""

from summit_map_project import VERSION


def version(request):
    """Expose the release version in one place: Summit vX in footers."""
    return {"summit_version": VERSION}
