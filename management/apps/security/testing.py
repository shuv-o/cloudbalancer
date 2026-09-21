"""
Test helpers for authenticated requests.

The panel requires a second factor by default, and the middleware enforces it
on every request rather than only at sign-in. A test that merely calls
force_login therefore gets a 403 telling it to go and enrol — which is correct
behaviour, and makes this helper the honest way to have an operator who is
actually allowed to do things.
"""
from django.contrib.auth.models import User
from django.utils import timezone

import pyotp

from apps.security.models import TotpDevice


def make_operator(
    *,
    username: str = "operator",
    password: str = "correct-horse-battery",
    superuser: bool = True,
    with_second_factor: bool = True,
) -> User:
    """Create an operator, enrolled by default."""
    factory = User.objects.create_superuser if superuser else User.objects.create_user
    user = factory(username=username, email=f"{username}@example.com", password=password)

    if with_second_factor:
        TotpDevice.objects.create(
            user=user,
            secret=pyotp.random_base32(),
            confirmed_at=timezone.now(),
            recovery_codes=[],
        )

    return user


def current_code(user: User) -> str:
    """The authenticator code this user's app would be showing right now."""
    return pyotp.TOTP(user.totp_device.secret).now()


def sign_in(client, *, user: User) -> User:
    """Put an enrolled operator into the session, as force_login would."""
    client.force_login(user)
    return user
