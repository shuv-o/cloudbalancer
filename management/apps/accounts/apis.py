"""
Session authentication for the control panel.

The panel is a single-page app served from the same origin as the API, so it
uses Django's session cookie rather than a bearer token: nothing to store in
JavaScript, and signing out actually ends the session server-side.

Sign-in is deliberately more involved than a password check. Reaching this
panel is equivalent to controlling every hostname the gateway serves, so the
endpoint below also enforces an address allowlist, an attempt limit, an account
lockout, and a second factor — and records the outcome either way.
"""
import logging

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token, rotate_token
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.security.models import LoginAttempt, PanelAccessPolicy, TotpDevice
from apps.security.services import (
    attempt_record,
    client_ip,
    ip_is_allowed,
    lock_check,
    lock_clear,
    lock_record_failure,
    totp_verify,
    user_requires_totp_setup,
)

logger = logging.getLogger(__name__)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=254)
    password = serializers.CharField(style={"input_type": "password"}, trim_whitespace=False)
    otp = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")


def _user_payload(user) -> dict:
    return {
        "id": user.pk,
        "username": user.get_username(),
        "email": user.email,
        "full_name": user.get_full_name(),
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
    }


class CsrfApi(APIView):
    """
    GET /api/v1/auth/csrf/  — set the CSRF cookie before the first write
    """
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrf_token": get_token(request)})


class LoginApi(APIView):
    """
    POST /api/v1/auth/login/  — start a session

    Send username and password. If the account has a second factor, the first
    response says so and the panel asks for a code; send all three together on
    the retry.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]
        otp = (serializer.validated_data.get("otp") or "").strip()

        policy = PanelAccessPolicy.load()
        address = client_ip(request)
        agent = request.META.get("HTTP_USER_AGENT", "")

        def record(outcome):
            attempt_record(
                username=username, ip_address=address, user_agent=agent, outcome=outcome
            )

        # 1. Address. Nginx rejects these first; this catches a front end that
        #    was bypassed or rendered without the allowlist.
        if not ip_is_allowed(policy=policy, address=address):
            record(LoginAttempt.Outcome.BLOCKED_IP)
            logger.warning("Sign-in refused for %s from %s: address not allowed",
                           username, address)
            return Response(
                {"error": "This address is not allowed to reach the control panel."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # 2. Lockout, checked before the password so a locked account cannot be
        #    used as an oracle for whether a guess was correct.
        lock = lock_check(username=username)
        if lock is not None:
            record(LoginAttempt.Outcome.LOCKED)
            return Response(
                {
                    "error": (
                        f"Too many failed attempts. Try again in "
                        f"{lock.minutes_remaining} minutes."
                    ),
                    "locked": True,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # 3. Password.
        user = authenticate(request, username=username, password=password)
        if user is None:
            lock_record_failure(username=username, ip_address=address)
            record(LoginAttempt.Outcome.BAD_PASSWORD)
            return Response(
                {"error": "That username and password do not match an account."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            record(LoginAttempt.Outcome.INACTIVE)
            return Response(
                {"error": "That account has been disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # 4. Second factor.
        device = TotpDevice.objects.filter(user=user).first()
        has_second_factor = bool(device and device.is_confirmed)

        if has_second_factor:
            if not otp:
                # Not a failure: the password was right and the panel now needs
                # to collect a code. Nothing is recorded as an attempt yet.
                return Response(
                    {
                        "totp_required": True,
                        "message": "Enter the code from your authenticator app.",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            if not totp_verify(user=user, code=otp):
                lock_record_failure(username=username, ip_address=address)
                record(LoginAttempt.Outcome.BAD_TOTP)
                return Response(
                    {
                        "error": "That code is not correct or has already been used.",
                        "totp_required": True,
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )

        # 5. In. Rotating the session key and CSRF token on sign-in is what
        #    stops a session fixed before authentication from being usable
        #    after it.
        login(request, user)
        request.session.cycle_key()
        rotate_token(request)
        request.session["started_at"] = timezone.now().timestamp()
        request.session["last_seen_at"] = timezone.now().timestamp()

        lock_clear(username=username)
        record(LoginAttempt.Outcome.SUCCESS)
        logger.info("%s signed in from %s", username, address)

        return Response({
            **_user_payload(user),
            "totp_setup_required": user_requires_totp_setup(user=user),
        })


class LogoutApi(APIView):
    """
    POST /api/v1/auth/logout/  — end the session
    """

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeApi(APIView):
    """
    GET /api/v1/auth/me/  — who is signed in, and what they still need to do
    """
    permission_classes = [AllowAny]

    def get(self, request):
        if not request.user.is_authenticated:
            return Response({"authenticated": False})

        return Response({
            "authenticated": True,
            **_user_payload(request.user),
            "totp_setup_required": user_requires_totp_setup(user=request.user),
        })
