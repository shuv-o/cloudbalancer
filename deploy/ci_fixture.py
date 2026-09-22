"""
Build a representative topology and render it, so CI can hand real files to a
real Nginx.

Everything this project asserts about the generated configuration is asserted
against strings. That catches a missing directive; it cannot catch a directive
that does not mean what we think it means, or one Nginx rejects outright. This
fixture exists so `nginx -t` gets a say, against an image with the traffic
module actually compiled in.

The topology deliberately exercises the awkward parts: TLS with a redirect,
TLS without one, header-based routing, caching with a varied key, rate limits,
a regex location, a streaming route and a blocked range.
"""
import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "management"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
django.setup()

from django.core.management import call_command  # noqa: E402

from apps.backends.services import backend_create, instance_add  # noqa: E402
from apps.certificates.services import certificate_generate_self_signed  # noqa: E402
from apps.domains.services import domain_create  # noqa: E402
from apps.routing.services import header_route_create, rule_create  # noqa: E402
from apps.security.services import address_block, policy_update  # noqa: E402
from apps.security.models import PanelAccessPolicy  # noqa: E402


def build() -> None:
    api = backend_create(name="api-service", lb_method="least_conn", keepalive_connections=64)
    instance_add(backend=api, address="10.20.4.11", port=8080)
    instance_add(backend=api, address="10.20.4.12", port=8080)

    canary = backend_create(name="api-canary")
    instance_add(backend=canary, address="10.20.4.31", port=8080)

    static = backend_create(name="static-assets")
    instance_add(backend=static, address="10.20.4.21", port=8080)

    realtime = backend_create(name="realtime", lb_method="ip_hash")
    instance_add(backend=realtime, address="10.20.4.41", port=8080)

    # Plain HTTP, no certificate.
    plain = domain_create(name="plain.example.com")
    rule_create(domain_id=plain.id, backend_id=api.id, match_value="/")

    # TLS, redirecting.
    secure = domain_create(name="secure.example.com")
    certificate_generate_self_signed(domain=secure)
    secure.refresh_from_db()

    cached = rule_create(
        domain_id=secure.id, backend_id=api.id, match_value="/v1/", priority=10,
        cache_enabled=True, cache_ttl=120, cache_key_headers=["Accept-Encoding"],
        cache_min_uses=2, rate_limit_enabled=True, rate_limit_rps=200,
        strip_prefix=True, custom_headers={"X-Tenant": "bdren"},
    )
    header_route_create(
        rule_id=cached.id, backend_id=canary.id,
        header_value="canary", description="opt-in canary",
    )
    rule_create(
        domain_id=secure.id, backend_id=api.id,
        match_type="exact_path", match_value="/health", priority=5,
    )
    rule_create(
        domain_id=secure.id, backend_id=static.id,
        match_type="regex", match_value=r"\.(js|css|png)$", priority=15,
        cache_enabled=True, cache_ttl=86400, cache_ignore_query_string=True,
    )
    rule_create(
        domain_id=secure.id, backend_id=realtime.id, match_value="/socket/",
        priority=20, proxy_buffering=False, proxy_read_timeout=3600,
    )
    rule_create(domain_id=secure.id, backend_id=api.id, match_value="/", priority=100)

    # TLS without the redirect: routes have to render on both listeners.
    both = domain_create(name="both.example.com")
    certificate_generate_self_signed(domain=both)
    both.refresh_from_db()
    both.force_ssl_redirect = False
    both.save()
    rule_create(domain_id=both.id, backend_id=api.id, match_value="/")

    # The control panel on its own hardened block, with an allowlist and mTLS.
    panel_domain = domain_create(name="control.example.com")
    certificate_generate_self_signed(domain=panel_domain)
    ca = Path(os.environ["NGINX_SSL_DIR"]) / "panel-ca.pem"
    ca.write_text(
        Path(panel_domain.ssl_cert_path).read_text(), encoding="utf-8"
    )
    policy_update(
        policy=PanelAccessPolicy.load(),
        data={
            "panel_domain": "control.example.com",
            "is_published": True,
            "ip_allowlist": ["203.0.113.0/24", "198.51.100.17"],
            "require_mtls": True,
            "mtls_ca_path": str(ca),
        },
    )

    address_block(cidr="192.0.2.0/24", reason="abuse", note="ci fixture")


if __name__ == "__main__":
    build()
    call_command("deploy_config", render_to=sys.argv[1])
    print("rendered", flush=True)
