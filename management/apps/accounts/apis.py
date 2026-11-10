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
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token, rotate_token
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.security.models import LoginAttempt, PanelAccessPolicy, TotpDevice
from apps.security.passkeys import (
    PasskeyError,
    passkeys_available,
)
from apps.security.passkeys import (
    authentication_options as passkey_authentication_options,
)
from apps.security.passkeys import (
    authentication_verify as passkey_authentication_verify,
)
from apps.security.passkeys import (
    registration_options as passkey_registration_options,
)
from apps.security.passkeys import (
    registration_verify as passkey_registration_verify,
)
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
        # Whether passkeys can be offered at all. Says only that the panel has
        # a hostname of its own, which whoever is looking can see in the
        # address bar -- so it is safe to answer before anyone has signed in,
        # and it keeps the form from showing a button that cannot work.
        available = passkeys_available()

        if not request.user.is_authenticated:
            return Response({"authenticated": False, "passkeys_available": available})

        return Response({
            "authenticated": True,
            **_user_payload(request.user),
            "totp_setup_required": user_requires_totp_setup(user=request.user),
            "passkeys_available": available,
        })


# ---------------------------------------------------------------------------
# Passkeys
#
# Two ceremonies, two halves each. Enrolment happens inside an existing
# session, so it is ordinary authenticated work. Sign-in does not, so the
# second half below repeats every gate the password path applies — address,
# lockout, active account — and records the outcome the same way. A second
# way in must not be a weaker way in.
# ---------------------------------------------------------------------------

class PasskeyRegisterOptionsApi(APIView):
    """
    POST /api/v1/auth/passkey/register/options/  — begin enrolling a passkey
    """

    def post(self, request):
        try:
            options = passkey_registration_options(
                user=request.user, session=request.session
            )
        except PasskeyError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(json.loads(options))


class PasskeyRegisterVerifyApi(APIView):
    """
    POST /api/v1/auth/passkey/register/verify/  — finish enrolling a passkey
    """

    def post(self, request):
        credential = request.data.get("credential")
        if not isinstance(credential, dict):
            return Response(
                {"error": "No passkey was sent."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            passkey = passkey_registration_verify(
                user=request.user,
                session=request.session,
                credential=credential,
                label=request.data.get("label", ""),
            )
        except PasskeyError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        logger.info("%s enrolled passkey '%s'", request.user, passkey.label)
        return Response(
            {
                "id": passkey.pk,
                "label": passkey.label,
                "is_backed_up": passkey.is_backed_up,
                "created_at": passkey.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class PasskeyLoginOptionsApi(APIView):
    """
    POST /api/v1/auth/passkey/login/options/  — begin signing in with a passkey

    Returns the same shape whatever accounts exist. The credentials are
    discoverable, so no list is sent and nothing here reveals who is enrolled.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        policy = PanelAccessPolicy.load()
        if not ip_is_allowed(policy=policy, address=client_ip(request)):
            return Response(
                {"error": "This address is not allowed to reach the control panel."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            options = passkey_authentication_options(session=request.session)
        except PasskeyError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(json.loads(options))


class PasskeyLoginVerifyApi(APIView):
    """
    POST /api/v1/auth/passkey/login/verify/  — finish signing in with a passkey

    A passkey that required user verification is already two factors: the
    device, and the biometric or PIN that unlocked it. So this path does not
    then ask for an authenticator code — doing so would add a phishable step
    to a sign-in that had none.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        credential = request.data.get("credential")
        if not isinstance(credential, dict):
            return Response(
                {"error": "No passkey was sent."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        policy = PanelAccessPolicy.load()
        address = client_ip(request)
        agent = request.META.get("HTTP_USER_AGENT", "")

        if not ip_is_allowed(policy=policy, address=address):
            attempt_record(
                username="(passkey)", ip_address=address, user_agent=agent,
                outcome=LoginAttempt.Outcome.BLOCKED_IP,
            )
            return Response(
                {"error": "This address is not allowed to reach the control panel."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            passkey = passkey_authentication_verify(
                session=request.session, credential=credential
            )
        except PasskeyError as exc:
            attempt_record(
                username="(passkey)", ip_address=address, user_agent=agent,
                outcome=LoginAttempt.Outcome.BAD_PASSKEY,
            )
            logger.warning("Passkey sign-in refused from %s: %s", address, exc)
            return Response({"error": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)

        user = passkey.user
        username = user.get_username()

        # The lockout is keyed on the username, and the username is only known
        # once the assertion has been checked -- so it is enforced here rather
        # than before. A locked account cannot be signed into by any route.
        lock = lock_check(username=username)
        if lock is not None:
            attempt_record(
                username=username, ip_address=address, user_agent=agent,
                outcome=LoginAttempt.Outcome.LOCKED,
            )
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

        if not user.is_active:
            attempt_record(
                username=username, ip_address=address, user_agent=agent,
                outcome=LoginAttempt.Outcome.INACTIVE,
            )
            return Response(
                {"error": "That account has been disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )

        login(request, user)
        request.session.cycle_key()
        rotate_token(request)
        request.session["started_at"] = timezone.now().timestamp()
        request.session["last_seen_at"] = timezone.now().timestamp()

        lock_clear(username=username)
        attempt_record(
            username=username, ip_address=address, user_agent=agent,
            outcome=LoginAttempt.Outcome.PASSKEY,
        )
        logger.info("%s signed in with passkey '%s' from %s",
                    username, passkey.label, address)

        return Response({
            **_user_payload(user),
            # A passkey stands in for the second factor, so enrolling an
            # authenticator app is no longer outstanding work.
            "totp_setup_required": False,
        })
