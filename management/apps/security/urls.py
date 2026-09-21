from django.urls import path

from apps.security.apis import (
    AuditApi,
    LoginAttemptApi,
    OperatorApi,
    PolicyApi,
    SecuritySummaryApi,
    TotpConfirmApi,
    TotpDisableApi,
    TotpResetApi,
    TotpSetupApi,
    TotpStatusApi,
)

urlpatterns = [
    path("policy/", PolicyApi.as_view(), name="security-policy"),
    path("summary/", SecuritySummaryApi.as_view(), name="security-summary"),
    path("audit/", AuditApi.as_view(), name="security-audit"),
    path("attempts/", LoginAttemptApi.as_view(), name="security-attempts"),
    path("operators/", OperatorApi.as_view(), name="security-operators"),
    path("totp/status/", TotpStatusApi.as_view(), name="totp-status"),
    path("totp/setup/", TotpSetupApi.as_view(), name="totp-setup"),
    path("totp/confirm/", TotpConfirmApi.as_view(), name="totp-confirm"),
    path("totp/disable/", TotpDisableApi.as_view(), name="totp-disable"),
    path("totp/reset/", TotpResetApi.as_view(), name="totp-reset"),
]
