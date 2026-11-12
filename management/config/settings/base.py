"""
Django settings for CloudBalancer.

Uses environment variables for all secrets and host-specific configuration.
"""
import os
from pathlib import Path

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# =============================================================================
# Core
# =============================================================================
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

# Session cookies, CSRF tokens and the hashes protecting two-factor recovery
# codes are all derived from this. A known value means all three can be
# forged, which is full access to the gateway without a password.
#
# It used to fall back to a fixed string. That is fine in development and
# catastrophic in production, and the failure is silent -- everything works,
# and the key protecting it is in a public repository. Refusing to start is
# the only safe response.
_PLACEHOLDER_KEYS = {
    "",
    "CHANGE_ME",
    "insecure-dev-key-change-in-production-!!",
    "insecure-dev-key-change-in-production",
}

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()

if SECRET_KEY in _PLACEHOLDER_KEYS or len(SECRET_KEY) < 32:
    if DEBUG:
        SECRET_KEY = "insecure-development-key-never-use-outside-debug"
    else:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is unset, a placeholder, or shorter than 32 "
            "characters, and DJANGO_DEBUG is off. Sessions, CSRF tokens and "
            "two-factor recovery codes all derive from it, so a predictable "
            "value is equivalent to no authentication at all.\n\n"
            "Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(50))\""
        )
def _allowed_hosts() -> list[str]:
    """
    The hostnames Django will answer for, plus the container's own loopback.

    ALLOWED_HOSTS exists to stop an attacker putting their own hostname in a
    Host header and having it echoed back into a password-reset link or a
    cache key. Loopback cannot be used that way: Nginx rewrites Host to the
    name the client asked for, so a request presenting 127.0.0.1 can only have
    come from inside this container.

    It is appended unconditionally because the health check talks to
    127.0.0.1:8000. Leaving it to be configured meant a deployment that set
    DJANGO_ALLOWED_HOSTS to its own public address broke its own liveness
    probe, and the only symptom was a DisallowedHost traceback every ten
    seconds with no connection to the setting that caused it.
    """
    configured = [
        host.strip()
        for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
        if host.strip()
    ]
    if "*" in configured:
        return configured

    # Loopback for the container's own health check, and the compose service
    # name for anything calling across the internal network -- Prometheus
    # scrapes http://django:8000/metrics, and Nginx is reachable the same way.
    # Neither name resolves outside the Docker network, so neither can be
    # spoofed by a client the way a public hostname can.
    internal = ["127.0.0.1", "localhost", "[::1]"]
    internal += [
        name.strip()
        for name in os.environ.get("DJANGO_INTERNAL_HOSTS", "django").split(",")
        if name.strip()
    ]

    for host in internal:
        if host not in configured:
            configured.append(host)
    return configured


ALLOWED_HOSTS = _allowed_hosts()
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# =============================================================================
# Installed apps
# =============================================================================
INSTALLED_APPS = [
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "django_filters",
    "corsheaders",
    "django_celery_beat",
    "django_prometheus",
    # Local apps
    "apps.accounts",
    "apps.security",
    "apps.domains",
    "apps.backends",
    "apps.routing",
    "apps.certificates",
    "apps.monitoring",
    "apps.gateway",
    "apps.backups",
    "apps.alerts",
]

# =============================================================================
# Middleware
# =============================================================================
MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Address filtering runs before sessions: a caller who is not allowed to be
    # here should not get as far as having a session looked up.
    "apps.security.middleware.IpAllowlistMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # These need request.user, so they follow authentication.
    "apps.security.middleware.SessionSecurityMiddleware",
    # Before the second-factor check. Both can be pending on a new
    # account, and the password is the one the admin who created it
    # already knows.
    "apps.security.middleware.RequirePasswordChangeMiddleware",
    "apps.security.middleware.RequireTotpMiddleware",
    "apps.security.middleware.AuditMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

# =============================================================================
# Templates
# =============================================================================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# =============================================================================
# Database — PostgreSQL
# =============================================================================
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "cloudbalancer"),
        "USER": os.environ.get("POSTGRES_USER", "cloudbalancer"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "changeme_in_production"),
        "HOST": os.environ.get("POSTGRES_HOST", "postgres"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 600,
    }
}

# =============================================================================
# Cache — Redis
# =============================================================================
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        # Database 1. The broker owns database 0, and sharing one would let
        # cache growth evict queued tasks -- a deploy or a certificate renewal
        # disappearing because a dashboard was left open.
        "LOCATION": os.environ.get("REDIS_CACHE_URL", "redis://redis:6379/1"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

# =============================================================================
# Celery
# =============================================================================
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 270
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200

# Periodic work. Health probing is frequent and cheap; certificate renewal is
# daily because a CA will not issue faster than that anyway and the renewal
# window is measured in weeks.
CELERY_BEAT_SCHEDULE = {
    "health-check-all-backends": {
        "task": "gateway.health_check_all",
        "schedule": float(os.environ.get("HEALTH_CHECK_INTERVAL", 15)),
    },
    "renew-due-certificates": {
        "task": "certificates.renew_due",
        "schedule": crontab(hour="3", minute="17"),
    },
    "sync-certificate-metadata": {
        "task": "certificates.sync_metadata",
        "schedule": crontab(hour="*/6", minute="5"),
    },
    "prune-security-history": {
        "task": "security.prune_history",
        "schedule": crontab(hour="4", minute="23"),
    },
    # Early, and before the security prune, so a backup exists from before
    # anything was trimmed. Verification runs inside this task rather than on
    # its own schedule: a verification pass that lags by a day reports on
    # yesterday's archive and says nothing about today's.
    "nightly-backup": {
        "task": "backups.run_scheduled",
        "schedule": crontab(hour="2", minute="41"),
    },
    # Often enough that a certificate expiring or a backend going down is
    # noticed while somebody is still at work, rarely enough that a condition
    # which fixes itself in a minute is never mentioned at all.
    "alert-checks": {
        "task": "alerts.run_checks",
        "schedule": float(os.environ.get("ALERT_CHECK_INTERVAL", 300)),
    },
    "prune-alerts": {
        "task": "alerts.prune",
        "schedule": crontab(hour="4", minute="51"),
    },
}

# =============================================================================
# Alerts
#
# One path from "something is wrong" to "somebody was told". Prometheus
# alerts arrive through an Alertmanager webhook; conditions only the control
# plane can see -- certificate expiry, backup verification, a refused deploy
# -- are raised directly. Both end up in one history with one set of
# channels, because two notification paths means two sets to keep in step and
# the one nobody updated is the one that matters.
# =============================================================================
ALERT_CHECK_INTERVAL = int(os.environ.get("ALERT_CHECK_INTERVAL", 300))
# How long before a still-firing alert is mentioned again. Acknowledging one
# stops this for that alert.
ALERT_REPEAT_MINUTES = int(os.environ.get("ALERT_REPEAT_MINUTES", 60))
ALERT_RETENTION_DAYS = int(os.environ.get("ALERT_RETENTION_DAYS", 90))
ALERT_HTTP_TIMEOUT = float(os.environ.get("ALERT_HTTP_TIMEOUT", 10))
# Put in the body of every notification, so a message is one click from the
# page that explains it.
ALERT_PANEL_URL = os.environ.get("ALERT_PANEL_URL", "")
ALERTMANAGER_WEBHOOK_SECRET = os.environ.get("ALERTMANAGER_WEBHOOK_SECRET", "")

ALERT_CERT_WARNING_DAYS = int(os.environ.get("ALERT_CERT_WARNING_DAYS", 14))
ALERT_CERT_CRITICAL_DAYS = int(os.environ.get("ALERT_CERT_CRITICAL_DAYS", 3))
ALERT_DISK_WARNING_PERCENT = float(os.environ.get("ALERT_DISK_WARNING_PERCENT", 15))
ALERT_DISK_CRITICAL_PERCENT = float(os.environ.get("ALERT_DISK_CRITICAL_PERCENT", 5))

# =============================================================================
# Email
#
# Only used for alerts. Unset means the email channel refuses to be used
# rather than silently discarding messages, which is what Django's default
# console backend would do in production.
# =============================================================================
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "false").lower() == "true"
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", 15))
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", "cloudbalancer@localhost"
)

# =============================================================================
# Backups
#
# The archive holds the database, every certificate and its private key, and
# the generated configuration. Without an encryption key it is a plaintext
# copy of every secret the gateway serves, which matters the moment it is
# copied anywhere else.
# =============================================================================
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/var/backups/cloudbalancer")
BACKUP_TIMEOUT = int(os.environ.get("BACKUP_TIMEOUT", 900))
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", 14))
BACKUP_KEEP_WEEKS = int(os.environ.get("BACKUP_KEEP_WEEKS", 12))
# How old the newest *verified* backup may be before the panel says so.
BACKUP_STALE_HOURS = int(os.environ.get("BACKUP_STALE_HOURS", 36))
# 64 hex characters. `openssl rand -hex 32`. Empty means no encryption.
BACKUP_ENCRYPTION_KEY = os.environ.get("BACKUP_ENCRYPTION_KEY", "")

# How long the security tables keep their rows. Sign-in attempts answer "is
# someone trying right now"; the audit trail answers "who changed this", which
# is asked about far older things.
LOGIN_ATTEMPT_RETENTION_DAYS = int(os.environ.get("LOGIN_ATTEMPT_RETENTION_DAYS", 30))
AUDIT_RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", 365))

# =============================================================================
# Django REST Framework
# =============================================================================
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Role-aware by default, so an endpoint added later is covered by what
    # its method implies -- a read needs a viewer, a write needs an operator --
    # rather than being open to anyone signed in because nobody restricted it.
    # A view that needs something else sets `required_role`.
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.security.roles.RolePermission",
    ],
    "EXCEPTION_HANDLER": "common.exception_handlers.exception_handler",
}

# =============================================================================
# CORS / CSRF
#
# The control panel is served from the same origin as the API in production,
# so CORS only matters while the Vite dev server runs on its own port.
# =============================================================================
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", ",".join(CORS_ALLOWED_ORIGINS)).split(",")
    if origin.strip()
]
CSRF_COOKIE_HTTPONLY = False  # the SPA reads this cookie to echo the token back
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_USE_SESSIONS = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"

# Whether the session and CSRF cookies are marked Secure, meaning the browser
# will only ever send them back over HTTPS.
#
# On by default, and it should stay on. Turn it off only to reach the panel
# over plain HTTP on a port -- `http://10.0.0.5:8081` -- on a network you
# control. Browsers make an exception for `localhost`, so an SSH tunnel does
# not need this; an IP address does.
#
# The cost is real: with this off, the password, the authenticator code and the
# session cookie all cross the network in clear text, and anyone positioned to
# read them controls every domain this gateway serves. It is a reasonable trade
# on a private LAN or over a VPN, and not one anywhere else.
PANEL_COOKIE_SECURE = os.environ.get(
    "PANEL_COOKIE_SECURE", "false" if DEBUG else "true"
).lower() in ("true", "1", "yes")

SESSION_COOKIE_SECURE = PANEL_COOKIE_SECURE
CSRF_COOKIE_SECURE = PANEL_COOKIE_SECURE
SESSION_COOKIE_NAME = "gateway_session"
SESSION_COOKIE_AGE = 12 * 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# =============================================================================
# Transport security
#
# Nginx sets most of these headers on the panel's own server block, which is
# where they cover the static bundle too. They are repeated here so a request
# that reaches Django by another path is not left unprotected.
# =============================================================================
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Redirection and HSTS are Nginx's job on the panel hostname; doing them here
# as well would break the loopback listener, which is the break-glass path.
SECURE_SSL_REDIRECT = False

# =============================================================================
# Second factor
# =============================================================================
TOTP_ISSUER = os.environ.get("TOTP_ISSUER", "CloudBalancer")

# =============================================================================
# Static files
# =============================================================================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# =============================================================================
# Auth
# =============================================================================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        # Longer than Django's default of 8. This password is one factor of two
        # on a surface that controls every domain the gateway serves.
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# =============================================================================
# Internationalization
# =============================================================================
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# =============================================================================
# Gateway — where the control plane writes, and how it reaches Nginx
# =============================================================================
NGINX_CONF_DIR = os.environ.get("NGINX_CONF_DIR", "/etc/nginx/conf.d")
# A directory shared with the Nginx container, where the control plane asks
# for `nginx -t` and `nginx -s reload` and a watcher there answers. This
# replaced mounting /var/run/docker.sock into the management container, which
# gave the panel the unauthenticated Docker API -- and therefore root on the
# host -- in order to send two signals.
NGINX_CONTROL_DIR = os.environ.get("NGINX_CONTROL_DIR", "/var/run/cloudbalancer")
NGINX_CONTROL_TIMEOUT = int(os.environ.get("NGINX_CONTROL_TIMEOUT", 30))
NGINX_SSL_DIR = os.environ.get("NGINX_SSL_DIR", "/etc/nginx/ssl")
NGINX_CACHE_DIR = os.environ.get("NGINX_CACHE_DIR", "/var/cache/nginx")

# Editing several rules in the panel should cost one reload, not one each.
# Every reload starts workers with empty upstream keepalive pools, so a burst
# of reloads is a burst of TCP handshakes on the backend hop.
GATEWAY_DEPLOY_DEBOUNCE_SECONDS = int(os.environ.get("GATEWAY_DEPLOY_DEBOUNCE", 3))

# =============================================================================
# Observability sources
# =============================================================================
NGINX_VTS_URL = os.environ.get("NGINX_VTS_URL", "http://nginx:8080/status/format/json")
NGINX_VTS_TIMEOUT = float(os.environ.get("NGINX_VTS_TIMEOUT", 2.0))
# Shorter than the dashboard's poll interval, so a refresh still shows
# movement, but long enough that one page load is one fetch.
NGINX_VTS_CACHE_SECONDS = float(os.environ.get("NGINX_VTS_CACHE_SECONDS", 2.0))
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
PROMETHEUS_TIMEOUT = float(os.environ.get("PROMETHEUS_TIMEOUT", 5.0))
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")

# =============================================================================
# ACME / TLS
# =============================================================================
CERTBOT_BIN = os.environ.get("CERTBOT_BIN", "certbot")
CERTBOT_TIMEOUT = int(os.environ.get("CERTBOT_TIMEOUT", 180))
LETSENCRYPT_DIR = os.environ.get("LETSENCRYPT_DIR", "/etc/letsencrypt")
CERTBOT_WORK_DIR = os.environ.get("CERTBOT_WORK_DIR", "/var/lib/letsencrypt")
CERTBOT_LOG_DIR = os.environ.get("CERTBOT_LOG_DIR", "/var/log/letsencrypt")

# Where Nginx serves the HTTP-01 challenge from. Both containers mount it.
ACME_WEBROOT_DIR = os.environ.get("ACME_WEBROOT_DIR", "/var/www/acme")

# ECDSA by default: smaller handshakes, and every browser in use supports it.
ACME_KEY_TYPE = os.environ.get("ACME_KEY_TYPE", "ecdsa")
SELF_SIGNED_ORG = os.environ.get("SELF_SIGNED_ORG", "CloudBalancer")

# =============================================================================
# Logging
# =============================================================================
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname:<8} {name} | {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.db.backends": {"level": "WARNING"},
    },
}
