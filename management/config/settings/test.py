"""
Settings for running tests and checks without the Compose stack.

SQLite and an in-process cache stand in for Postgres and Redis; Celery runs
tasks inline. Everything the gateway writes is redirected into a temporary
directory so a test run never touches a real Nginx config.
"""
import os
import tempfile
from pathlib import Path

from config.settings.base import *  # noqa: F401,F403

_TMP = Path(tempfile.mkdtemp(prefix="cloudbalancer-test-"))

DEBUG = False
SECRET_KEY = "test-only-key"
ALLOWED_HOSTS = ["*"]

# base.py derives these from DEBUG as it is imported, which here is before the
# line above has run. Setting them explicitly means the suite exercises the
# same cookie posture production uses, rather than a weaker one that would let
# a regression through.
PANEL_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# In-memory by default. Point SQLITE_PATH at a file to keep data between
# runs, which is what `make dev` does so the panel has something to show.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", ":memory:"),
    }
}

# No caching during tests.
#
# Django rebuilds the database between tests but nothing resets the cache, so
# a cached singleton outlives the rows it was read from and leaks into the
# next test with entirely different data. Caching is a production concern; the
# tests that exercise it turn it on for themselves with override_settings.
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}
}

# A real cache, for the handful of tests that are about caching.
LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "cloudbalancer-test",
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BEAT_SCHEDULE = {}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

def _path(name: str, default: str) -> str:
    """
    A writable path for tests, overridable from the environment.

    Every one of these goes through here deliberately. When only some of them
    honoured an override, a caller that set NGINX_SSL_DIR got its certificates
    written somewhere else entirely -- and the only symptom was Nginx failing
    to open a file at a path nothing had told it to use.
    """
    return os.environ.get(name, str(_TMP / default))


NGINX_CONF_DIR = _path("NGINX_CONF_DIR", "conf.d")
NGINX_SSL_DIR = _path("NGINX_SSL_DIR", "ssl")
NGINX_CACHE_DIR = _path("NGINX_CACHE_DIR", "cache")
LETSENCRYPT_DIR = _path("LETSENCRYPT_DIR", "letsencrypt")
CERTBOT_WORK_DIR = _path("CERTBOT_WORK_DIR", "letsencrypt-work")
CERTBOT_LOG_DIR = _path("CERTBOT_LOG_DIR", "letsencrypt-log")
ACME_WEBROOT_DIR = _path("ACME_WEBROOT_DIR", "acme")

for _path in (
    NGINX_CONF_DIR, NGINX_SSL_DIR, NGINX_CACHE_DIR,
    LETSENCRYPT_DIR, ACME_WEBROOT_DIR,
):
    Path(_path).mkdir(parents=True, exist_ok=True)

GATEWAY_DEPLOY_DEBOUNCE_SECONDS = 0
