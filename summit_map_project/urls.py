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

# avatars are served by django because there is no web server in front.
# dont swap this for static(), that 404s when debug is off. media root
# is read per request so the tests can point it at a temp dir
def serve_media(request, path):
    return serve(request, path, document_root=settings.MEDIA_ROOT)


urlpatterns += [path("media/<path:path>", serve_media)]