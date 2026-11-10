"""
Stapling is emitted per certificate, not everywhere.

Nginx logs `"ssl_stapling" ignored, no OCSP responder URL in the certificate`
on every reload for a certificate that carries no responder -- which, since
Let's Encrypt retired OCSP, is every certificate this control plane issues.
These tests pin both halves: no responder means no directive and no warning, a
responder means the directive is still emitted and stapling still works.
"""
import datetime
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import TestCase

from apps.backends.services import backend_create, instance_add
from apps.certificates.models import Certificate
from apps.domains.services import domain_create
from apps.gateway.ocsp import has_ocsp_responder
from apps.gateway.services import config_render
from apps.routing.services import rule_create

OCSP_URL = "http://ocsp.example-ca.com"
CA_ISSUERS_URL = "http://crt.example-ca.com/ca.der"


def write_certificate(directory: Path, *, aia: list | None) -> Path:
    """A self-signed leaf at <directory>/fullchain.pem, with the given AIA."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "app.example.com")])
    now = datetime.datetime.now(datetime.timezone.utc)

    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=30))
    )
    if aia is not None:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(aia), critical=False
        )

    certificate = builder.sign(key, hashes.SHA256())
    path = directory / "fullchain.pem"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path


def ocsp_entry(url: str = OCSP_URL) -> x509.AccessDescription:
    return x509.AccessDescription(
        x509.oid.AuthorityInformationAccessOID.OCSP,
        x509.UniformResourceIdentifier(url),
    )


def ca_issuers_entry() -> x509.AccessDescription:
    return x509.AccessDescription(
        x509.oid.AuthorityInformationAccessOID.CA_ISSUERS,
        x509.UniformResourceIdentifier(CA_ISSUERS_URL),
    )


class HasOcspResponderTests(TestCase):
    """What the leaf certificate actually carries."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_responder_present(self):
        path = write_certificate(self.dir, aia=[ocsp_entry()])
        self.assertTrue(has_ocsp_responder(str(path)))

    def test_no_aia_extension_at_all(self):
        """A Let's Encrypt leaf since 2025 -- the case that caused the noise."""
        path = write_certificate(self.dir, aia=None)
        self.assertFalse(has_ocsp_responder(str(path)))

    def test_aia_without_an_ocsp_entry(self):
        """CA Issuers is also an AIA entry. Finding the extension is not enough."""
        path = write_certificate(self.dir, aia=[ca_issuers_entry()])
        self.assertFalse(has_ocsp_responder(str(path)))

    def test_mixed_aia_entries(self):
        path = write_certificate(self.dir, aia=[ca_issuers_entry(), ocsp_entry()])
        self.assertTrue(has_ocsp_responder(str(path)))

    def test_leaf_is_read_not_the_rest_of_the_chain(self):
        """cert_path is a fullchain. The leaf comes first and it is the one that counts."""
        leaf = write_certificate(self.dir, aia=None).read_bytes()

        issuer_dir = self.dir / "issuer"
        issuer_dir.mkdir()
        intermediate = write_certificate(issuer_dir, aia=[ocsp_entry()]).read_bytes()

        fullchain = self.dir / "combined.pem"
        fullchain.write_bytes(leaf + intermediate)
        self.assertFalse(has_ocsp_responder(str(fullchain)))

    def test_missing_file(self):
        self.assertFalse(has_ocsp_responder(str(self.dir / "absent.pem")))

    def test_unparseable_file(self):
        path = self.dir / "junk.pem"
        path.write_text("this is not a certificate")
        self.assertFalse(has_ocsp_responder(str(path)))

    def test_empty_path(self):
        self.assertFalse(has_ocsp_responder(""))


class StaplingRenderTests(TestCase):
    """What reaches the rendered Nginx config."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

        backend = backend_create(name="appserver")
        instance_add(backend=backend, address="10.0.0.1", port=8080)
        self.backend = backend

    def _render_with(self, *, aia: list | None) -> str:
        cert_path = write_certificate(self.dir, aia=aia)
        domain = domain_create(
            name="app.example.com",
            ssl_enabled=True,
            ssl_cert_path=str(cert_path),
            ssl_key_path=str(self.dir / "privkey.pem"),
        )
        Certificate.objects.create(
            domain=domain,
            cert_path=str(cert_path),
            key_path=str(self.dir / "privkey.pem"),
            chain_path=str(self.dir / "chain.pem"),
            status=Certificate.Status.ACTIVE,
        )
        rule_create(domain_id=domain.id, backend_id=self.backend.id)
        return config_render()["20-servers.conf"]

    def test_no_responder_emits_no_stapling(self):
        config = self._render_with(aia=None)

        self.assertIn("ssl_certificate", config)
        self.assertNotIn("ssl_stapling", config)
        self.assertNotIn("ssl_trusted_certificate", config)

    def test_responder_still_emits_stapling(self):
        config = self._render_with(aia=[ocsp_entry()])

        self.assertIn("ssl_stapling            on;", config)
        self.assertIn("ssl_stapling_verify     on;", config)
        self.assertIn(str(self.dir / "chain.pem"), config)
