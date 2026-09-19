from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("profile/", views.profile_view, name="profile"),
    path("stats/", views.stats_view, name="stats"),
    path("profile/update/", views.profile_update_view, name="profile-update"),
    path("profile/password/", views.password_change_view, name="password-change"),
    path("profile/delete/", views.account_delete_view, name="account-delete"),
]
