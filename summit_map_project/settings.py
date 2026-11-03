"""settings for the summit map site. most of the server values come
from env vars so the same code runs local and in docker."""

from pathlib import Path
import os

# project root, used to build the other paths
BASE_DIR = Path(__file__).resolve().parent.parent


# debug is on by default for local dev. the container sets it to False
DEBUG = os.environ.get("DJANGO_DEBUG", "True") == "True"

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-b6%1kx+tmsm#029s!=*sl1il(ad^f5dsyath-&(y$ih*!s+p3&"
)

# dont let the dev key go live by mistake
if not DEBUG and SECRET_KEY.startswith("django-insecure-"):
    raise ValueError(
        "DJANGO_SECRET_KEY must be set to a real secret when DEBUG is off.")

# hosts django will answer for
ALLOWED_HOSTS = [
    host for host in
    os.environ.get(
        "DJANGO_ALLOWED_HOSTS",
        "127.0.0.1,localhost,192.168.0.215,summit.andw.net").split(",")
    if host
]
# post requests (login, logging sends) only pass csrf from these origins
CSRF_TRUSTED_ORIGINS = [
    origin for origin in
    os.environ.get(
        "DJANGO_CSRF_ORIGINS", "https://summit.andw.net").split(",")
    if origin
]
# cloudflare terminates https and passes plain http to django, so trust
# the forwarded proto. cookies only go over https in prod
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG


# apps
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'climbs',
    'accounts'
]

AUTH_USER_MODEL = 'accounts.User'

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'summit_map_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'summit_map_project.context_processors.version',
                'summit_map_project.context_processors.install_prompt',
            ],
        },
    },
]

WSGI_APPLICATION = 'summit_map_project.wsgi.application'


# sqlite is fine for the size of this site
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get("DJANGO_DB_PATH", BASE_DIR / 'db.sqlite3'),
    }
}


# password rules from django
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# timezone is utc in the db. the views convert to london for display
LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# static files. hashed names only in prod so dev and tests dont need
# a seperate collectstatic run
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
if not DEBUG:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_PATH", BASE_DIR / 'media'))
LOGIN_REDIRECT_URL = "climbs:map"
LOGOUT_REDIRECT_URL = "climbs:map"

# points config, used by climbs/scoring.py. grade mode can be flat,
# gentle or linear. tries table is the multiplier. anything at or
# above the top key gets the top value, so 6 and 40 tries score the same
POINTS_GRADE_MODE = "gentle"
POINTS_TRIES_MULTIPLIERS = {1: 1.00, 2: 0.99, 3: 0.98, 4: 0.96, 5: 0.94}

# anti cheat config. all the thresholds live here so they can be tuned
# in one place. the peer based rules dont run until there is enough
# users to compare against
ANTI_CHEAT = {
    # need at least this many users before peer stats mean anything
    "min_users_for_peer_stats": 5,
    # 1. loads of flashes
    "flash_rate_min_sends": 8,
    "flash_rate_threshold": 0.90,
    "flash_rate_peer_margin": 0.40,
    "flash_rate_min_grade_sends": 3,
    # 2. flashing something way harder than your normal level
    "big_grade_gap": 2,
    "big_grade_high": 6,
    # 3. flashing climbs that other people find hard
    "community_flash_min_ascents": 5,
    "community_flash_low_rate": 0.20,
    "community_flash_repeat": 3,
    # 4. way fewer tries than other people on the same climbs
    "tries_peer_min_shared": 3,
    "tries_peer_margin": 2.5,
    # 5. logging sends faster than the tries claim is possible
    "minutes_per_attempt": 1.5,
    "pace_count": 6,
    "pace_window_minutes": 30,
    # 6. bulk logs in one minute or logs outside gym hours
    "bulk_per_minute": 4,
    "gym_open_hour_start": 10,
    "gym_open_hour_end": 22,
    # 7. tries edited down after the send
    "edit_down_points": 10,
    "edit_down_rank_points": 20,
    # 8. big points jump in 24h or a new account hitting the top 3
    "rank_jump_points_24h": 2000,
    "new_account_days": 7,
    # 9. sending rate per minute compared to the rest of the gym
    "send_rate_min_sends": 5,
    "send_rate_percentile": 0.95,
    "send_rate_min_sessions": 10,
    "send_rate_absolute_per_min": 2.0,
    # 10. harder grades should take longer between attempts
    "grade_pace_base_minutes": 1.0,
    "grade_pace_per_grade_minutes": 0.4,
    # 11. flash rate creeping up compared to your older sends
    "flash_drift_window_sends": 10,
    "flash_drift_min_history": 10,
    "flash_drift_margin": 0.45,
    "flash_drift_burst_days": 7,
    # 12. grade jumping faster than is normal
    "grade_jump_days": 7,
    "grade_jump_grades": 3,
    "grade_jump_min_history": 5,
    # 13. a whole day logged with tiny gaps, looks like batch logging
    "cluster_min_sends": 8,
    "cluster_median_gap_seconds": 60,
    # 14. sends per visit compared to the gym average
    "z_min_days": 5,
    "z_threshold": 2.0,
    # hard limit on how many sends one user can log an hour
    "ascent_rate_limit_per_hour": 30,
    # suspicion score stops at 100
    "suspicion_cap": 100,
}

# just print emails to the console, no mail server needed. probaly only
# used in dev
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
