"""
Operator input must not be able to write its own Nginx directives.

The control plane renders a config file from rows an operator controls. A value
that closes its own quote can open a `location` block of its own, and `nginx -t`
passes it, because the result is valid configuration. The deploy check is no
defence here: it verifies syntax, not authorship.

Two layers are tested. Values that become quoted strings or comments are
escaped at render time; values that become bare directives -- a server_name, a
location, an upstream name -- are constrained at save time, because nothing can
quote them.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.gateway.nginx_values import UnsafeNginxValue, comment, quoted
from apps.gateway.services import config_render
from apps.routing.services import header_route_create, rule_create

BREAKOUT = 'x"; } location /INJECTED { return 200 "pwned"; } location /z { set $a "'


def outside_quoted_strings(config: str) -> str:
    """
    The config with every double-quoted string removed.

    Escaping works by keeping a payload *inside* a quoted string, so the
    payload's own text still appears in the rendered file -- harmlessly. What
    must never happen is that it appears as configuration. Stripping the quoted
    strings leaves exactly the part Nginx would read as directives.
    """
    out, i, in_string = [], 0, False
    while i < len(config):
        char = config[i]
        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def has_unescaped_quote(rendered: str) -> bool:
    """Does a rendered quoted string contain a quote that ends it early?"""
    body, i = rendered[1:-1], 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == '"':
            return True
        i += 1
    return False


class EscapingTests(TestCase):
    """The filters themselves."""

    def test_quote_and_backslash_are_escaped(self):
        self.assertEqual(quoted('a"b'), '"a\\"b"')
        self.assertEqual(quoted("a\\b"), '"a\\\\b"')

    def test_a_breakout_attempt_stays_one_string(self):
        rendered = quoted(BREAKOUT)
        self.assertTrue(rendered.startswith('"'))
        self.assertTrue(rendered.endswith('"'))
        self.assertFalse(has_unescaped_quote(rendered))

    def test_control_characters_are_refused(self):
        with self.assertRaises(UnsafeNginxValue):
            quoted("line one\nline two")

    def test_comments_cannot_reach_the_next_line(self):
        self.assertEqual(comment("note\nreturn 200;"), "note return 200;")
        self.assertNotIn("\n", comment("a\nb\nc"))


class CustomHeaderInjectionTests(TestCase):
    """The route that proved this was reachable in production."""

    def setUp(self):
        self.backend = backend_create(name="svc")
        instance_add(backend=self.backend, address="10.0.1.1", port=8080)
        self.domain = domain_create(name="probe.example.com")

    def test_header_value_cannot_open_a_location_block(self):
        rule_create(
            domain_id=self.domain.id,
            backend_id=self.backend.id,
            match_value="/",
            custom_headers={"X-Probe": BREAKOUT},
        )
        config = config_render()["20-servers.conf"]
        # The payload is still in the file -- inside a string, where it is
        # text. What matters is that none of it is read as configuration.
        self.assertIn("X-Probe", config)
        self.assertNotIn("INJECTED", outside_quoted_strings(config))

    def test_a_newline_in_a_header_value_is_refused_at_save(self):
        """
        Not at render: the escaper raises, and a value that reached it would
        fail the deploy of every domain rather than just this route.
        """
        with self.assertRaises(ValidationError):
            rule_create(
                domain_id=self.domain.id,
                backend_id=self.backend.id,
                match_value="/",
                custom_headers={"X-Probe": "a\nproxy_pass http://evil;"},
            )

    def test_header_names_are_constrained(self):
        with self.assertRaises(ValidationError):
            rule_create(
                domain_id=self.domain.id,
                backend_id=self.backend.id,
                match_value="/",
                custom_headers={"X Probe; return 200": "v"},
            )


class HeaderRouteInjectionTests(TestCase):
    def setUp(self):
        self.backend = backend_create(name="svc")
        instance_add(backend=self.backend, address="10.0.1.1", port=8080)
        self.other = backend_create(name="other")
        instance_add(backend=self.other, address="10.0.1.2", port=8080)
        self.domain = domain_create(name="probe.example.com")
        self.rule = rule_create(
            domain_id=self.domain.id, backend_id=self.backend.id, match_value="/"
        )

    def test_map_key_cannot_escape_its_quotes(self):
        header_route_create(
            rule_id=self.rule.id,
            backend_id=self.other.id,
            header_name="X-Route",
            header_value=BREAKOUT,
        )
        config = config_render()["00-maps.conf"]
        self.assertNotIn("INJECTED", outside_quoted_strings(config))


class BareDirectiveTests(TestCase):
    """Values with nowhere to hide: nothing here can be quoted."""

    def test_a_hostname_must_be_a_hostname(self):
        for bad in ["https://api.example.com", "api example.com", "api..example.com"]:
            with self.subTest(name=bad), self.assertRaises(ValidationError):
                domain_create(name=bad)

    def test_case_and_padding_are_normalised_rather_than_refused(self):
        """Two spellings of one name would otherwise become two domains."""
        domain = domain_create(name="  API.Example.COM  ")
        self.assertEqual(domain.name, "api.example.com")

    def test_a_hostname_with_a_directive_in_it_is_refused(self):
        with self.assertRaises(ValidationError):
            domain_create(name="evil.com; return 200 'x'")

    def test_wildcards_are_still_allowed(self):
        domain = domain_create(name="*.example.com")
        self.assertEqual(domain.name, "*.example.com")

    def test_a_backend_name_must_be_a_usable_symbol(self):
        with self.assertRaises(ValidationError):
            backend_create(name="svc; } upstream evil {")

    def test_a_path_must_start_with_a_slash(self):
        backend = backend_create(name="svc")
        instance_add(backend=backend, address="10.0.1.1", port=8080)
        domain = domain_create(name="paths.example.com")
        with self.assertRaises(ValidationError):
            rule_create(
                domain_id=domain.id, backend_id=backend.id, match_value="api/v1"
            )

    def test_a_path_cannot_end_the_directive(self):
        backend = backend_create(name="svc")
        instance_add(backend=backend, address="10.0.1.1", port=8080)
        domain = domain_create(name="paths.example.com")
        with self.assertRaises(ValidationError):
            rule_create(
                domain_id=domain.id,
                backend_id=backend.id,
                match_value="/a; } location /b {",
            )

    def test_a_regex_may_still_use_braces(self):
        backend = backend_create(name="svc")
        instance_add(backend=backend, address="10.0.1.1", port=8080)
        domain = domain_create(name="rx.example.com")
        rule = rule_create(
            domain_id=domain.id,
            backend_id=backend.id,
            match_type="regex",
            match_value=r"^/v\d{1,2}/",
        )
        self.assertIn("{1,2}", rule.match_value)
