"""
Caching static assets by what the backend actually returned.

The decision is made from the response's content type, which is only known
after the response exists. That single fact shapes the whole feature, and the
tests below exist mostly to pin the consequences of it.
"""
from django.test import TestCase

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.gateway.services import config_render
from apps.routing.models import RoutingRule
from apps.routing.services import rule_create


class StaticCacheRenderTests(TestCase):
    def setUp(self):
        self.backend = backend_create(name="appserver")
        instance_add(backend=self.backend, address="10.0.0.1", port=8080)
        self.domain = domain_create(name="app.example.com")

    @staticmethod
    def _directives(config: str, name: str) -> list[str]:
        """The actual directive lines, not the comments that mention them."""
        return [
            line.strip()
            for line in config.splitlines()
            if line.strip().startswith(name)
        ]

    def _render(self, **kwargs) -> str:
        rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            cache_enabled=True,
            **kwargs,
        )
        return config_render()["20-servers.conf"]

    def test_static_mode_refuses_to_store_anything_else(self):
        config = self._render(cache_mode=RoutingRule.CacheMode.STATIC)
        self.assertIn("proxy_no_cache", config)
        self.assertIn("$deny_dynamic_cache", config)

    def test_the_content_type_map_never_reaches_proxy_cache_bypass(self):
        """
        The trap this feature is built around.

        proxy_cache_bypass is evaluated before the request is sent, when
        $upstream_http_content_type is still empty. The map reads empty as "not
        static", so putting it here would bypass every lookup and the cache
        would never be read from -- a cache that stores perfectly and serves
        nothing, with no error anywhere to say so.
        """
        config = self._render(cache_mode=RoutingRule.CacheMode.STATIC)

        for line in self._directives(config, "proxy_cache_bypass"):
            self.assertNotIn("$deny_dynamic_cache", line)
            self.assertNotIn("$response_is_static", line)

    def test_everything_mode_does_not_filter_by_content_type(self):
        config = self._render(cache_mode=RoutingRule.CacheMode.EVERYTHING)
        self.assertNotIn("$deny_dynamic_cache", config)

    def test_one_proxy_no_cache_directive_not_two(self):
        """
        Nginx keeps only the last proxy_no_cache in a context. Two would mean
        the static filter silently replaced the authenticated-request filter,
        or the other way round -- and the config would still pass nginx -t.
        """
        config = self._render(
            cache_mode=RoutingRule.CacheMode.STATIC, cache_bypass_auth=True
        )
        self.assertEqual(len(self._directives(config, "proxy_no_cache")), 1)

    def test_both_filters_end_up_in_that_one_directive(self):
        config = self._render(
            cache_mode=RoutingRule.CacheMode.STATIC, cache_bypass_auth=True
        )
        line = self._directives(config, "proxy_no_cache")[0]
        self.assertIn("$deny_dynamic_cache", line)
        self.assertIn("$http_authorization", line)

    def test_a_streaming_route_still_caches_nothing(self):
        config = self._render(
            cache_mode=RoutingRule.CacheMode.STATIC, proxy_buffering=False
        )
        self.assertIn("proxy_cache off;", config)
        self.assertNotIn("$deny_dynamic_cache", config)


class StaticCacheMapTests(TestCase):
    def setUp(self):
        self.config = config_render()["00-maps.conf"]

    def test_the_usual_asset_types_are_static(self):
        for pattern in ["^image/", "^font/", "^text/css", "^application/javascript"]:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, self.config)

    def test_html_and_json_are_not_named(self):
        """They fall to `default 0`, which is what keeps them out of the cache."""
        self.assertNotIn("text/html", self.config)
        self.assertNotIn("application/json", self.config)

    def test_static_maps_to_empty_because_that_is_what_lets_it_cache(self):
        """
        proxy_no_cache stores when every parameter is empty or "0". Mapping
        static to "0" would work too; mapping it to anything else would mean
        static assets were the only thing never cached.
        """
        block = self.config.split("map $response_is_static $deny_dynamic_cache {")[1]
        block = block.split("}")[0]
        self.assertRegex(block, r'1\s+"";')
        self.assertRegex(block, r'default\s+"1";')
