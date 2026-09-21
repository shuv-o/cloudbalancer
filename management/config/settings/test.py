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

# In-memory by default. Point SQLITE_PATH at a file to keep data between
# runs, which is what `make dev` does so the panel has something to show.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", ":memory:"),
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "cloudbalancer-test",
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BEAT_SCHEDULE = {}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

NGINX_CONF_DIR = os.environ.get("NGINX_CONF_DIR", str(_TMP / "conf.d"))
NGINX_SSL_DIR = str(_TMP / "ssl")
NGINX_CACHE_DIR = str(_TMP / "cache")
LETSENCRYPT_DIR = str(_TMP / "letsencrypt")
CERTBOT_WORK_DIR = str(_TMP / "letsencrypt-work")
CERTBOT_LOG_DIR = str(_TMP / "letsencrypt-log")
ACME_WEBROOT_DIR = str(_TMP / "acme")

for _path in (
    NGINX_CONF_DIR, NGINX_SSL_DIR, NGINX_CACHE_DIR,
    LETSENCRYPT_DIR, ACME_WEBROOT_DIR,
):
    Path(_path).mkdir(parents=True, exist_ok=True)

GATEWAY_DEPLOY_DEBOUNCE_SECONDS = 0
