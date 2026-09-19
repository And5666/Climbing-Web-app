from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
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
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)