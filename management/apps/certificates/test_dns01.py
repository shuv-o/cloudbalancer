"""
Wildcard certificates, and the two places zones actually live.

A wildcard cannot be issued over HTTP-01 at all. Proving control of
a.example.com says nothing about b.example.com, so the only proof a CA will
accept for *.example.com is a TXT record in the zone -- which means writing to
DNS, which means credentials, which means knowing whose DNS.

Both providers are here because both are real: some zones at Cloudflare, some
on BIND or Knot. The matching between them is the part worth testing hardest,
because getting it wrong hands a challenge to a server with no authority over
the name and the error arrives sixty days later during an unattended renewal.
"""
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.certificates.dns_providers import (
    DnsProvider,
    DnsProviderError,
    provider_for,
)
from apps.certificates.models import AcmeAccount, Certificate
from apps.certificates.services import (
    CertificateError,
    _challenge_plan,
    certificate_issue_acme,
    is_wildcard,
)
from apps.domains.services import domain_create
from apps.security.roles import Role
from apps.security.testing import make_operator
from common.secrets import decrypt, encrypt


def cloudflare(**overrides) -> DnsProvider:
    defaults = {
        "name": "cloudflare",
        "kind": DnsProvider.Kind.CLOUDFLARE,
        "zones": ["example.com"],
        "api_token": "cf-token-abcdef123456",
    }
    defaults.update(overrides)
    return DnsProvider.objects.create(**defaults)


def knot(**overrides) -> DnsProvider:
    defaults = {
        "name": "knot",
        "kind": DnsProvider.Kind.RFC2136,
        "zones": ["example.net"],
        "server": "10.0.0.53",
        "tsig_name": "cloudbalancer-key",
        "tsig_algorithm": "HMAC-SHA256",
        "tsig_secret": "c2VjcmV0LWtleS1tYXRlcmlhbA==",
    }
    defaults.update(overrides)
    return DnsProvider.objects.create(**defaults)


class ZoneMatchingTests(TestCase):
    def test_a_name_in_the_zone_matches(self):
        provider = cloudflare()
        self.assertEqual(provider.covers("api.example.com"), "example.com")
        self.assertEqual(provider.covers("example.com"), "example.com")

    def test_a_wildcard_matches_its_zone(self):
        provider = cloudflare()
        self.assertEqual(provider.covers("*.example.com"), "example.com")

    def test_matching_is_on_label_boundaries(self):
        """
        A plain suffix check matches notexample.com against example.com.

        That provider would then be handed a challenge it has no authority to
        write, and the failure arrives from the CA rather than from here.
        """
        provider = cloudflare()
        self.assertIsNone(provider.covers("notexample.com"))
        self.assertIsNone(provider.covers("example.com.evil.test"))

    def test_a_name_in_another_zone_does_not_match(self):
        self.assertIsNone(cloudflare().covers("example.net"))

    def test_the_right_provider_is_chosen_for_each_zone(self):
        cf = cloudflare()
        kn = knot()

        self.assertEqual(provider_for("api.example.com").pk, cf.pk)
        self.assertEqual(provider_for("api.example.net").pk, kn.pk)
        self.assertIsNone(provider_for("api.example.org"))

    def test_a_delegated_subzone_wins_over_its_parent(self):
        """
        example.com at Cloudflare and research.example.com on Knot is a real
        arrangement, and the longer zone is the one with authority.
        """
        cloudflare(zones=["example.com"])
        inner = knot(name="knot-research", zones=["research.example.com"])

        self.assertEqual(provider_for("www.research.example.com").pk, inner.pk)
        self.assertEqual(provider_for("www.example.com").name, "cloudflare")

    def test_a_disabled_provider_is_not_chosen(self):
        cloudflare(is_enabled=False)
        self.assertIsNone(provider_for("api.example.com"))


class ConfigValidationTests(TestCase):
    def test_a_provider_with_no_zones_is_refused(self):
        provider = DnsProvider(
            name="x", kind=DnsProvider.Kind.CLOUDFLARE, zones=[], api_token="t"
        )
        with self.assertRaises(DnsProviderError):
            provider.validate_config()

    def test_cloudflare_without_a_token_is_refused(self):
        provider = DnsProvider(
            name="x", kind=DnsProvider.Kind.CLOUDFLARE, zones=["example.com"]
        )
        with self.assertRaises(DnsProviderError):
            provider.validate_config()

    def test_rfc2136_names_every_missing_field_at_once(self):
        """
        Rather than one per attempt. Filling these in is a copy-and-paste job
        from a nameserver config, and three round trips to discover three
        missing fields is three chances to give up.
        """
        provider = DnsProvider(
            name="x", kind=DnsProvider.Kind.RFC2136, zones=["example.net"]
        )
        with self.assertRaises(DnsProviderError) as caught:
            provider.validate_config()

        message = str(caught.exception)
        self.assertIn("nameserver address", message)
        self.assertIn("TSIG key name", message)
        self.assertIn("TSIG secret", message)

    def test_an_unknown_tsig_algorithm_is_refused(self):
        """
        A typo here fails at renewal, months later, with an error from
        dnspython that names neither the zone nor the provider.
        """
        provider = knot(tsig_algorithm="HMAC-SHA255")
        with self.assertRaises(DnsProviderError):
            provider.validate_config()

    def test_a_zone_that_is_not_a_domain_is_refused(self):
        provider = cloudflare(zones=["not a zone"])
        with self.assertRaises(DnsProviderError):
            provider.validate_config()


class CredentialStorageTests(TestCase):
    def column(self, provider, name: str) -> str:
        """
        The bytes actually on disk.

        Not objects.raw(), which still runs the field's converters and so
        hands back the decrypted value -- a test written that way passes
        against a column storing plaintext, which is the one thing it exists
        to rule out.
        """
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {name} FROM certificates_dnsprovider WHERE id = %s",
                [provider.pk],
            )
            return cursor.fetchone()[0]

    def test_a_token_is_encrypted_in_the_column(self):
        """
        It is in every nightly backup. Encrypting the column is what keeps a
        dump copied to a laptop from being a working Cloudflare token.
        """
        provider = cloudflare(api_token="cf-token-secret-value")

        stored = self.column(provider, "api_token")

        self.assertNotIn("cf-token-secret-value", stored)
        self.assertTrue(stored.startswith("enc:v1:"))

    def test_it_reads_back_unchanged(self):
        provider = cloudflare(api_token="cf-token-secret-value")
        provider.refresh_from_db()
        self.assertEqual(provider.api_token, "cf-token-secret-value")

    def test_a_tsig_secret_is_encrypted_too(self):
        provider = knot(tsig_secret="base64secretkeymaterial==")
        stored = self.column(provider, "tsig_secret")
        self.assertNotIn("base64secretkeymaterial", stored)

    def test_a_value_stored_before_encryption_still_reads(self):
        """
        Turning this on must not orphan existing rows, which would mean every
        provider silently failing at its next renewal.
        """
        self.assertEqual(decrypt("plain-old-value"), "plain-old-value")

    def test_round_tripping_is_stable(self):
        self.assertEqual(decrypt(encrypt("hello")), "hello")
        self.assertEqual(encrypt(""), "")
        self.assertEqual(decrypt(""), "")


class CredentialsFileTests(TestCase):
    def setUp(self):
        import tempfile

        self.directory = Path(tempfile.mkdtemp(prefix="cb-dns-"))

    def test_cloudflare_writes_the_format_its_plugin_reads(self):
        path = cloudflare().write_credentials(self.directory)
        self.assertIn("dns_cloudflare_api_token = cf-token-abcdef123456", path.read_text())

    def test_rfc2136_writes_every_field_its_plugin_needs(self):
        path = knot().write_credentials(self.directory)
        body = path.read_text()

        for expected in (
            "dns_rfc2136_server = 10.0.0.53",
            "dns_rfc2136_port = 53",
            "dns_rfc2136_name = cloudbalancer-key",
            "dns_rfc2136_algorithm = HMAC-SHA256",
        ):
            self.assertIn(expected, body)

    def test_the_file_is_not_readable_by_anyone_else(self):
        """
        Certbot refuses a loose credentials file, which is correct: this is a
        key that can write to a zone.
        """
        path = knot().write_credentials(self.directory)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    def test_the_arguments_name_the_right_plugin(self):
        path = self.directory / "x.ini"

        self.assertIn("--dns-cloudflare", cloudflare().certbot_args(path))
        self.assertIn("--dns-rfc2136", knot().certbot_args(path))

    def test_propagation_time_is_passed_through(self):
        args = knot(propagation_seconds=120).certbot_args(self.directory / "x.ini")
        self.assertIn("--dns-rfc2136-propagation-seconds", args)
        self.assertIn("120", args)


class ChallengePlanTests(TestCase):
    def test_a_plain_name_with_no_provider_uses_http(self):
        provider, challenge = _challenge_plan(["api.example.org"])
        self.assertIsNone(provider)
        self.assertEqual(challenge, "http-01")

    def test_a_name_with_a_provider_prefers_dns(self):
        """
        DNS-01 needs no inbound port 80, so it keeps working behind a
        firewall and before DNS has been pointed at this gateway at all.
        """
        cloudflare()
        provider, challenge = _challenge_plan(["api.example.com"])

        self.assertEqual(provider.name, "cloudflare")
        self.assertEqual(challenge, "dns-01")

    def test_a_wildcard_uses_dns(self):
        cloudflare()
        _, challenge = _challenge_plan(["*.example.com"])
        self.assertEqual(challenge, "dns-01")

    def test_a_wildcard_with_no_provider_says_what_is_missing(self):
        """
        Rather than falling back to HTTP-01, which cannot work, and failing
        at the CA with a message about an unauthorised challenge type.
        """
        with self.assertRaises(CertificateError) as caught:
            _challenge_plan(["*.example.org"])

        message = str(caught.exception)
        self.assertIn("*.example.org", message)
        self.assertIn("DNS provider", message)

    def test_names_spread_across_providers_are_refused(self):
        """
        certbot takes one plugin per run. Splitting them silently would
        produce several certificates, which is not what was asked for.
        """
        cloudflare()
        knot()

        with self.assertRaises(CertificateError) as caught:
            _challenge_plan(["api.example.com", "api.example.net"])

        self.assertIn("separately", str(caught.exception))

    def test_a_wildcard_and_its_apex_in_one_zone_are_fine(self):
        """The ordinary shape of a wildcard certificate."""
        cloudflare()
        provider, challenge = _challenge_plan(["*.example.com", "example.com"])
        self.assertEqual(challenge, "dns-01")
        self.assertEqual(provider.name, "cloudflare")

    def test_is_wildcard(self):
        self.assertTrue(is_wildcard("*.example.com"))
        self.assertFalse(is_wildcard("example.com"))
        self.assertFalse(is_wildcard("star.example.com"))


class IssuanceTests(TestCase):
    def setUp(self):
        self.account = AcmeAccount.objects.create(
            email="ops@example.com",
            directory_url="https://acme.example.com/directory",
            agreed_to_tos=True,
            is_default=True,
            registered_at="2026-01-01T00:00:00Z",
        )

    def run_issue(self, domain_name, *, ok=True, output="done"):
        domain = domain_create(name=domain_name)
        with mock.patch(
            "apps.certificates.services._certbot_run", return_value=(ok, output)
        ) as run:
            certificate = certificate_issue_acme(domain=domain, account=self.account)
        return certificate, run

    def test_a_wildcard_is_issued_with_the_dns_plugin(self):
        cloudflare()
        _, run = self.run_issue("*.example.com")

        args = run.call_args.args[0]
        self.assertIn("--dns-cloudflare", args)
        self.assertNotIn("--webroot", args)

    def test_a_wildcard_does_not_create_a_glob_in_a_path(self):
        """
        certbot names its live directory after --cert-name. A literal
        `*.example.com` directory is matched by every shell glob on the host,
        including the ones in backup and cleanup scripts.
        """
        cloudflare()
        _, run = self.run_issue("*.example.com")

        args = run.call_args.args[0]
        cert_name = args[args.index("--cert-name") + 1]
        self.assertNotIn("*", cert_name)
        self.assertEqual(cert_name, "wildcard.example.com")

    def test_the_name_itself_still_goes_to_the_ca_with_its_star(self):
        cloudflare()
        _, run = self.run_issue("*.example.com")

        args = run.call_args.args[0]
        self.assertIn("*.example.com", args)

    def test_an_rfc2136_zone_uses_that_plugin(self):
        knot()
        _, run = self.run_issue("*.example.net")
        self.assertIn("--dns-rfc2136", run.call_args.args[0])

    def test_a_plain_name_with_no_provider_still_uses_webroot(self):
        _, run = self.run_issue("api.example.org")

        args = run.call_args.args[0]
        self.assertIn("--webroot", args)
        self.assertNotIn("--dns-cloudflare", args)

    def test_the_credentials_file_is_removed_afterwards(self):
        """
        It holds a token that can edit every record in an account. Leaving it
        between renewals widens the window for no benefit.
        """
        cloudflare()
        _, run = self.run_issue("*.example.com")

        args = run.call_args.args[0]
        path = Path(args[args.index("--dns-cloudflare-credentials") + 1])
        self.assertFalse(path.exists())

    def test_it_is_removed_even_when_issuance_fails(self):
        cloudflare()
        _, run = self.run_issue("*.example.com", ok=False, output="CA said no")

        args = run.call_args.args[0]
        path = Path(args[args.index("--dns-cloudflare-credentials") + 1])
        self.assertFalse(path.exists())

    def test_a_dns_run_gets_longer_than_a_webroot_one(self):
        """
        DNS-01 waits for propagation. A timeout sized for HTTP-01 cancels a
        healthy issuance partway through and leaves the TXT record behind.
        """
        cloudflare(propagation_seconds=300)
        _, run = self.run_issue("*.example.com")

        self.assertGreater(run.call_args.kwargs["timeout"], 300)

    def test_a_wildcard_without_a_provider_fails_with_a_reason(self):
        domain = domain_create(name="*.nowhere.example")
        with mock.patch("apps.certificates.services._certbot_run") as run:
            certificate = certificate_issue_acme(domain=domain, account=self.account)

        run.assert_not_called()
        self.assertEqual(certificate.status, Certificate.Status.FAILED)
        self.assertIn("DNS provider", certificate.last_error)


class RenewalTests(TestCase):
    def setUp(self):
        self.account = AcmeAccount.objects.create(
            email="ops@example.com",
            directory_url="https://acme.example.com/directory",
            agreed_to_tos=True,
            is_default=True,
            registered_at="2026-01-01T00:00:00Z",
        )

    def test_renewal_keeps_every_name_on_the_certificate(self):
        """
        The bug this would otherwise have.

        A wildcard certificate is almost always *.example.com plus
        example.com. Renewing with only the domain silently replaces it with
        a certificate covering one name, and the symptom is every subdomain
        failing TLS sixty days later, unattended.
        """
        from apps.certificates.services import certificate_renew

        cloudflare()
        domain = domain_create(name="*.example.com")
        certificate = Certificate.objects.create(
            domain=domain,
            issuer=Certificate.Issuer.ACME,
            acme_account=self.account,
            san_domains=["*.example.com", "example.com"],
        )

        with mock.patch(
            "apps.certificates.services._certbot_run", return_value=(True, "ok")
        ) as run:
            certificate_renew(certificate=certificate)

        args = run.call_args.args[0]
        self.assertIn("*.example.com", args)
        self.assertIn("example.com", args)


class DnsProviderApiTests(TestCase):
    URL = "/api/v1/certificates/dns-providers/"

    def setUp(self):
        self.admin = make_operator(username="boss", superuser=True)

    def test_an_admin_can_add_a_cloudflare_provider(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.URL,
            {
                "name": "cf",
                "kind": "cloudflare",
                "zones": ["example.com"],
                "api_token": "cf-token-value",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(DnsProvider.objects.count(), 1)

    def test_an_admin_can_add_an_rfc2136_provider(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.URL,
            {
                "name": "knot",
                "kind": "rfc2136",
                "zones": ["example.net"],
                "server": "10.0.0.53",
                "tsig_name": "key",
                "tsig_algorithm": "HMAC-SHA256",
                "tsig_secret": "c2VjcmV0",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)

    def test_a_provider_that_cannot_work_is_refused(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.URL,
            {"name": "broken", "kind": "cloudflare", "zones": ["example.com"]},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(DnsProvider.objects.count(), 0)

    def test_no_endpoint_returns_a_credential(self):
        """
        A value that can be read back out of an admin page is a value that
        leaves in a screenshot.
        """
        cloudflare(api_token="cf-token-very-secret")
        knot(tsig_secret="tsig-very-secret")
        self.client.force_login(self.admin)

        body = self.client.get(self.URL).content.decode()

        self.assertNotIn("cf-token-very-secret", body)
        self.assertNotIn("tsig-very-secret", body)

    def test_an_edit_that_omits_the_secret_keeps_it(self):
        """
        The panel cannot show the current value, so it cannot send it back.
        Without this, changing a propagation timeout wipes the token.
        """
        provider = cloudflare(api_token="cf-token-value")
        self.client.force_login(self.admin)

        response = self.client.patch(
            f"{self.URL}{provider.pk}/",
            {"propagation_seconds": 120},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        provider.refresh_from_db()
        self.assertEqual(provider.api_token, "cf-token-value")
        self.assertEqual(provider.propagation_seconds, 120)

    def test_removing_a_provider_a_wildcard_depends_on_is_refused(self):
        """
        Otherwise the next renewal fails, sixty days later, for a reason
        nobody connects to a change made today.
        """
        provider = cloudflare()
        domain_create(name="*.example.com")
        self.client.force_login(self.admin)

        response = self.client.delete(f"{self.URL}{provider.pk}/")

        self.assertEqual(response.status_code, 400)
        self.assertIn("*.example.com", response.json()["error"])
        self.assertTrue(DnsProvider.objects.filter(pk=provider.pk).exists())

    def test_removing_an_unused_provider_is_fine(self):
        provider = cloudflare()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.delete(f"{self.URL}{provider.pk}/").status_code, 204)

    def test_coverage_says_which_domains_can_have_a_wildcard(self):
        cloudflare()
        domain_create(name="api.example.com")
        domain_create(name="api.elsewhere.test")
        self.client.force_login(self.admin)

        coverage = {row["domain"]: row for row in self.client.get(self.URL).json()["coverage"]}

        self.assertEqual(coverage["api.example.com"]["provider"], "cloudflare")
        self.assertIsNone(coverage["api.elsewhere.test"]["provider"])

    def test_an_operator_cannot_see_dns_providers(self):
        self.client.force_login(
            make_operator(username="op", superuser=False, role=Role.OPERATOR)
        )
        self.assertEqual(self.client.get(self.URL).status_code, 403)
