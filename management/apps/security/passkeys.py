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
import json
import logging
import time

from django.db import IntegrityError, transaction
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

# Where challenges live between the two halves of a ceremony. The session, not
# the database: each is single-use, short-lived, and belongs to one browser.
#
# One store per ceremony, and several challenges outstanding in each. A single
# slot looked simpler and was not: a second tab, a page that refetched its
# options, or the sign-in page open beside the account page each overwrote the
# challenge the other was holding, and the failure surfaced on whichever
# ceremony finished second -- as "expired", on a passkey that was fine.
REGISTER_CHALLENGES = "webauthn_register_challenges"
LOGIN_CHALLENGES = "webauthn_login_challenges"

# How long a challenge stays redeemable. Long enough that an operator who
# opens the page and reads it first is not refused; short enough that a
# captured challenge is not useful for long. The panel refreshes its options
# well inside this, so an idle page never holds a dead one.
CHALLENGE_TTL_SECONDS = 300

# Bounds the store. A page that refreshes on a timer, in a few tabs, stays far
# below it; anything above it is evicted oldest first.
MAX_OUTSTANDING_CHALLENGES = 8

# How long the browser waits for the person to answer the prompt -- a fumbled
# security key, a phone being fetched for a QR scan. Separate from the TTL
# above, which is how long the server waits for the prompt to be *started*.
CEREMONY_TIMEOUT_MS = 120_000

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



def _issue_challenge(session, key: str, challenge: bytes, *, user_id=None) -> None:
    """Remember a challenge as redeemable, pruning the expired and the excess."""
    now = time.time()
    store = {
        value: entry
        for value, entry in (session.get(key) or {}).items()
        if isinstance(entry, dict) and now - entry.get("at", 0) < CHALLENGE_TTL_SECONDS
    }
    store[bytes_to_base64url(challenge)] = {"at": now, "user": user_id}

    if len(store) > MAX_OUTSTANDING_CHALLENGES:
        newest = sorted(store.items(), key=lambda item: item[1]["at"])
        store = dict(newest[-MAX_OUTSTANDING_CHALLENGES:])

    # Reassigned, never mutated in place: Django only saves a session whose
    # top-level keys were set.
    session[key] = store


def _redeem_challenge(session, key: str, credential: dict, *, user_id=None) -> bytes:
    """
    Take the challenge this credential answers out of the store, once.

    The browser signs over clientDataJSON, which names the challenge it was
    given. Reading it from there is what lets several be outstanding: the
    response says which one it answers, and verification then checks the
    signature over exactly that. Trusting it is safe because verification
    fails unless the server issued that challenge *and* the signature covers it.
    """
    try:
        raw = (credential.get("response") or {}).get("clientDataJSON") or ""
        answered = json.loads(base64url_to_bytes(raw))["challenge"]
    except Exception as exc:
        raise PasskeyError("The browser's response was not readable. Try again.") from exc

    store = dict(session.get(key) or {})
    entry = store.pop(answered, None)
    session[key] = store

    if not isinstance(entry, dict):
        raise PasskeyError(
            "This request has already been used or was never issued here. Try again."
        )
    if time.time() - entry.get("at", 0) >= CHALLENGE_TTL_SECONDS:
        raise PasskeyError("This request expired before it was answered. Try again.")
    if user_id is not None and entry.get("user") != user_id:
        raise PasskeyError("This request was started by another account. Try again.")

    return base64url_to_bytes(answered)


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
    return current_rp_id() is not None


def current_rp_id() -> str | None:
    """The domain passkeys are bound to now, or None while the panel has none."""
    try:
        return relying_party().id
    except PasskeysUnavailable:
        return None


def usable_passkeys(user):
    """
    This account's passkeys that can sign in at the panel's current address.

    A passkey bound to a hostname the panel has since moved off cannot be
    used, and must not go on counting as a second factor: if it did, the
    account would be waved past the enrolment gate holding a factor that
    cannot be presented.
    """
    rp_id = current_rp_id()
    if rp_id is None:
        return Passkey.objects.none()
    return Passkey.objects.filter(user=user, rp_id=rp_id)


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
        timeout=CEREMONY_TIMEOUT_MS,
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

    _issue_challenge(session, REGISTER_CHALLENGES, options.challenge, user_id=user.pk)
    return options_to_json(options)


def registration_verify(*, user, session, credential: dict, label: str) -> Passkey:
    """Finish enrolment and store the public key."""
    rp = relying_party()
    challenge = _redeem_challenge(session, REGISTER_CHALLENGES, credential, user_id=user.pk)

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            require_user_verification=True,
        )
    except Exception as exc:
        # The library's reason goes to the log, not the browser: it names
        # internals (origin, rp hash, attestation format) that help whoever
        # is probing the endpoint more than whoever is enrolling.
        logger.warning("Passkey enrolment rejected for %s: %s", user, exc)
        raise PasskeyError(
            "That passkey could not be registered. If this keeps happening, "
            "check the panel is open at https://%s." % rp.id
        ) from exc

    credential_id = bytes_to_base64url(verified.credential_id)
    try:
        # Atomic so a duplicate -- a double-submitted form, or an authenticator
        # that ignored excludeCredentials -- rolls back cleanly instead of
        # poisoning the request's transaction and surfacing as a 500.
        with transaction.atomic():
            return Passkey.objects.create(
                user=user,
                label=(label or "").strip()[:64] or "Passkey",
                credential_id=credential_id,
                public_key=bytes_to_base64url(verified.credential_public_key),
                sign_count=verified.sign_count,
                transports=_clean_transports(credential.get("transports")),
                is_backed_up=bool(getattr(verified, "credential_backed_up", False)),
                rp_id=rp.id,
            )
    except IntegrityError as exc:
        raise PasskeyError("This passkey is already registered.") from exc


_KNOWN_TRANSPORTS = {"usb", "nfc", "ble", "internal", "hybrid", "smart-card", "cable"}


def _clean_transports(value) -> list[str]:
    """Browser-supplied, so kept to known strings rather than stored as given."""
    if not isinstance(value, list):
        return []
    return [t for t in value if isinstance(t, str) and t in _KNOWN_TRANSPORTS]


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
        timeout=CEREMONY_TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    _issue_challenge(session, LOGIN_CHALLENGES, options.challenge)
    return options_to_json(options)


def authentication_verify(*, session, credential: dict) -> Passkey:
    """Check an assertion and return the passkey that produced it."""
    rp = relying_party()
    challenge = _redeem_challenge(session, LOGIN_CHALLENGES, credential)

    credential_id = credential.get("id") or credential.get("rawId") or ""
    if not isinstance(credential_id, str):
        raise PasskeyError("That passkey is not registered on this panel.")
    passkey = (
        Passkey.objects
        .select_related("user")
        .filter(credential_id=credential_id)
        .first()
    )
    if passkey is None:
        raise PasskeyError("That passkey is not registered on this panel.")
    if passkey.rp_id != rp.id:
        raise PasskeyError(
            "That passkey was made for %s, not this address. Sign in another "
            "way and add a new one." % (passkey.rp_id or "an earlier address")
        )

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
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

    # Conditional on the count it was verified against, so two assertions
    # racing each other cannot both win: the loser updates nothing and is
    # refused, exactly as a replayed counter would be.
    updated = Passkey.objects.filter(
        pk=passkey.pk, sign_count=passkey.sign_count
    ).update(sign_count=verified.new_sign_count, last_used_at=timezone.now())
    if not updated:
        raise PasskeyError("That passkey was not accepted.")

    passkey.refresh_from_db()
    return passkey
