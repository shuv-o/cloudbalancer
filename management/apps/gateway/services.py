"""
Gateway services — render the Nginx config from the database, test it, and
reload.

The whole control plane exists to produce three files. Django is never in the
request path; it writes configuration and sends a signal, and Nginx does the
rest. That separation is what keeps the gateway's added latency a function of
Nginx alone.

Deploy is a five-step pipeline with a rollback at each failure point:

    render -> back up -> write -> nginx -t -> reload

A configuration that fails `nginx -t` is restored from the backup before Nginx
ever sees it, so a bad rule in the control panel cannot take the gateway down.
"""
import hashlib
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from jinja2 import Environment, FileSystemLoader

from apps.backends.models import Backend
from apps.domains.models import Domain
from apps.routing.models import ConfigDeployLog, RoutingRule
from common.types import CachePurgeResult, NginxTestResult

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "nginx"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)

# Files this system owns. Anything else in conf.d is left alone, so an
# operator can drop in a hand-written block without it being deleted.
MANAGED_FILES = (
    "00-maps.conf",
    "05-panel.conf",
    "10-upstreams.conf",
    "20-servers.conf",
)


def _conf_dir() -> Path:
    return Path(settings.NGINX_CONF_DIR)


def _backup_dir() -> Path:
    backup_dir = _conf_dir() / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


# ---------------------------------------------------------------------------
# Cache key construction
# ---------------------------------------------------------------------------

def _cache_key_suffix(headers: list[str]) -> str:
    """
    Turn the route's keyed headers into the tail of its proxy_cache_key.

    Accept-Encoding goes through the normalising map rather than in raw: the
    same three encodings arrive spelled dozens of ways, and keying on the raw
    string splits one cached object into one entry per client.
    """
    parts = []
    for header in headers or []:
        normalised = header.strip().lower()
        if not normalised:
            continue
        if normalised == "accept-encoding":
            parts.append("$encoding_key")
        else:
            parts.append("$http_" + normalised.replace("-", "_"))
    return "".join(parts)


def cache_key_for(
    *,
    scheme: str,
    host: str,
    uri: str,
    method: str = "GET",
    suffix: str = "",
) -> str:
    """Rebuild the exact string Nginx hashes, so we can find a cached entry."""
    return f"{scheme}{host}{method}{uri}{suffix}"


def cache_path_for_key(*, key: str) -> Path:
    """
    Locate a cached entry on disk.

    Nginx stores each entry at a path derived from the MD5 of its cache key,
    split by the `levels=1:2` setting in nginx.conf. Deriving the path is how
    this gateway purges a single URL without the commercial purge module or a
    custom Nginx build.
    """
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return Path(settings.NGINX_CACHE_DIR) / digest[-1] / digest[-3:-1] / digest


# ---------------------------------------------------------------------------
# Template context
# ---------------------------------------------------------------------------

def _rule_context(rule: RoutingRule) -> dict:
    header_routes = [
        hr for hr in rule.header_routes.all()
        if hr.is_active and hr.backend.is_active
    ]
    return {
        "slug": rule.slug,
        "nginx_location": rule.nginx_location,
        "match_type": rule.match_type,
        "match_value": rule.match_value,
        "priority": rule.priority,
        "backend_name": rule.backend.name,
        "upstream_name": rule.backend.upstream_name,
        "cache_enabled": rule.cache_enabled,
        "cache_ttl": rule.cache_ttl,
        "cache_min_uses": rule.cache_min_uses,
        "cache_ignore_query_string": rule.cache_ignore_query_string,
        "cache_bypass_auth": rule.cache_bypass_auth,
        "cache_ignore_upstream_control": rule.cache_ignore_upstream_control,
        "cache_key_suffix": _cache_key_suffix(rule.cache_key_headers),
        "strip_prefix": rule.strip_prefix,
        "custom_headers": rule.custom_headers or {},
        "proxy_buffering": rule.proxy_buffering,
        "proxy_read_timeout": rule.proxy_read_timeout,
        "rate_limit_enabled": rule.rate_limit_enabled,
        "rate_limit_rps": rule.rate_limit_rps,
        "rate_limit_burst": rule.rate_limit_burst,
        "header_routes": [
            {
                "header_value": hr.header_value,
                "upstream_name": hr.backend.upstream_name,
                "description": hr.description,
            }
            for hr in header_routes
        ],
        "header_name": header_routes[0].header_name if header_routes else "",
        "header_variable": header_routes[0].nginx_variable if header_routes else "",
    }


def _active_rules_by_domain() -> dict[int, list[dict]]:
    """
    Every deployable rule, grouped by domain.

    A rule is deployable when it, its domain, and its backend are all active,
    and the backend has at least one instance to send traffic to. A rule
    pointing at an empty upstream would fail `nginx -t`, so it is dropped here
    rather than breaking the whole deploy.
    """
    backends_with_instances = set(
        Backend.objects
        .filter(is_active=True, instances__is_active=True)
        .values_list("id", flat=True)
    )

    grouped: dict[int, list[dict]] = {}
    rules = (
        RoutingRule.objects
        .filter(is_active=True, backend__is_active=True, domain__is_active=True)
        .select_related("domain", "backend")
        .prefetch_related("header_routes__backend")
        .order_by("domain_id", "priority", "match_value")
    )
    for rule in rules:
        if rule.backend_id not in backends_with_instances:
            logger.warning(
                "Skipping rule %s: backend '%s' has no active instances",
                rule, rule.backend.name,
            )
            continue
        grouped.setdefault(rule.domain_id, []).append(_rule_context(rule))
    return grouped


def _protection_context() -> dict:
    """Per-address limits, as the templates want them."""
    from apps.security.models import TrafficProtectionPolicy

    policy = TrafficProtectionPolicy.load()
    return {
        "enabled": policy.enabled,
        "per_ip_connections": policy.per_ip_connections,
        "per_ip_requests_per_second": policy.per_ip_requests_per_second,
        "per_ip_burst": policy.per_ip_burst,
    }


def _blocked_addresses() -> list[dict]:
    """
    Addresses to refuse, skipping any whose block has expired.

    Expiry is applied here rather than by a scheduled cleanup, so a block that
    has run out stops being enforced at the next deploy whether or not anything
    swept the table.
    """
    from apps.security.models import BlockedAddress, TrafficProtectionPolicy

    if not TrafficProtectionPolicy.load().denylist_enabled:
        return []

    now = timezone.now()
    return [
        {"cidr": entry.cidr, "note": entry.note}
        for entry in BlockedAddress.objects.all()
        if entry.expires_at is None or entry.expires_at > now
    ]


def render_maps(*, rules_by_domain: dict[int, list[dict]], domains: dict[int, Domain]) -> str:
    """Render the shared http-context file: maps, limits and the blocklist."""
    rate_limited, header_routed = [], []

    for domain_id, rules in rules_by_domain.items():
        for rule in rules:
            if rule["rate_limit_enabled"]:
                rate_limited.append(rule)
            if rule["header_routes"]:
                header_routed.append({
                    **rule,
                    "domain_name": domains[domain_id].name,
                    "default_upstream": rule["upstream_name"],
                })

    return _jinja_env.get_template("00-maps.conf.j2").render(
        rate_limited_rules=rate_limited,
        header_routed_rules=header_routed,
        protection=_protection_context(),
        blocked_addresses=_blocked_addresses(),
    )


def render_panel() -> str:
    """
    Render the control panel's own server block.

    Driven by the access policy rather than by routing rules, so a mistake in a
    route can never change how the panel is reached. If the policy is not
    published, or the hostname has no certificate, this renders to nothing and
    the panel stays on the loopback listener in default.conf -- which is never
    generated and therefore always works.
    """
    from apps.security.models import PanelAccessPolicy

    policy = PanelAccessPolicy.load()
    context = {"is_published": False}

    if policy.is_published and policy.panel_domain:
        domain = (
            Domain.objects
            .filter(name=policy.panel_domain, is_active=True)
            .select_related("certificate")
            .first()
        )

        # Publishing over plain HTTP would expose the panel's session cookie to
        # anyone on the path, and that cookie controls every domain here. The
        # policy refuses to save in that state; this is the second check, for
        # the case where a certificate was deleted afterwards.
        if domain and domain.ssl_enabled and domain.ssl_cert_path and domain.ssl_key_path:
            certificate = getattr(domain, "certificate", None)
            context = {
                "is_published": True,
                "domain": domain.name,
                "cert_path": domain.ssl_cert_path,
                "key_path": domain.ssl_key_path,
                "chain_path": certificate.chain_path if certificate else "",
                "allowlist": policy.normalised_allowlist,
                "require_mtls": policy.require_mtls,
                "mtls_ca_path": policy.mtls_ca_path,
                "hsts_seconds": policy.hsts_seconds,
                "login_rate_per_minute": policy.login_rate_per_minute,
                "api_rate_per_second": policy.api_rate_per_second,
                "api_burst": max(policy.api_rate_per_second * 2, 20),
                "expose_django_admin": policy.expose_django_admin,
            }
        else:
            logger.warning(
                "Panel is marked published at %s but has no usable certificate; "
                "keeping it on the loopback listener only.",
                policy.panel_domain,
            )

    return _jinja_env.get_template("05-panel.conf.j2").render(
        panel=context,
        acme_webroot=settings.ACME_WEBROOT_DIR,
    )


def render_upstreams() -> str:
    """Render one upstream block per active backend that has instances."""
    backends = []
    for backend in Backend.objects.filter(is_active=True).prefetch_related("instances"):
        instances = list(backend.instances.filter(is_active=True))
        if not instances:
            continue
        backends.append({
            "name": backend.name,
            "upstream_name": backend.upstream_name,
            "lb_method": backend.lb_method,
            "keepalive_connections": backend.keepalive_connections,
            "keepalive_requests": backend.keepalive_requests,
            "keepalive_timeout": backend.keepalive_timeout,
            "instances": instances,
            "serving_count": sum(1 for i in instances if not i.is_draining),
            "total_count": len(instances),
        })

    return _jinja_env.get_template("10-upstreams.conf.j2").render(backends=backends)


def render_servers(*, rules_by_domain: dict[int, list[dict]], domains: dict[int, Domain]) -> str:
    """Render one server block per active domain, plus a TLS block if certified."""
    from apps.security.models import PanelAccessPolicy

    policy = PanelAccessPolicy.load()
    panel_hostname = policy.panel_domain if policy.is_published else None

    domain_contexts = []

    for domain in domains.values():
        # The panel renders its own hardened block in 05-panel.conf. Emitting a
        # second server block for the same name would make which one wins a
        # matter of file order.
        if panel_hostname and domain.name == panel_hostname:
            continue

        rules = rules_by_domain.get(domain.id, [])
        certificate = getattr(domain, "certificate", None)
        ssl_ready = bool(
            domain.ssl_enabled and domain.ssl_cert_path and domain.ssl_key_path
        )

        # A domain with no routes still deploys, so its ACME challenge stays
        # answerable and a certificate can be issued before any routing exists.
        domain_contexts.append({
            "name": domain.name,
            "slug": domain.slug,
            "ssl_enabled": ssl_ready,
            "ssl_cert_path": domain.ssl_cert_path,
            "ssl_key_path": domain.ssl_key_path,
            "chain_path": certificate.chain_path if certificate else "",
            "cert_issuer_label": (
                certificate.get_issuer_display() if certificate else "manual configuration"
            ),
            "force_ssl_redirect": domain.force_ssl_redirect,
            "rules": rules,
            "has_root_rule": any(
                r["match_value"] == "/" and r["match_type"] == RoutingRule.MatchType.PATH_PREFIX
                for r in rules
            ),
        })

    return _jinja_env.get_template("20-servers.conf.j2").render(
        domains=domain_contexts,
        acme_webroot=settings.ACME_WEBROOT_DIR,
        protection=_protection_context(),
        blocked_addresses=_blocked_addresses(),
    )


def config_render() -> dict[str, str]:
    """Render every managed config file from current database state."""
    domains = {
        d.id: d
        for d in Domain.objects.filter(is_active=True).select_related("certificate")
    }
    rules_by_domain = _active_rules_by_domain()

    return {
        "00-maps.conf": render_maps(rules_by_domain=rules_by_domain, domains=domains),
        "05-panel.conf": render_panel(),
        "10-upstreams.conf": render_upstreams(),
        "20-servers.conf": render_servers(rules_by_domain=rules_by_domain, domains=domains),
    }


def config_render_combined() -> str:
    """One annotated string of the whole config, for preview and audit."""
    return "\n".join(
        f"# ===== {name} =====\n{content}"
        for name, content in config_render().items()
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def config_write(configs: dict[str, str]) -> list[Path]:
    """Write rendered configs into the directory Nginx includes."""
    conf_dir = _conf_dir()
    conf_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for filename, content in configs.items():
        path = conf_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)
        logger.info("Wrote %s (%d bytes)", path, len(content))
    return written


def config_backup() -> Path:
    """Snapshot the managed files so a failed test can be undone."""
    conf_dir = _conf_dir()
    backup_path = _backup_dir() / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path.mkdir(parents=True, exist_ok=True)

    for name in MANAGED_FILES:
        source = conf_dir / name
        if source.exists():
            shutil.copy2(source, backup_path / name)

    _prune_backups()
    return backup_path


def _prune_backups(keep: int = 20) -> None:
    backups = sorted(_backup_dir().iterdir(), reverse=True)
    for stale in backups[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def config_restore(backup_path: Path) -> None:
    """Put the managed files back as they were before the failed deploy."""
    conf_dir = _conf_dir()

    for name in MANAGED_FILES:
        restored = backup_path / name
        current = conf_dir / name
        if restored.exists():
            shutil.copy2(restored, current)
        elif current.exists():
            # The file did not exist before this deploy, so remove it again.
            current.unlink()

    logger.info("Restored config from %s", backup_path)


# ---------------------------------------------------------------------------
# Nginx control
# ---------------------------------------------------------------------------

def _nginx_exec(args: list[str], *, timeout: int = 15) -> tuple[bool, str]:
    """
    Run an Nginx command, in its container if we are not inside it.

    Falls back to a direct call so the same code works when the gateway runs
    on the host rather than under Compose.
    """
    container = settings.NGINX_CONTAINER_NAME
    attempts = [
        ["docker", "exec", container, *args],
        args,
    ]

    last_output = ""
    for attempt in attempts:
        try:
            result = subprocess.run(attempt, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            last_output = f"{attempt[0]} not found"
            continue
        except subprocess.TimeoutExpired:
            return False, f"{' '.join(args)} timed out after {timeout}s"
        return result.returncode == 0, (result.stdout + result.stderr).strip()

    return False, last_output or "no way to reach Nginx"


def nginx_test() -> NginxTestResult:
    """Check the configuration before Nginx is asked to load it."""
    success, output = _nginx_exec(["nginx", "-t"])
    logger.info("nginx -t: %s", "ok" if success else "failed")
    return NginxTestResult(success=success, output=output)


def nginx_reload() -> NginxTestResult:
    """
    Reload gracefully: new workers take new connections, old ones drain.

    Not free. Fresh workers start with empty upstream keepalive pools, so a
    reload costs a short burst of TCP handshakes. That is why deploys are
    coalesced rather than run per edit.
    """
    success, output = _nginx_exec(["nginx", "-s", "reload"])
    logger.info("nginx reload: %s", "ok" if success else "failed")
    return NginxTestResult(success=success, output=output)


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

def config_deploy(*, user=None) -> ConfigDeployLog:
    """
    Render, verify and activate the configuration.

    Returns the log entry either way; a failed deploy is a recorded outcome,
    not an exception, because the caller is usually a Celery task whose job is
    to report rather than to crash.
    """
    configs = config_render()
    snapshot = "\n".join(f"# ===== {n} =====\n{c}" for n, c in configs.items())

    log = ConfigDeployLog.objects.create(
        status=ConfigDeployLog.Status.PENDING,
        config_snapshot=snapshot,
        deployed_by=user,
    )

    try:
        backup_path = config_backup()
        config_write(configs)

        log.status = ConfigDeployLog.Status.TESTING
        log.save(update_fields=["status"])

        test = nginx_test()
        if not test.success:
            config_restore(backup_path)
            return _fail(log, test.output)

        reload_result = nginx_reload()
        if not reload_result.success:
            config_restore(backup_path)
            nginx_reload()
            return _fail(log, f"Reload failed: {reload_result.output}")

        log.status = ConfigDeployLog.Status.DEPLOYED
        log.completed_at = timezone.now()
        log.save(update_fields=["status", "completed_at"])
        logger.info("Deploy #%s succeeded", log.pk)
        return log

    except Exception as exc:
        logger.exception("Deploy #%s raised", log.pk)
        return _fail(log, str(exc))


def _fail(log: ConfigDeployLog, error: str) -> ConfigDeployLog:
    log.status = ConfigDeployLog.Status.FAILED
    log.error_output = error[:8000]
    log.completed_at = timezone.now()
    log.save(update_fields=["status", "error_output", "completed_at"])
    logger.error("Deploy #%s failed: %s", log.pk, error)
    return log


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cache_purge_all() -> CachePurgeResult:
    """
    Empty the cache.

    Blunt: every subsequent request misses until the cache refills, so a busy
    gateway hands its whole load to the backends for a moment. Prefer
    cache_purge_url when you know what changed.
    """
    cache_dir = Path(settings.NGINX_CACHE_DIR)
    purged, errors = 0, []

    if not cache_dir.exists():
        return CachePurgeResult(purged=0, keys=[], errors=[f"{cache_dir} is not mounted here"])

    for entry in cache_dir.iterdir():
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            purged += 1
        except OSError as exc:
            errors.append(str(exc))

    logger.info("Purged %d cache shards", purged)
    return CachePurgeResult(purged=purged, keys=[], errors=errors)


def cache_purge_url(
    *,
    host: str,
    uri: str,
    scheme: str = "https",
    method: str = "GET",
    key_suffixes: list[str] | None = None,
) -> CachePurgeResult:
    """
    Remove one URL from the cache.

    Nginx's own purge directive is a commercial feature, but the on-disk layout
    is derivable: the entry lives at a path built from the MD5 of its cache
    key. A route that keys on extra headers has one entry per variant, so the
    caller passes the variant suffixes to clear them all.
    """
    if not uri.startswith("/"):
        uri = "/" + uri

    suffixes = key_suffixes if key_suffixes else [""]
    purged, cleared_keys, errors = 0, [], []

    for suffix in suffixes:
        key = cache_key_for(scheme=scheme, host=host, uri=uri, method=method, suffix=suffix)
        path = cache_path_for_key(key=key)
        try:
            path.unlink()
            purged += 1
            cleared_keys.append(key)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{key}: {exc}")

    logger.info("Purged %d entries for %s%s", purged, host, uri)
    return CachePurgeResult(purged=purged, keys=cleared_keys, errors=errors)


def cache_variant_suffixes(*, rule: RoutingRule) -> list[str]:
    """
    Every cache-key tail a route can produce.

    Only the normalised Accept-Encoding map has a known, finite set of values.
    Arbitrary keyed headers cannot be enumerated, so those routes are purged
    by their unkeyed variant and the rest expire on their own.
    """
    headers = [h.lower() for h in (rule.cache_key_headers or [])]
    if headers == ["accept-encoding"]:
        return ["", "gzip", "br", "zstd"]
    if not headers:
        return [""]
    return [""]
