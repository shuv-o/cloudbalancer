from django.urls import path

from apps.security.apis import (
    AuditApi,
    BlockedAddressApi,
    BlockedAddressDetailApi,
    LoginAttemptApi,
    OperatorApi,
    PasskeyApi,
    PasskeyDetailApi,
    PolicyApi,
    ProtectionApi,
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
    path("protection/", ProtectionApi.as_view(), name="security-protection"),
    path("blocked/", BlockedAddressApi.as_view(), name="security-blocked"),
    path("blocked/<int:blocked_id>/", BlockedAddressDetailApi.as_view(), name="security-blocked-detail"),
    path("audit/", AuditApi.as_view(), name="security-audit"),
    path("attempts/", LoginAttemptApi.as_view(), name="security-attempts"),
    path("operators/", OperatorApi.as_view(), name="security-operators"),
    path("passkeys/", PasskeyApi.as_view(), name="security-passkeys"),
    path("passkeys/<int:passkey_id>/", PasskeyDetailApi.as_view(),
         name="security-passkey-detail"),
    path("totp/status/", TotpStatusApi.as_view(), name="totp-status"),
    path("totp/setup/", TotpSetupApi.as_view(), name="totp-setup"),
    path("totp/confirm/", TotpConfirmApi.as_view(), name="totp-confirm"),
    path("totp/disable/", TotpDisableApi.as_view(), name="totp-disable"),
    path("totp/reset/", TotpResetApi.as_view(), name="totp-reset"),
]
