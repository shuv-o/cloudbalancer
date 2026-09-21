"""
Tests for the control plane.

The focus is the two things that would be expensive to get wrong: the rendered
Nginx configuration, because a mistake there is a production outage, and the
cache rules, because a mistake there leaks one user's data to another.
"""
import hashlib
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.gateway.services import (
    cache_key_for,
    cache_path_for_key,
    cache_purge_url,
    config_render,
)
from apps.routing.services import header_route_create, rule_create


class ConfigRenderTests(TestCase):
    """What ends up in the files Nginx loads."""

    def setUp(self):
        self.backend = backend_create(
            name="api-service", lb_method="least_conn", keepalive_connections=64
        )
        instance_add(backend=self.backend, address="10.0.1.11", port=8080)
        instance_add(backend=self.backend, address="10.0.1.12", port=8080)
        self.domain = domain_create(name="api.example.com")

    def test_upstream_declares_shared_zone(self):
        """
        Least-connections without a shared zone counts per worker, so each
        worker makes a locally-optimal and globally-wrong choice.
        """
        config = config_render()["10-upstreams.conf"]
        self.assertIn("zone api_service 64k;", config)
        self.assertIn("least_conn;", config)

    def test_upstream_keeps_connections_alive(self):
        config = config_render()["10-upstreams.conf"]
        self.assertIn("keepalive          64;", config)
        self.assertIn("keepalive_requests 1000;", config)

    def test_draining_instance_is_marked_down(self):
        instance = self.backend.instances.first()
        instance.is_draining = True
        instance.save()

        config = config_render()["10-upstreams.conf"]
        self.assertIn(f"server {instance.netloc} weight=1 max_fails=3 fail_timeout=10s down;",
                      config)

    def test_backend_without_instances_is_skipped(self):
        """An upstream with no servers fails nginx -t and takes everything down."""
        empty = backend_create(name="not-deployed-yet")
        config = config_render()["10-upstreams.conf"]
        self.assertNotIn(empty.upstream_name, config)

    def test_rule_pointing_at_empty_backend_is_skipped(self):
        empty = backend_create(name="not-deployed-yet")
        rule_create(domain_id=self.domain.id, backend_id=empty.id, match_value="/orphan/")

        config = config_render()["20-servers.conf"]
        self.assertNotIn("/orphan/", config)

    def test_every_domain_answers_acme_challenges(self):
        """
        Without this, getting a first certificate needs a manual bootstrap:
        the challenge must be answerable before the certificate exists.
        """
        config = config_render()["20-servers.conf"]
        self.assertIn("location ^~ /.well-known/acme-challenge/", config)

    def test_http_only_domain_still_serves_its_routes(self):
        rule_create(domain_id=self.domain.id, backend_id=self.backend.id, match_value="/")
        config = config_render()["20-servers.conf"]
        self.assertIn("proxy_pass http://api_service;", config)

    def test_tls_domain_without_redirect_serves_on_both_ports(self):
        """
        Turning the redirect off must not leave port 80 answering nothing but
        challenges -- that would 404 every plain HTTP request.
        """
        rule_create(domain_id=self.domain.id, backend_id=self.backend.id, match_value="/")
        self.domain.ssl_enabled = True
        self.domain.force_ssl_redirect = False
        self.domain.ssl_cert_path = "/etc/letsencrypt/live/api.example.com/fullchain.pem"
        self.domain.ssl_key_path = "/etc/letsencrypt/live/api.example.com/privkey.pem"
        self.domain.save()

        config = config_render()["20-servers.conf"]
        self.assertEqual(config.count("proxy_pass http://api_service;"), 2)
        self.assertNotIn("return 301", config)

    def test_tls_domain_with_redirect_serves_routes_once(self):
        rule_create(domain_id=self.domain.id, backend_id=self.backend.id, match_value="/")
        self.domain.ssl_enabled = True
        self.domain.force_ssl_redirect = True
        self.domain.ssl_cert_path = "/etc/letsencrypt/live/api.example.com/fullchain.pem"
        self.domain.ssl_key_path = "/etc/letsencrypt/live/api.example.com/privkey.pem"
        self.domain.save()

        config = config_render()["20-servers.conf"]
        self.assertEqual(config.count("proxy_pass http://api_service;"), 1)
        self.assertIn("return 301 https://$host$request_uri;", config)

    def test_ssl_is_not_enabled_without_certificate_paths(self):
        """A 443 listener with no certificate fails nginx -t on every reload."""
        rule_create(domain_id=self.domain.id, backend_id=self.backend.id, match_value="/")
        self.domain.ssl_enabled = True
        self.domain.save()

        config = config_render()["20-servers.conf"]
        self.assertNotIn("listen 443 ssl;", config)


class CacheDirectiveTests(TestCase):
    """The directives that decide what gets stored and served."""

    def setUp(self):
        self.backend = backend_create(name="api-service")
        instance_add(backend=self.backend, address="10.0.1.11", port=8080)
        self.domain = domain_create(name="api.example.com")

    def _render(self, **kwargs):
        rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/v1/",
            cache_enabled=True,
            **kwargs,
        )
        return config_render()["20-servers.conf"]

    def test_vary_is_never_ignored(self):
        """
        Ignoring Vary caches one variant and serves it to everyone, regardless
        of what they asked for. Nginx honours it by default; nothing here may
        turn that off.
        """
        self.assertNotIn("proxy_ignore_headers Vary", self._render())

    def test_expired_entries_are_revalidated(self):
        """Respecting upstream ETag means a 304 instead of a re-transfer."""
        self.assertIn("proxy_cache_revalidate on;", self._render())

    def test_stale_while_revalidate_is_configured(self):
        config = self._render()
        self.assertIn("proxy_cache_use_stale", config)
        self.assertIn("updating", config)
        self.assertIn("proxy_cache_background_update on;", config)
        self.assertIn("proxy_cache_lock              on;", config)

    def test_only_safe_methods_are_cached(self):
        self.assertIn("proxy_cache_methods GET HEAD;", self._render())

    def test_method_is_part_of_the_cache_key(self):
        """Otherwise a bodiless HEAD response can be served to a GET."""
        self.assertIn("$request_method", self._render())

    def test_authenticated_requests_are_both_bypassed_and_not_stored(self):
        """
        Bypass on its own still writes the response into the cache for the next
        anonymous caller. Both directives are needed.
        """
        config = self._render()
        self.assertIn("proxy_cache_bypass $http_authorization", config)
        self.assertIn("proxy_no_cache     $http_authorization", config)

    def test_accept_encoding_is_normalised_in_the_key(self):
        """
        Keying on the raw header splits one object into one entry per client
        spelling of the same three encodings.
        """
        config = self._render(cache_key_headers=["Accept-Encoding"])
        self.assertIn("$encoding_key", config)
        self.assertNotIn("$http_accept_encoding$", config)

    def test_cache_status_header_is_emitted_on_every_route(self):
        rule_create(domain_id=self.domain.id, backend_id=self.backend.id, match_value="/plain/")
        config = self._render()
        self.assertEqual(config.count("add_header X-Cache-Status $cache_status_display always;"), 2)

    def test_streaming_routes_never_cache(self):
        rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/events/",
            proxy_buffering=False,
        )
        config = config_render()["20-servers.conf"]
        self.assertIn("proxy_cache off;", config)
        self.assertIn("proxy_set_header Connection        $connection_upgrade;", config)


class CacheSafetyTests(TestCase):
    """The one configuration that serves one user's data to another."""

    def setUp(self):
        self.backend = backend_create(name="api-service")
        instance_add(backend=self.backend, address="10.0.1.11", port=8080)
        self.domain = domain_create(name="api.example.com")

    def test_caching_authenticated_responses_is_refused_by_default(self):
        with self.assertRaises(ValidationError) as caught:
            rule_create(
                domain_id=self.domain.id,
                backend_id=self.backend.id,
                match_value="/v1/",
                cache_enabled=True,
                cache_bypass_auth=False,
            )
        self.assertIn("cache_bypass_auth", caught.exception.message_dict)

    def test_caching_authenticated_responses_is_allowed_deliberately(self):
        rule = rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/v1/",
            cache_enabled=True,
            cache_bypass_auth=False,
            cache_allow_authenticated=True,
        )
        self.assertFalse(rule.cache_bypass_auth)

    def test_the_guard_does_not_apply_to_uncached_routes(self):
        rule = rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/v1/",
            cache_enabled=False,
            cache_bypass_auth=False,
        )
        self.assertFalse(rule.cache_enabled)


class HeaderRoutingTests(TestCase):
    """Sending one header value somewhere else, without poisoning the cache."""

    def setUp(self):
        self.backend = backend_create(name="api-service")
        instance_add(backend=self.backend, address="10.0.1.11", port=8080)
        self.canary = backend_create(name="api-canary")
        instance_add(backend=self.canary, address="10.0.1.99", port=8080)
        self.domain = domain_create(name="api.example.com")

    def test_map_resolves_to_declared_upstream_names(self):
        """
        Nginx matches a variable proxy_pass against upstream names before it
        considers DNS, which is what preserves the keepalive pool.
        """
        rule = rule_create(
            domain_id=self.domain.id, backend_id=self.backend.id, match_value="/v1/"
        )
        header_route_create(rule_id=rule.id, backend_id=self.canary.id, header_value="canary")

        config = config_render()
        self.assertIn("map $http_x_route $target_", config["00-maps.conf"])
        self.assertIn("default  api_service;", config["00-maps.conf"])
        self.assertIn('"canary"  api_canary;', config["00-maps.conf"])
        self.assertIn("proxy_pass http://$target_", config["20-servers.conf"])

    def test_switching_header_is_added_to_the_cache_key(self):
        """
        Without this the canary's response is cached under the same key as the
        stable one and served to everybody.
        """
        rule = rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/v1/",
            cache_enabled=True,
        )
        header_route_create(rule_id=rule.id, backend_id=self.canary.id, header_value="canary")

        rule.refresh_from_db()
        self.assertIn("X-Route", rule.cache_key_headers)
        self.assertIn("$http_x_route", config_render()["20-servers.conf"])

    def test_override_to_inactive_backend_is_dropped(self):
        rule = rule_create(
            domain_id=self.domain.id, backend_id=self.backend.id, match_value="/v1/"
        )
        header_route_create(rule_id=rule.id, backend_id=self.canary.id, header_value="canary")
        self.canary.is_active = False
        self.canary.save()

        config = config_render()
        self.assertNotIn("api_canary", config["00-maps.conf"])
        self.assertIn("proxy_pass http://api_service;", config["20-servers.conf"])


class RateLimitTests(TestCase):
    def test_zone_is_declared_once_per_limited_route(self):
        backend = backend_create(name="api-service")
        instance_add(backend=backend, address="10.0.1.11", port=8080)
        domain = domain_create(name="api.example.com")
        rule = rule_create(
            domain_id=domain.id,
            backend_id=backend.id,
            match_value="/v1/",
            rate_limit_enabled=True,
            rate_limit_rps=50,
            rate_limit_burst=100,
        )

        config = config_render()
        self.assertIn(f"zone=rl_{rule.slug}:10m rate=50r/s;", config["00-maps.conf"])
        self.assertIn(f"limit_req        zone=rl_{rule.slug} burst=100 nodelay;",
                      config["20-servers.conf"])


class CachePurgeTests(TestCase):
    """
    Purging one URL without the commercial purge module.

    Nginx stores each entry at a path derived from the MD5 of its cache key,
    split by the levels= setting. Deriving that path is the whole mechanism,
    so it is worth a test that fails loudly if the layout assumption breaks.
    """

    def test_path_matches_the_levels_1_2_layout(self):
        key = cache_key_for(scheme="https", host="api.example.com", uri="/v1/things")
        digest = hashlib.md5(key.encode()).hexdigest()
        path = cache_path_for_key(key=key)

        self.assertEqual(path.name, digest)
        self.assertEqual(path.parent.name, digest[-3:-1])
        self.assertEqual(path.parent.parent.name, digest[-1])

    def test_purging_removes_the_entry(self):
        key = cache_key_for(scheme="https", host="api.example.com", uri="/v1/things")
        path = cache_path_for_key(key=key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached response")

        result = cache_purge_url(host="api.example.com", uri="/v1/things")

        self.assertEqual(result.purged, 1)
        self.assertFalse(path.exists())

    def test_purging_something_uncached_is_not_an_error(self):
        result = cache_purge_url(host="api.example.com", uri="/never/requested")
        self.assertEqual(result.purged, 0)
        self.assertEqual(result.errors, [])

    def test_leading_slash_is_added_when_missing(self):
        with_slash = cache_purge_url(host="api.example.com", uri="/x")
        without = cache_purge_url(host="api.example.com", uri="x")
        self.assertEqual(with_slash.purged, without.purged)


class RenderedConfigStructureTests(TestCase):
    """
    Structural checks on the generated files.

    `nginx -t` is the real gate and runs on every deploy, but it only runs
    where Nginx is installed. These catch the mistakes a template is most
    likely to make -- an unbalanced brace, a reference to an upstream that was
    never declared -- in a plain test run.
    """

    def setUp(self):
        api = backend_create(name="api-service", lb_method="least_conn")
        instance_add(backend=api, address="10.0.1.11", port=8080)
        instance_add(backend=api, address="10.0.1.12", port=8080)
        canary = backend_create(name="api-canary")
        instance_add(backend=canary, address="10.0.1.99", port=8080)
        static = backend_create(name="static-assets")
        instance_add(backend=static, address="10.0.2.11", port=8080)

        plain = domain_create(name="plain.example.com")
        secure = domain_create(name="secure.example.com")
        secure.ssl_enabled = True
        secure.ssl_cert_path = "/etc/letsencrypt/live/secure.example.com/fullchain.pem"
        secure.ssl_key_path = "/etc/letsencrypt/live/secure.example.com/privkey.pem"
        secure.save()

        rule = rule_create(
            domain_id=secure.id,
            backend_id=api.id,
            match_value="/v1/",
            priority=10,
            cache_enabled=True,
            cache_key_headers=["Accept-Encoding"],
            rate_limit_enabled=True,
            strip_prefix=True,
            custom_headers={"X-Tenant": "bdren"},
        )
        header_route_create(rule_id=rule.id, backend_id=canary.id, header_value="canary")

        rule_create(
            domain_id=secure.id,
            backend_id=api.id,
            match_type="exact_path",
            match_value="/health",
            priority=5,
        )
        rule_create(
            domain_id=secure.id,
            backend_id=api.id,
            match_value="/events/",
            priority=20,
            proxy_buffering=False,
        )
        rule_create(
            domain_id=plain.id,
            backend_id=static.id,
            match_type="regex",
            match_value=r"\.(js|css)$",
            priority=15,
        )
        rule_create(domain_id=plain.id, backend_id=static.id, match_value="/", priority=100)

        self.config = config_render()

    @staticmethod
    def _strip_comments(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )

    def test_braces_balance_in_every_file(self):
        for name, content in self.config.items():
            with self.subTest(file=name):
                stripped = self._strip_comments(content)
                self.assertEqual(
                    stripped.count("{"), stripped.count("}"),
                    f"{name} has unbalanced braces",
                )

    def test_every_directive_is_terminated(self):
        """A missing semicolon is the classic template slip."""
        for name, content in self.config.items():
            for number, line in enumerate(content.splitlines(), start=1):
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                if text.endswith(("{", "}", ";")):
                    continue
                # Continuation lines inside a multi-value directive are fine.
                if text.endswith(("\\", ",")):
                    continue
                self.fail(f"{name}:{number} is not terminated: {text!r}")

    def test_every_proxy_pass_target_exists(self):
        """
        A proxy_pass to an upstream that was never declared fails nginx -t and
        rolls the whole deploy back.
        """
        import re

        declared = set(re.findall(r"^upstream (\S+) \{", self.config["10-upstreams.conf"],
                                  re.MULTILINE))
        mapped = set(re.findall(r"\$(target_\S+) \{", self.config["00-maps.conf"]))

        for target in re.findall(r"proxy_pass http://(\S+);", self.config["20-servers.conf"]):
            with self.subTest(target=target):
                if target.startswith("$"):
                    self.assertIn(target[1:], mapped)
                else:
                    self.assertIn(target, declared)

    def test_every_map_value_is_a_declared_upstream(self):
        import re

        declared = set(re.findall(r"^upstream (\S+) \{", self.config["10-upstreams.conf"],
                                  re.MULTILINE))
        body = self.config["00-maps.conf"]
        for block in re.findall(r"map \$http_\S+ \$target_\S+ \{(.*?)\n\}", body, re.DOTALL):
            for value in re.findall(r"^\s+(?:default|\"[^\"]*\")\s+(\S+);", block, re.MULTILINE):
                with self.subTest(value=value):
                    self.assertIn(value, declared)

    def test_every_rate_limit_zone_used_is_declared(self):
        import re

        declared = set(re.findall(r"zone=(\S+):\d+m", self.config["00-maps.conf"]))
        used = set(re.findall(r"limit_req\s+zone=(\S+) ", self.config["20-servers.conf"]))
        self.assertTrue(used)
        self.assertTrue(used <= declared, f"undeclared zones: {used - declared}")

    def test_regex_location_renders_with_its_operator(self):
        self.assertIn(r"location ~ \.(js|css)$ {", self.config["20-servers.conf"])

    def test_custom_headers_are_forwarded(self):
        self.assertIn('proxy_set_header X-Tenant "bdren";', self.config["20-servers.conf"])

    def test_prefix_strip_rewrites_before_proxying(self):
        servers = self.config["20-servers.conf"]
        rewrite_at = servers.index("rewrite ^/v1/")
        proxy_at = servers.index("proxy_pass http://$target_", rewrite_at)
        self.assertLess(rewrite_at, proxy_at)


class StreamingRouteTests(TestCase):
    def setUp(self):
        backend = backend_create(name="events-service")
        instance_add(backend=backend, address="10.0.1.11", port=8080)
        domain = domain_create(name="api.example.com")
        rule_create(
            domain_id=domain.id,
            backend_id=backend.id,
            match_value="/events/",
            proxy_buffering=False,
        )
        self.config = config_render()["20-servers.conf"]

    def test_connection_header_is_set_exactly_once(self):
        """
        Nginx adds every proxy_set_header in a context rather than replacing,
        so setting Connection twice sends it twice and the WebSocket upgrade
        handshake fails.
        """
        self.assertEqual(self.config.count("proxy_set_header Connection"), 1)

    def test_upgrade_token_is_forwarded(self):
        self.assertIn("proxy_set_header Connection        $connection_upgrade;", self.config)
        self.assertIn("proxy_set_header Upgrade           $http_upgrade;", self.config)

    def test_buffered_route_clears_connection(self):
        backend = backend_create(name="plain-service")
        instance_add(backend=backend, address="10.0.2.11", port=8080)
        domain = domain_create(name="plain.example.com")
        rule_create(domain_id=domain.id, backend_id=backend.id, match_value="/")

        config = config_render()["20-servers.conf"]
        self.assertIn('proxy_set_header Connection        "";', config)


class WebSocketTests(TestCase):
    """
    Upgraded connections.

    WebSocket through Nginx has a well-known way of appearing to work and then
    not: the handshake succeeds, messages flow, and then the socket dies the
    first time it is quiet for longer than the read timeout. These cover the
    handshake and that timeout together, because the first without the second
    is the bug.
    """

    def setUp(self):
        backend = backend_create(name="realtime")
        instance_add(backend=backend, address="10.0.1.11", port=8080)
        self.domain = domain_create(name="ws.example.com")
        self.backend = backend

    def _render(self, **kwargs):
        rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/socket/",
            proxy_buffering=False,
            **kwargs,
        )
        return config_render()

    def test_the_upgrade_handshake_is_forwarded(self):
        config = self._render()["20-servers.conf"]

        self.assertIn("proxy_http_version 1.1;", config)
        self.assertIn("proxy_set_header Upgrade           $http_upgrade;", config)
        self.assertIn("proxy_set_header Connection        $connection_upgrade;", config)

    def test_the_connection_variable_is_defined(self):
        """
        Without the map, $connection_upgrade is empty and the upgrade header
        never reaches the backend -- the handshake just fails.
        """
        maps = self._render()["00-maps.conf"]

        self.assertIn("map $http_upgrade $connection_upgrade {", maps)
        self.assertIn("default  upgrade;", maps)
        # A plain request must not be told to upgrade.
        self.assertIn('""       "";', maps)

    def test_connection_is_set_exactly_once(self):
        """Nginx adds headers rather than replacing them; two would break it."""
        config = self._render()["20-servers.conf"]
        self.assertEqual(config.count("proxy_set_header Connection"), 1)

    def test_both_idle_directions_are_bounded(self):
        """
        An upgraded connection that is quiet inbound but not outbound would
        still be closed if only one direction carried a timeout.
        """
        config = self._render(proxy_read_timeout=3600)["20-servers.conf"]

        self.assertIn("proxy_read_timeout 3600s;", config)
        self.assertIn("proxy_send_timeout 3600s;", config)

    def test_a_request_response_route_does_not_get_a_send_timeout(self):
        """It would mean something different there, and the http default applies."""
        rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/api/",
        )
        config = config_render()["20-servers.conf"]

        api_block = config[config.index("location /api/"):]
        self.assertNotIn("proxy_send_timeout", api_block)

    def test_buffering_and_caching_are_both_off(self):
        """
        Buffering would hold messages back until a buffer filled, and nothing
        arriving in pieces over a held connection can be cached anyway.
        """
        config = self._render()["20-servers.conf"]

        self.assertIn("proxy_buffering off;", config)
        self.assertIn("proxy_cache off;", config)
        self.assertIn("chunked_transfer_encoding off;", config)

    def test_a_streaming_route_still_gets_per_address_limits(self):
        """
        The handshake is an ordinary request, so it is rate limited like any
        other. Only the upgraded connection escapes, which is correct.
        """
        config = self._render()["20-servers.conf"]

        self.assertIn("limit_conn per_ip_conn", config)
        self.assertIn("limit_req  zone=per_ip_req", config)
