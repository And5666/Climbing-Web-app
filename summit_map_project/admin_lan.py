from django.http import Http404


def _private(ip: str) -> bool:
    """True for loopback, RFC1918 and Tailscale addresses."""
    if not ip:
        return True
    if ip in ("127.0.0.1", "::1"):
        return True
    if ip.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                      "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
                      "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")):
        return True
    if ip.startswith("100."):
        try:
            second = int(ip.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 64 <= second <= 127
    return False


class AdminLANOnlyMiddleware:
    """Keep /admin/ off the public internet; allow LAN and Tailscale."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/admin/"):
            client = request.META.get("HTTP_CF_CONNECTING_IP", "")
            if client and not _private(client):
                raise Http404("Not found")
        return self.get_response(request)
