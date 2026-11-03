from django.urls import path
from . import views

app_name = "climbs"

urlpatterns = [
    # main pages
    path("", views.map_view, name="map"),
    path("manifest.webmanifest", views.manifest_view, name="manifest"),
    path("sw.js", views.service_worker_view, name="sw"),
    path("leaderboard/", views.leaderboard_view, name="leaderboard"),
    path("forum/", views.forum_view, name="forum"),
    path("forum/<int:post_id>/", views.forum_post_view, name="forum-post"),
    # old news links keep working
    path("news/", views.news_redirect, name="news"),
    # forum api
    path("forum/create/", views.forum_create_api, name="forum-create"),
    path("forum/<int:post_id>/update/", views.forum_update_api,
         name="forum-update"),
    path("forum/<int:post_id>/delete/", views.forum_delete_api,
         name="forum-delete"),
    path("forum/<int:post_id>/pin/", views.forum_pin_api, name="forum-pin"),
    path("forum/<int:post_id>/like/", views.forum_post_like_api,
         name="forum-like"),
    path("forum/<int:post_id>/comments/", views.forum_comment_create_api,
         name="forum-comment"),
    path("forum/comments/<int:comment_id>/like/",
         views.forum_comment_like_api, name="forum-comment-like"),
    path("forum/comments/<int:comment_id>/delete/",
         views.forum_comment_delete_api, name="forum-comment-delete"),
    path("climbers/<str:username>/", views.climber_api, name="climber-detail"),
    path("leaderboard/sets/<int:set_id>/users/<str:username>/sends/",
         views.set_user_sends_api, name="set-user-sends"),
    # climb api
    path("<int:climb_id>/", views.climb_detail_api, name="climb-detail"),
    path("<int:climb_id>/ascent/", views.ascent_api, name="climb-ascent"),
    path("<int:climb_id>/rate/", views.rating_api, name="climb-rate"),
    path("<int:climb_id>/grade/", views.grade_vote_api, name="climb-grade"),
    path("<int:climb_id>/comments/", views.comment_create_api, name="climb-comment"),
    path("<int:climb_id>/comments/<int:comment_id>/delete/",
         views.comment_delete_api, name="climb-comment-delete"),
    # staff admin tools
    path("admin-tools/climbs/", views.climb_admin_view, name="climb-admin"),
    path("admin-tools/boards/", views.board_admin_view, name="board-admin"),
    path("admin-tools/climbs/create/", views.climb_create_api, name="climb-create"),
    path("admin-tools/climbs/<int:climb_id>/update/", views.climb_update_api, name="climb-update"),
    path("admin-tools/climbs/<int:climb_id>/delete/", views.climb_delete_api, name="climb-delete"),
    path("admin-tools/sets/start/", views.climb_set_start_api, name="climb-set-start"),
    path("admin-tools/sets/<int:set_id>/activate/", views.climb_set_activate_api, name="climb-set-activate"),
    path("admin-tools/sets/<int:set_id>/update/", views.climb_set_update_api, name="climb-set-update"),
    path("admin-tools/sets/<int:set_id>/delete/", views.climb_set_delete_api, name="climb-set-delete"),
    path("admin-tools/sets/<int:set_id>/remove-user/", views.set_remove_user_api, name="climb-set-remove-user"),
    path("admin-tools/boards/recalculate/", views.recalc_points_api, name="board-recalculate"),
    path("admin-tools/flags/<int:flag_id>/review/", views.flag_review_api, name="flag-review"),
    path("admin-tools/flags/review-many/", views.flags_review_many_api, name="flags-review-many"),
    path("admin-tools/ascents/<int:ascent_id>/void/", views.ascent_void_api, name="ascent-void"),
    path("admin-tools/users/<str:username>/hide/", views.user_hide_api, name="user-hide"),
    path("admin-tools/users/<str:username>/log/", views.user_log_view, name="user-log"),
]
