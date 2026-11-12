from django.urls import path

from apps.alerts.apis import (
    AlertApi,
    AlertDetailApi,
    AlertmanagerWebhookApi,
    ChannelApi,
    ChannelDetailApi,
    ChannelTestApi,
)

urlpatterns = [
    path("", AlertApi.as_view(), name="alerts"),
    path("prometheus/", AlertmanagerWebhookApi.as_view(), name="alerts-prometheus"),
    path("channels/", ChannelApi.as_view(), name="alert-channels"),
    path("channels/<int:channel_id>/", ChannelDetailApi.as_view(),
         name="alert-channel-detail"),
    path("channels/<int:channel_id>/test/", ChannelTestApi.as_view(),
         name="alert-channel-test"),
    path("<int:alert_id>/acknowledge/", AlertDetailApi.as_view(),
         name="alert-acknowledge"),
]
