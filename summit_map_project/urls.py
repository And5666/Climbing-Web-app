from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.views.static import serve
from climbs.views import SignupView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("climbs.urls")),
    path("accounts/", include("accounts.urls")),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", SignupView.as_view(), name="signup"),
]

# Avatars are served by Django itself: there is no separate web server
# in front, and at this scale the tiny images cost nothing. Uploads
# are magic-byte validated images, so nothing executable can land here.
# This must not use static(): that helper adds nothing when DEBUG is
# off, which 404s every avatar on the live container. MEDIA_ROOT is
# read per request so tests can point it at a temp dir.
def serve_media(request, path):
    return serve(request, path, document_root=settings.MEDIA_ROOT)


urlpatterns += [path("media/<path:path>", serve_media)]