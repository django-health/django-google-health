"""Minimal Django settings for the bundled demo project.

Demonstrates installing both ``healthdatamodel`` (storage) and ``googlehealth``
(this app) alongside Django's built-in apps.

Google OAuth credentials, scopes, and webhook secret are read from environment
variables so you can run the demo without editing this file:

* ``GOOGLE_HEALTH_CLIENT_ID``
* ``GOOGLE_HEALTH_CLIENT_SECRET``
* ``GOOGLE_HEALTH_REDIRECT_URI`` (must EXACTLY match what's registered in
  Google Cloud Console, including the trailing slash)
* ``GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`` (optional, only for webhook testing)
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-demo-key-do-not-use-in-production"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "healthdatamodel",
    "googlehealth",
    "demo",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "demo.urls"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

GOOGLE_HEALTH_CLIENT_ID = os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "")
GOOGLE_HEALTH_CLIENT_SECRET = os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "")
GOOGLE_HEALTH_REDIRECT_URI = os.environ.get(
    "GOOGLE_HEALTH_REDIRECT_URI",
    "http://localhost:8000/google-health/callback/",
)
GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION = os.environ.get(
    "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION", ""
)

# Scopes requested at connect time. The package default omits profile, but the
# demo runs sync with compute_basal=True, which calls users.getProfile (for the
# age/sex needed by the BMR calc) — that endpoint needs profile.readonly, so we
# add it here. Without it, sync fails with
# "HTTP 403: Request had insufficient authentication scopes".
GOOGLE_HEALTH_DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
]

# Where googlehealth.views.callback / disconnect redirect to. Library default
# is /admin/; for the demo we want the user-facing homepage.
GOOGLE_HEALTH_CONNECT_SUCCESS_URL = "/"
