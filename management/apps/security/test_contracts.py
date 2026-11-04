"""
Contract tests between input serializers and the services they feed.

These exist because the same mistake shipped four times: a field was added to
a model and to its serializer, and the service that receives the serializer's
output was never updated. Every time, the API accepted the request, validated
it, and then raised TypeError -- a 500 on a perfectly valid form submission,
visible only by submitting that form.

Nothing here tests behaviour. They test that the pieces still fit, which is
the part that kept coming apart.
"""
import inspect

from django.test import TestCase
from rest_framework import serializers as drf

from apps.backends import serializers as backend_serializers, services as backend_services
from apps.certificates import serializers as cert_serializers, services as cert_services
from apps.domains import serializers as domain_serializers, services as domain_services
from apps.routing import serializers as routing_serializers, services as routing_services
from apps.security import serializers as security_serializers, services as security_services

# serializer -> the service its validated_data is splatted into, plus any
# argument the view supplies itself rather than the serializer.
PAIRS = [
    (
        backend_serializers.BackendInputSerializer,
        backend_services.backend_create,
        set(),
    ),
    (
        backend_serializers.BackendInstanceInputSerializer,
        backend_services.instance_add,
        {"backend"},
    ),
    (
        domain_serializers.DomainInputSerializer,
        domain_services.domain_create,
        set(),
    ),
    (
        routing_serializers.RoutingRuleInputSerializer,
        routing_services.rule_create,
        set(),
    ),
    (
        routing_serializers.HeaderRouteInputSerializer,
        routing_services.header_route_create,
        {"rule_id"},
    ),
    (
        cert_serializers.AcmeAccountInputSerializer,
        cert_services.acme_account_create,
        set(),
    ),
    (
        security_serializers.BlockedAddressInputSerializer,
        security_services.address_block,
        set(),
    ),
]


class SerializerServiceContractTests(TestCase):
    def test_every_service_accepts_every_field_its_serializer_produces(self):
        """
        A view that does `service(**serializer.validated_data)` raises
        TypeError the moment the serializer grows a field the service does
        not name. The request is already validated by then, so it surfaces as
        a 500 rather than a 400.
        """
        failures = []
        for serializer_cls, service, supplied_by_view in PAIRS:
            produced = set(serializer_cls().get_fields())
            accepted = set(inspect.signature(service).parameters) | supplied_by_view
            missing = produced - accepted
            if missing:
                failures.append(
                    f"{service.__name__}() cannot accept "
                    f"{', '.join(sorted(missing))} from {serializer_cls.__name__}"
                )

        self.assertEqual(failures, [], "\n  " + "\n  ".join(failures))

    def test_every_service_argument_has_a_default_or_comes_from_the_view(self):
        """
        A serializer field that is not required may be absent from
        validated_data, so the service argument behind it needs a default or
        the call fails on a request that simply omitted it.
        """
        failures = []
        for serializer_cls, service, supplied_by_view in PAIRS:
            fields = serializer_cls().get_fields()
            params = inspect.signature(service).parameters

            for name, param in params.items():
                if name in supplied_by_view or param.default is not inspect.Parameter.empty:
                    continue
                field = fields.get(name)
                if field is None:
                    failures.append(
                        f"{service.__name__}() requires '{name}', which no "
                        f"field in {serializer_cls.__name__} supplies"
                    )
                elif not field.required and field.default is drf.empty:
                    failures.append(
                        f"{service.__name__}() requires '{name}' but "
                        f"{serializer_cls.__name__}.{name} is optional with no default"
                    )

        self.assertEqual(failures, [], "\n  " + "\n  ".join(failures))


class OptionalTextFieldTests(TestCase):
    """
    An optional text field has to accept the empty string.

    A browser form sends `""` for a field the operator left alone, not a
    missing key. A CharField rejects that unless told otherwise, so "I did not
    write a description" came back as a validation error.

    Fields where blank is genuinely meaningless are listed as exceptions, so
    the rule stays strict everywhere else.
    """

    # Blank is invalid for these: a route needs a path, an override needs a
    # header name. The panel marks both required, so it never sends blank.
    BLANK_IS_INVALID = {
        ("RoutingRuleInputSerializer", "match_value"),
        ("HeaderRouteInputSerializer", "header_name"),
    }

    def test_optional_text_fields_accept_an_empty_string(self):
        failures = []
        for serializer_cls, _service, _supplied in PAIRS:
            name = serializer_cls.__name__
            for field_name, field in serializer_cls().get_fields().items():
                if not isinstance(field, drf.CharField):
                    continue
                if (name, field_name) in self.BLANK_IS_INVALID:
                    continue
                optional = (not field.required) or field.default is not drf.empty
                if optional and not field.allow_blank:
                    failures.append(
                        f"{name}.{field_name} is optional but rejects \"\", "
                        f"which is what a form sends for an untouched field"
                    )

        self.assertEqual(failures, [], "\n  " + "\n  ".join(failures))


class RuntimeDependencyTests(TestCase):
    """
    Imports that only happen in production.

    Tests run Celery eagerly, so nothing ever loads the broker transport --
    and kombu's Redis transport imports `packaging` without declaring it as a
    dependency. A clean install therefore produced a Celery that could not
    queue anything, and the first symptom was a 500 on the first write an
    operator attempted.

    Importing the module here costs nothing and closes that gap.
    """

    def test_the_broker_transport_imports(self):
        """Every write in the panel queues a task, which needs this module."""
        import kombu.transport.redis  # noqa: F401

    def test_the_scheduler_imports(self):
        import django_celery_beat.schedulers  # noqa: F401

    def test_the_cache_backend_imports(self):
        import django_redis.cache  # noqa: F401

    def test_queueing_a_task_does_not_raise(self):
        """
        Exercises the path a write actually takes. Eager mode still builds the
        message, so a transport that cannot be imported fails here.
        """
        from apps.gateway.tasks import request_config_deploy

        request_config_deploy()


class AllowedHostsCoverageTests(TestCase):
    """
    Every Host header something inside the stack actually sends.

    These are not guesses: each one appeared in a production log as a
    DisallowedHost traceback. The container health check presents
    127.0.0.1:8000 and Prometheus presents django:8000, and neither is
    configurable by whoever set DJANGO_ALLOWED_HOSTS to their public address.
    """

    def test_internal_callers_are_allowed(self):
        import os

        from config.settings import base

        original = os.environ.get("DJANGO_ALLOWED_HOSTS")
        os.environ["DJANGO_ALLOWED_HOSTS"] = "203.0.113.9"
        try:
            hosts = base._allowed_hosts()
        finally:
            if original is None:
                os.environ.pop("DJANGO_ALLOWED_HOSTS", None)
            else:
                os.environ["DJANGO_ALLOWED_HOSTS"] = original

        self.assertIn("203.0.113.9", hosts, "the configured host must survive")
        for caller, why in (
            ("127.0.0.1", "the container health check"),
            ("localhost", "local tooling"),
            ("django", "Prometheus scraping /metrics"),
        ):
            self.assertIn(caller, hosts, f"{caller} is used by {why}")
