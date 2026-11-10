"""
The two WebAuthn ceremonies, and the one decision that governs both.

A passkey is bound to a *relying party ID* — a domain name — chosen when the
credential is created and checked on every use. That binding is what makes the
sign-in unphishable, and it is also what makes the configuration unforgiving:

  * An IP address cannot be a relying party ID. The specification requires a
    domain, so a panel reached at http://203.0.113.10:8081 cannot use passkeys
    at all, however it is configured.
  * A credential enrolled under one RP ID will not work under another. There is
    no migration. Moving the panel to a different hostname means everyone
    enrols again.

So the RP ID is taken from the published panel domain and from nowhere else —
never from the request's Host header, which an attacker chooses. If the panel
is not published, enrolment is refused rather than silently bound to something
that will have to be thrown away.
"""
import logging

from django.utils import timezone
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from apps.security.models import PanelAccessPolicy, Passkey

logger = logging.getLogger(__name__)

# Where the challenge lives between the two halves of a ceremony. The session,
# not the database: it is single-use, short-lived, and belongs to one browser.
CHALLENGE_KEY = "webauthn_challenge"

RP_NAME = "CloudBalancer"


class PasskeyError(Exception):
    """Something about this ceremony cannot be completed, with a reason to show."""


class PasskeysUnavailable(PasskeyError):
    """The panel has no hostname a passkey could be bound to."""


class RelyingParty:
    __slots__ = ("id", "name", "origin")

    def __init__(self, rp_id: str):
        self.id = rp_id
        self.name = RP_NAME
        # Always https. A passkey ceremony in a browser requires a secure
        # context, so there is no http origin that could legitimately appear.
        self.origin = f"https://{rp_id}"


def relying_party(*, policy: PanelAccessPolicy | None = None) -> RelyingParty:
    """
    The domain passkeys are bound to.

    Deliberately not derived from the request. Host is attacker-controlled, and
    an RP ID taken from it would let a request for another name mint a
    credential this panel would later accept.
    """
    policy = policy or PanelAccessPolicy.load()

    if not (policy.is_published and policy.panel_domain):
        raise PasskeysUnavailable(
            "Passkeys need the panel to have a hostname of its own. Publish it "
            "under Security → Where the panel can be reached first: a passkey "
            "is bound to a domain name, and an IP address cannot be one."
        )

    return RelyingParty(policy.panel_domain)


def passkeys_available() -> bool:
    """Whether enrolment can be offered at all, for the panel to ask before showing it."""
    try:
        relying_party()
    except PasskeysUnavailable:
        return False
    return True


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

def registration_options(*, user, session) -> str:
    """Start enrolment. Returns the options as JSON, ready for the browser."""
    rp = relying_party()

    options = generate_registration_options(
        rp_id=rp.id,
        rp_name=rp.name,
        # The user handle. An opaque id, not the username: it is stored on the
        # authenticator and should not leak who the account belongs to.
        user_id=str(user.pk).encode(),
        user_name=user.get_username(),
        user_display_name=user.get_full_name() or user.get_username(),
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Discoverable, so signing in needs no username typed first.
            resident_key=ResidentKeyRequirement.REQUIRED,
            # Required, so the passkey is two factors on its own: the device,
            # plus the biometric or PIN that unlocks it. This is what lets a
            # passkey stand in for a password and a code together.
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        # Stops a second credential being created on an authenticator that
        # already holds one for this account.
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
            for cid in Passkey.objects.filter(user=user).values_list(
                "credential_id", flat=True
            )
        ],
    )

    session[CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def registration_verify(*, user, session, credential: dict, label: str) -> Passkey:
    """Finish enrolment and store the public key."""
    rp = relying_party()
    challenge = session.pop(CHALLENGE_KEY, None)
    if not challenge:
        raise PasskeyError(
            "This enrolment has expired. Start again from the Security page."
        )

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            require_user_verification=True,
        )
    except Exception as exc:
        logger.warning("Passkey enrolment rejected for %s: %s", user, exc)
        raise PasskeyError(f"That passkey could not be registered: {exc}") from exc

    return Passkey.objects.create(
        user=user,
        label=(label or "").strip()[:64] or "Passkey",
        credential_id=bytes_to_base64url(verified.credential_id),
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        transports=credential.get("transports") or [],
        is_backed_up=bool(getattr(verified, "credential_backed_up", False)),
    )


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

def authentication_options(*, session) -> str:
    """
    Start a sign-in.

    No credential list is sent. The credentials are discoverable, so the
    browser offers whichever ones it holds for this domain — which is why the
    sign-in needs no username, and why this response tells an unauthenticated
    caller nothing about which accounts exist.
    """
    rp = relying_party()

    options = generate_authentication_options(
        rp_id=rp.id,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    session[CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def authentication_verify(*, session, credential: dict) -> Passkey:
    """Check an assertion and return the passkey that produced it."""
    rp = relying_party()
    challenge = session.pop(CHALLENGE_KEY, None)
    if not challenge:
        raise PasskeyError("This sign-in has expired. Try again.")

    credential_id = credential.get("id") or credential.get("rawId") or ""
    passkey = (
        Passkey.objects
        .select_related("user")
        .filter(credential_id=credential_id)
        .first()
    )
    if passkey is None:
        raise PasskeyError("That passkey is not registered on this panel.")

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            credential_public_key=base64url_to_bytes(passkey.public_key),
            # A counter that does not advance means two authenticators are
            # answering for one credential. py_webauthn refuses it, and that
            # refusal is the only clone detection WebAuthn offers.
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        logger.warning("Passkey assertion rejected for %s: %s", passkey.user, exc)
        raise PasskeyError("That passkey was not accepted.") from exc

    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = timezone.now()
    passkey.save(update_fields=["sign_count", "last_used_at"])
    return passkey
