from django.urls import path

from . import api_v1, views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.settings_page, name="settings"),
    path("rules/", views.rules_page, name="rules"),
    # path("healthz", views.healthz, name="healthz"),
    path("healthz/", views.healthz, name="healthz"),
    path("api/snapshot/", views.snapshot, name="api-snapshot"),
    path("api/add/", views.add, name="api-add"),
    path("api/action/", views.action, name="api-action"),
    path("api/download/<int:pk>/", views.detail, name="api-detail"),
    path("api/download/<int:pk>/file/", views.file_download, name="api-file"),
    path("api/settings/", views.settings_api, name="api-settings"),
    path("api/rules/", views.rules_api, name="api-rules"),
    # mobile / third party API (bearer tokens, see api_v1.py)
    path("api/v1/openapi.json", api_v1.openapi, name="v1-openapi"),
    path("api/v1/auth/token", api_v1.token, name="v1-token"),
    path("api/v1/auth/me", api_v1.me, name="v1-me"),
    path("api/v1/auth/tokens", api_v1.token_list, name="v1-tokens"),
    path("api/v1/auth/tokens/<int:pk>", api_v1.token_detail, name="v1-token-detail"),
    path("api/v1/summary", api_v1.summary, name="v1-summary"),
    path("api/v1/downloads", api_v1.downloads, name="v1-downloads"),
    path("api/v1/downloads/actions", api_v1.actions, name="v1-actions"),
    path("api/v1/downloads/<int:pk>", api_v1.download_detail, name="v1-detail"),
    path("api/v1/downloads/<int:pk>/file", api_v1.file, name="v1-file"),
    path("api/v1/events", api_v1.event_endpoint, name="v1-events"),
]
