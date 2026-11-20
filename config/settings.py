"""Loopback-only pilot. No credentials are shipped in the source tree."""
import os
import secrets
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)
DATA_DIR = Path(os.environ.get("LEAD_DATA_DIR", BASE_DIR / "data")).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
secret_path = DATA_DIR / ".django-secret"
if not os.environ.get("DJANGO_SECRET_KEY") and not secret_path.exists():
    try:
        with secret_path.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(48))
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError:
        pass
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or secret_path.read_text(encoding="utf-8").strip()
DEBUG = os.environ.get("LEAD_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]", "testserver"]
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "radar",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True, "OPTIONS": {"builtins": ["radar.templatetags.radar_ui"], "context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "radar.context_processors.runtime_context",
        "radar.ui_language.language_context",
    ]},
}]
database_url = os.environ.get("DATABASE_URL", "").strip()
if database_url:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use postgresql://; leave blank for local SQLite pilot")
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""), "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "127.0.0.1", "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": 0,
    }}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "radar.sqlite3", "OPTIONS": {"timeout": 30}}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = DATA_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
LEAD_MODEL_ENABLED = os.environ.get("LEAD_MODEL_ENABLED", "false").lower() == "true"
LEAD_MAX_ITEMS = max(1, min(30, int(os.environ.get("LEAD_MAX_ITEMS", "8"))))
LEAD_MAX_PAGES = max(1, min(5, int(os.environ.get("LEAD_MAX_PAGES", "1"))))
LEAD_HEAD_PAGES = max(1, min(20, int(os.environ.get("LEAD_HEAD_PAGES", "3"))))
LEAD_CRAWL_TIMEOUT = max(30, min(600, int(os.environ.get("LEAD_CRAWL_TIMEOUT", "180"))))
LEAD_MODEL_DAILY_BUDGET = max(0, int(os.environ.get("LEAD_MODEL_DAILY_BUDGET", "30")))
LEAD_AUTOMATIC_ENABLED = os.environ.get("LEAD_AUTOMATIC_ENABLED", "false").lower() == "true"

