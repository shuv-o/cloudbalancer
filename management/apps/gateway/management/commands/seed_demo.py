"""
Load a small demo topology so the dashboard has something to show.

Points at the mock backends from the benchmark harness, so `make demo` gives a
working gateway end to end without any real services.
"""
from django.core.management.base import BaseCommand

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.gateway.services import config_deploy
from apps.routing.services import header_route_create, rule_create


class Command(BaseCommand):
    help = "Create demo domains, backends and routing rules."

    def handle(self, *args, **options):
        from apps.backends.models import Backend
        from apps.domains.models import Domain

        if Domain.objects.exists() or Backend.objects.exists():
            self.stdout.write(
                self.style.WARNING("Data already exists; nothing was created.")
            )
            return

        api = backend_create(
            name="api-service",
            description="Mock JSON API, three instances",
            lb_method="least_conn",
            keepalive_connections=64,
        )
        for port in (9101, 9102, 9103):
            instance_add(backend=api, address="127.0.0.1", port=port)

        static = backend_create(
            name="static-assets",
            description="Cacheable static content",
            keepalive_connections=32,
        )
        instance_add(backend=static, address="127.0.0.1", port=9201)

        canary = backend_create(name="api-canary", description="Canary pool")
        instance_add(backend=canary, address="127.0.0.1", port=9301)

        domain = domain_create(
            name="demo.local",
            description="Demo domain for the benchmark harness",
        )

        rule_create(
            domain_id=domain.id,
            backend_id=static.id,
            match_value="/static/",
            priority=10,
            cache_enabled=True,
            cache_ttl=3600,
            cache_key_headers=["Accept-Encoding"],
        )
        api_rule = rule_create(
            domain_id=domain.id,
            backend_id=api.id,
            match_value="/api/",
            priority=20,
            cache_enabled=True,
            cache_ttl=30,
        )
        header_route_create(
            rule_id=api_rule.id,
            backend_id=canary.id,
            header_name="X-Route",
            header_value="canary",
            description="Opt-in canary pool",
        )
        rule_create(
            domain_id=domain.id,
            backend_id=api.id,
            match_value="/",
            priority=100,
        )

        self.stdout.write(self.style.SUCCESS("Demo topology created."))
        log = config_deploy()
        self.stdout.write(f"Deploy: {log.status}")
