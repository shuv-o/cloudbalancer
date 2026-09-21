"""
Tests for certificate handling.

Issuance itself talks to a remote authority, so those paths are exercised with
certbot stubbed out. What is tested for real is everything around it: whether a
key actually matches its certificate, when renewal is due, and whether a
certificate correctly switches its domain onto TLS.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.test import TestCase
from django.utils import timezone

from apps.certificates.models import AcmeAccount, Certificate
from apps.certificates.selectors import (
    certificate_expiry_summary,
    certificate_list_due_for_renewal,
)
from apps.certificates.services import (
    CertificateError,
    certificate_generate_self_signed,
    certificate_import,
    certificate_issue_acme,
    certificate_sync_metadata,
)
from apps.domains.services import domain_create


def make_pem_pair(common_name="api.example.com", days=90, issued_days_ago=0):
    """A matching certificate and key. Negative `days` makes an expired one."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(dt_timezone.utc)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=issued_days_ago, minutes=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


class SelfSignedTests(TestCase):
    def setUp(self):
        self.domain = domain_create(name="internal.lan")

    def test_material_is_written_and_readable(self):
        certificate = certificate_generate_self_signed(domain=self.domain)

        self.assertTrue(Path(certificate.cert_path).exists())
        self.assertTrue(Path(certificate.key_path).exists())
        self.assertEqual(certificate.status, Certificate.Status.ACTIVE)
        self.assertEqual(certificate.subject_common_name, "internal.lan")

    def test_private_key_is_not_world_readable(self):
        certificate = certificate_generate_self_signed(domain=self.domain)
        mode = Path(certificate.key_path).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_domain_is_switched_onto_tls(self):
        certificate = certificate_generate_self_signed(domain=self.domain)
        self.domain.refresh_from_db()

        self.assertTrue(self.domain.ssl_enabled)
        self.assertEqual(self.domain.ssl_cert_path, certificate.cert_path)

    def test_self_signed_does_not_auto_renew(self):
        """Nothing can renew it, so promising to would be a lie on the page."""
        certificate = certificate_generate_self_signed(domain=self.domain)
        self.assertFalse(certificate.auto_renew)
        self.assertFalse(certificate.is_due_for_renewal)


class ImportTests(TestCase):
    def setUp(self):
        self.domain = domain_create(name="api.example.com")

    def test_matching_pair_is_accepted(self):
        cert_pem, key_pem = make_pem_pair()
        certificate = certificate_import(
            domain=self.domain, cert_pem=cert_pem, key_pem=key_pem
        )

        self.assertEqual(certificate.issuer, Certificate.Issuer.MANUAL)
        self.assertEqual(certificate.status, Certificate.Status.ACTIVE)
        self.assertIn("api.example.com", certificate.san_domains)

    def test_mismatched_key_is_refused(self):
        """
        Nginx would fail to start on a mismatched pair, taking every domain
        down -- not just the one being changed.
        """
        cert_pem, _ = make_pem_pair()
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        wrong_key = other_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        with self.assertRaises(CertificateError) as caught:
            certificate_import(domain=self.domain, cert_pem=cert_pem, key_pem=wrong_key)
        self.assertIn("does not match", str(caught.exception))

    def test_malformed_certificate_is_refused(self):
        _, key_pem = make_pem_pair()
        with self.assertRaises(CertificateError):
            certificate_import(
                domain=self.domain, cert_pem="not a certificate", key_pem=key_pem
            )

    def test_malformed_key_is_refused(self):
        cert_pem, _ = make_pem_pair()
        with self.assertRaises(CertificateError):
            certificate_import(domain=self.domain, cert_pem=cert_pem, key_pem="not a key")


class MetadataTests(TestCase):
    def test_dates_and_names_are_read_off_disk(self):
        domain = domain_create(name="api.example.com")
        cert_pem, key_pem = make_pem_pair(days=45)
        certificate = certificate_import(
            domain=domain, cert_pem=cert_pem, key_pem=key_pem
        )

        self.assertIsNotNone(certificate.not_after)
        self.assertEqual(certificate.days_until_expiry, 44)
        self.assertTrue(certificate.fingerprint_sha256)

    def test_an_expired_certificate_is_marked_expired_on_sync(self):
        """
        Catches certificates that lapsed while nothing was watching -- a
        certbot cron on the host that stopped running, say.
        """
        domain = domain_create(name="lapsed.example.com")
        cert_pem, key_pem = make_pem_pair(
            common_name="lapsed.example.com", days=-1, issued_days_ago=90
        )
        certificate = certificate_import(
            domain=domain, cert_pem=cert_pem, key_pem=key_pem
        )

        self.assertEqual(certificate.status, Certificate.Status.EXPIRED)
        self.assertLess(certificate.days_until_expiry, 0)


class RenewalWindowTests(TestCase):
    def setUp(self):
        self.account = AcmeAccount.objects.create(
            email="ops@example.com", agreed_to_tos=True, is_default=True
        )

    def _certificate(self, name, days, **kwargs):
        domain = domain_create(name=name)
        return Certificate.objects.create(
            domain=domain,
            acme_account=self.account,
            issuer=Certificate.Issuer.ACME,
            status=Certificate.Status.ACTIVE,
            cert_path=f"/etc/letsencrypt/live/{name}/fullchain.pem",
            key_path=f"/etc/letsencrypt/live/{name}/privkey.pem",
            not_after=timezone.now() + timedelta(days=days),
            **kwargs,
        )

    def test_certificate_inside_the_window_is_due(self):
        self.assertTrue(self._certificate("soon.example.com", 10).is_due_for_renewal)

    def test_certificate_outside_the_window_is_not_due(self):
        self.assertFalse(self._certificate("later.example.com", 60).is_due_for_renewal)

    def test_renewal_can_be_turned_off_per_certificate(self):
        certificate = self._certificate("manual.example.com", 5, auto_renew=False)
        self.assertFalse(certificate.is_due_for_renewal)

    def test_sweep_selects_only_what_is_due(self):
        self._certificate("soon.example.com", 10)
        self._certificate("later.example.com", 60)

        due = [c for c in certificate_list_due_for_renewal() if c.is_due_for_renewal]
        self.assertEqual([c.domain.name for c in due], ["soon.example.com"])

    def test_a_certificate_mid_renewal_is_not_picked_up_again(self):
        certificate = self._certificate("soon.example.com", 10)
        certificate.status = Certificate.Status.RENEWING
        certificate.save()

        self.assertEqual(list(certificate_list_due_for_renewal()), [])

    def test_summary_counts_what_needs_attention(self):
        self._certificate("soon.example.com", 9)
        self._certificate("later.example.com", 60)

        summary = certificate_expiry_summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["expiring_within_14d"], 1)
        self.assertEqual(summary["auto_renewing"], 2)


class AcmeIssuanceTests(TestCase):
    """Issuance with certbot stubbed, so no network and no rate limits."""

    def setUp(self):
        self.account = AcmeAccount.objects.create(
            email="ops@example.com", agreed_to_tos=True, is_default=True
        )
        self.domain = domain_create(name="api.example.com")

    @patch("apps.certificates.services._certbot_run", return_value=(True, "Congratulations!"))
    def test_success_points_the_domain_at_the_live_directory(self, _run):
        certificate = certificate_issue_acme(domain=self.domain, account=self.account)
        self.domain.refresh_from_db()

        self.assertEqual(certificate.status, Certificate.Status.ACTIVE)
        self.assertTrue(certificate.cert_path.endswith("api.example.com/fullchain.pem"))
        self.assertTrue(self.domain.ssl_enabled)

    @patch(
        "apps.certificates.services._certbot_run",
        return_value=(False, "Detail: DNS problem: NXDOMAIN looking up A for api.example.com"),
    )
    def test_failure_records_the_actionable_line(self, _run):
        """
        Certbot's output runs to dozens of lines. The dashboard shows one, so
        it had better be the one naming the problem.
        """
        certificate = certificate_issue_acme(domain=self.domain, account=self.account)

        self.assertEqual(certificate.status, Certificate.Status.FAILED)
        self.assertIn("NXDOMAIN", certificate.last_error)

    @patch("apps.certificates.services._certbot_run", return_value=(True, ""))
    def test_failure_does_not_switch_the_domain_to_tls(self, run):
        run.return_value = (False, "Detail: connection refused")
        certificate_issue_acme(domain=self.domain, account=self.account)
        self.domain.refresh_from_db()

        self.assertFalse(self.domain.ssl_enabled)

    def test_a_name_no_authority_can_validate_is_refused_early(self):
        """
        Asking Let's Encrypt for "internal" burns a rate-limited attempt to be
        told no. Refuse it here and suggest the thing that would work.
        """
        internal = domain_create(name="internal")
        with self.assertRaises(CertificateError) as caught:
            certificate_issue_acme(domain=internal, account=self.account)
        self.assertIn("self-signed", str(caught.exception))

    def test_issuance_without_an_account_explains_what_to_do(self):
        AcmeAccount.objects.all().delete()
        with self.assertRaises(CertificateError) as caught:
            certificate_issue_acme(domain=self.domain)
        self.assertIn("No ACME account", str(caught.exception))
