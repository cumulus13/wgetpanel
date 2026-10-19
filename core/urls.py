from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.settings_page, name="settings"),
    path("rules/", views.rules_page, name="rules"),
    path("healthz", views.healthz, name="healthz"),
    path("api/snapshot/", views.snapshot, name="api-snapshot"),
    path("api/add/", views.add, name="api-add"),
    path("api/action/", views.action, name="api-action"),
    path("api/download/<int:pk>/", views.detail, name="api-detail"),
    path("api/download/<int:pk>/file/", views.file_download, name="api-file"),
    path("api/settings/", views.settings_api, name="api-settings"),
    path("api/rules/", views.rules_api, name="api-rules"),
]
