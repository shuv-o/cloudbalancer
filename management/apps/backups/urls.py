from django.urls import path

from apps.backups.apis import BackupApi, BackupDetailApi, BackupPruneApi

urlpatterns = [
    path("", BackupApi.as_view(), name="backups"),
    path("prune/", BackupPruneApi.as_view(), name="backups-prune"),
    path("<int:backup_id>/", BackupDetailApi.as_view(), name="backup-detail"),
    path("<int:backup_id>/verify/", BackupDetailApi.as_view(), name="backup-verify"),
]
