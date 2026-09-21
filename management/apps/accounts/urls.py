from django.urls import path

from apps.accounts.apis import CsrfApi, LoginApi, LogoutApi, MeApi

urlpatterns = [
    path("csrf/", CsrfApi.as_view(), name="auth-csrf"),
    path("login/", LoginApi.as_view(), name="auth-login"),
    path("logout/", LogoutApi.as_view(), name="auth-logout"),
    path("me/", MeApi.as_view(), name="auth-me"),
]
