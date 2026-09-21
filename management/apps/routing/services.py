"""
Routing services — write operations.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.routing.models import ConfigDeployLog, HeaderRoute, RoutingRule
from common.services import model_update

# Headers a route switches on must appear in the cache key, or the first
# response cached for one variant is served to every other variant.
_ALWAYS_KEYED_HEADERS = {"accept-encoding"}


def _guard_cache_safety(*, rule_data: dict) -> None:
    """
    Refuse the one cache configuration that leaks data between users.

    Caching a route that carries an Authorization header or a session cookie,
    without bypassing the cache for those requests, means the first user's
    response is served to the second. It is a legitimate thing to want for a
    signed-but-public API, so it is allowed — but only deliberately.
    """
    if not rule_data.get("cache_enabled"):
        return
    if rule_data.get("cache_bypass_auth", True):
        return
    if rule_data.get("cache_allow_authenticated"):
        return

    raise ValidationError({
        "cache_bypass_auth": (
            "Caching authenticated responses would serve one user's data to another. "
            "Turn on 'Cache authenticated responses' as well if that is really intended."
        )
    })


def _normalise_cache_key_headers(headers: list[str] | None) -> list[str]:
    """Deduplicate, title-case, and guarantee the headers Vary always needs."""
    supplied = [h.strip() for h in (headers or []) if h.strip()]
    seen, result = set(), []
    for header in supplied:
        key = header.lower()
        if key not in seen:
            seen.add(key)
            result.append(header.title())
    for required in _ALWAYS_KEYED_HEADERS:
        if required not in seen:
            result.append(required.title())
    return result


def rule_create(
    *,
    domain_id: int,
    backend_id: int,
    match_type: str = RoutingRule.MatchType.PATH_PREFIX,
    match_value: str = "/",
    priority: int = 100,
    cache_enabled: bool = False,
    cache_ttl: int = 600,
    cache_bypass_auth: bool = True,
    cache_min_uses: int = 1,
    cache_key_headers: list[str] | None = None,
    cache_ignore_upstream_control: bool = False,
    cache_allow_authenticated: bool = False,
    strip_prefix: bool = False,
    custom_headers: dict | None = None,
    proxy_buffering: bool = True,
    proxy_read_timeout: int = 60,
    rate_limit_enabled: bool = False,
    rate_limit_rps: int = 100,
    rate_limit_burst: int = 200,
    is_active: bool = True,
) -> RoutingRule:
    """Create a new routing rule."""
    _guard_cache_safety(rule_data={
        "cache_enabled": cache_enabled,
        "cache_bypass_auth": cache_bypass_auth,
        "cache_allow_authenticated": cache_allow_authenticated,
    })

    rule = RoutingRule(
        domain_id=domain_id,
        backend_id=backend_id,
        match_type=match_type,
        match_value=match_value.strip(),
        priority=priority,
        cache_enabled=cache_enabled,
        cache_ttl=cache_ttl,
        cache_bypass_auth=cache_bypass_auth,
        cache_min_uses=cache_min_uses,
        cache_key_headers=_normalise_cache_key_headers(cache_key_headers) if cache_enabled else [],
        cache_ignore_upstream_control=cache_ignore_upstream_control,
        cache_allow_authenticated=cache_allow_authenticated,
        strip_prefix=strip_prefix,
        custom_headers=custom_headers or {},
        proxy_buffering=proxy_buffering,
        proxy_read_timeout=proxy_read_timeout,
        rate_limit_enabled=rate_limit_enabled,
        rate_limit_rps=rate_limit_rps,
        rate_limit_burst=rate_limit_burst,
        is_active=is_active,
    )
    rule.full_clean()
    rule.save()
    return rule


def rule_update(*, rule: RoutingRule, data: dict) -> RoutingRule:
    """Update a routing rule."""
    merged = {
        "cache_enabled": data.get("cache_enabled", rule.cache_enabled),
        "cache_bypass_auth": data.get("cache_bypass_auth", rule.cache_bypass_auth),
        "cache_allow_authenticated": data.get(
            "cache_allow_authenticated", rule.cache_allow_authenticated
        ),
    }
    _guard_cache_safety(rule_data=merged)

    if "cache_key_headers" in data:
        data = dict(data)
        data["cache_key_headers"] = _normalise_cache_key_headers(data["cache_key_headers"])

    updatable_fields = [
        "domain_id",
        "backend_id",
        "match_type",
        "match_value",
        "priority",
        "cache_enabled",
        "cache_ttl",
        "cache_bypass_auth",
        "cache_min_uses",
        "cache_key_headers",
        "cache_ignore_upstream_control",
        "cache_allow_authenticated",
        "strip_prefix",
        "custom_headers",
        "proxy_buffering",
        "proxy_read_timeout",
        "rate_limit_enabled",
        "rate_limit_rps",
        "rate_limit_burst",
        "is_active",
    ]
    instance, _ = model_update(instance=rule, fields=updatable_fields, data=data)
    return instance


@transaction.atomic
def rule_delete(*, rule: RoutingRule) -> None:
    """Delete a routing rule and its header overrides."""
    rule.delete()


# ---------------------------------------------------------------------------
# Header routes
# ---------------------------------------------------------------------------

def header_route_create(
    *,
    rule_id: int,
    backend_id: int,
    header_name: str = "X-Route",
    header_value: str,
    description: str = "",
    is_active: bool = True,
) -> HeaderRoute:
    """
    Send one header value to a different backend than the rule's default.

    If the rule caches, the header is folded into the cache key automatically —
    otherwise the canary's response would be served to everyone.
    """
    header_route = HeaderRoute(
        rule_id=rule_id,
        backend_id=backend_id,
        header_name=header_name.strip(),
        header_value=header_value.strip(),
        description=description,
        is_active=is_active,
    )
    header_route.full_clean()

    with transaction.atomic():
        header_route.save()
        rule = header_route.rule
        if rule.cache_enabled:
            rule.cache_key_headers = _normalise_cache_key_headers(
                list(rule.cache_key_headers) + [header_route.header_name]
            )
            rule.save(update_fields=["cache_key_headers", "updated_at"])

    return header_route


def header_route_update(*, header_route: HeaderRoute, data: dict) -> HeaderRoute:
    fields = ["backend_id", "header_name", "header_value", "description", "is_active"]
    instance, _ = model_update(instance=header_route, fields=fields, data=data)
    return instance


def header_route_delete(*, header_route: HeaderRoute) -> None:
    header_route.delete()


# ---------------------------------------------------------------------------
# Deploy log
# ---------------------------------------------------------------------------

def deploy_log_create(
    *,
    config_snapshot: str,
    deployed_by=None,
) -> ConfigDeployLog:
    """Create a new config deploy log entry."""
    log = ConfigDeployLog(
        config_snapshot=config_snapshot,
        deployed_by=deployed_by,
    )
    log.save()
    return log
