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
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "insecure-dev-key-change-in-production-!!"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
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
        "LOCATION": os.environ.get("REDIS_URL", "redis://redis:6379/0"),
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
}

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
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "EXCEPTION_HANDLER": "rest_framework.views.exception_handler",
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
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
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
NGINX_CONTAINER_NAME = os.environ.get("NGINX_CONTAINER_NAME", "cloudbalancer-nginx")
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
