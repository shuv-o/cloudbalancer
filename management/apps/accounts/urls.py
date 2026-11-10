from django.urls import path

from apps.accounts.apis import (
    CsrfApi,
    LoginApi,
    LogoutApi,
    MeApi,
    PasskeyLoginOptionsApi,
    PasskeyLoginVerifyApi,
    PasskeyRegisterOptionsApi,
    PasskeyRegisterVerifyApi,
)

urlpatterns = [
    path("csrf/", CsrfApi.as_view(), name="auth-csrf"),
    path("login/", LoginApi.as_view(), name="auth-login"),
    path("logout/", LogoutApi.as_view(), name="auth-logout"),
    path("me/", MeApi.as_view(), name="auth-me"),
    path("passkey/register/options/", PasskeyRegisterOptionsApi.as_view(),
         name="passkey-register-options"),
    path("passkey/register/verify/", PasskeyRegisterVerifyApi.as_view(),
         name="passkey-register-verify"),
    path("passkey/login/options/", PasskeyLoginOptionsApi.as_view(),
         name="passkey-login-options"),
    path("passkey/login/verify/", PasskeyLoginVerifyApi.as_view(),
         name="passkey-login-verify"),
]
