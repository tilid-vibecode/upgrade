# File location: /server/server/settings.py
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from dotenv import load_dotenv

from evidence_matrix.weight_profiles import DEFAULT_WEIGHT_PROFILES

BASE_DIR = Path(__file__).resolve().parent.parent
env_path = BASE_DIR.parent / '.env'
load_dotenv(dotenv_path=env_path)

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'change-me-in-production')
DEBUG = os.getenv('DJANGO_DEBUG', 'false').lower() == 'true'
ENVIRONMENT = os.getenv('DJANGO_ENVIRONMENT', 'development')

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,0.0.0.0').split(',')
    if h.strip()
]

CSRF_TRUSTED_ORIGINS = []
for host in ALLOWED_HOSTS:
    CSRF_TRUSTED_ORIGINS.append(f'http://{host}')
    CSRF_TRUSTED_ORIGINS.append(f'https://{host}')
if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        'http://localhost:5173',
        'https://localhost:3000',
    ]

_cors_env = os.getenv('CORS_ALLOWED_ORIGINS', '')
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_env.split(',') if o.strip()]
if DEBUG and not CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS = [
        'http://localhost:5173',
        'http://localhost:3000',
        'https://localhost:3000',
        'http://localhost:8000',
        'http://127.0.0.1:8000',
    ]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
    'django_extensions',
    'basics',
    'company_intake',
    'org_context',
    'employee_assessment',
    'skill_blueprint',
    'evidence_matrix',
    'development_plans',
    'organization',
    'feature',
    'media_storage',
    'scheduler',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'server.urls'
ASGI_APPLICATION = 'server.asgi.application'
AUTH_USER_MODEL = 'auth.User'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

DATA_UPLOAD_MAX_NUMBER_FIELDS = 10240

_db_url = os.getenv('DATABASE_URL', 'postgresql://upg:upg@localhost:5432/upg')
_parsed = urlparse(_db_url)
_qs = parse_qs(_parsed.query or '')


def _qparam(key, default=None):
    vals = _qs.get(key, [default])
    return vals[0] if vals else default


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': _parsed.hostname or 'localhost',
        'PORT': _parsed.port or 5432,
        'NAME': (_parsed.path or '/upg').lstrip('/'),
        'USER': unquote(_parsed.username) if _parsed.username else 'upg',
        'PASSWORD': unquote(_parsed.password) if _parsed.password else 'upg',
        'CONN_MAX_AGE': 600,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'connect_timeout': int(_qparam('connect_timeout', 30)),
            'application_name': os.getenv('APP_NAME', 'upg-server'),
        },
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'static'


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

REDIS_CONFIG = {
    'HOST': os.getenv('REDIS_HOST', 'localhost'),
    'PORT': int(os.getenv('REDIS_PORT', '6379')),
    'DB': int(os.getenv('REDIS_DB', '0')),
    'PASSWORD': os.getenv('REDIS_PASSWORD') or None,
    'SSL': os.getenv('REDIS_SSL', 'false').lower() == 'true',
    'POOL_MAX': int(os.getenv('REDIS_POOL_MAX', '50')),
    'TIMEOUT': int(os.getenv('REDIS_TIMEOUT', '5')) or None,
    'HEALTH_CHECK_INTERVAL': 30,
    'DECODE_RESPONSES': True,
}

# ---------------------------------------------------------------------------
# Dramatiq
# ---------------------------------------------------------------------------

DRAMATIQ_MAX_RETRIES = int(os.getenv('DRAMATIQ_MAX_RETRIES', '10'))
DRAMATIQ_MIN_BACKOFF_MS = int(os.getenv('DRAMATIQ_MIN_BACKOFF_MS', '1000'))
DRAMATIQ_MAX_BACKOFF_MS = int(os.getenv('DRAMATIQ_MAX_BACKOFF_MS', '600000'))
DRAMATIQ_USE_RESULTS = os.getenv('DRAMATIQ_USE_RESULTS', 'false').lower() == 'true'
DRAMATIQ_HEARTBEAT_TIMEOUT_MS = 90_000
DRAMATIQ_DEAD_TTL_MS = 7 * 24 * 3600 * 1000
DRAMATIQ_MAINTENANCE_CHANCE = 1000
DRAMATIQ_QUEUES = ['default']


# =============================================================================
# STORAGE — unified dual-role configuration
# =============================================================================
# Every backend speaks the S3 protocol.  MinIO is S3 with an ENDPOINT_URL.
#
# Two roles:
#   processing  — fast MinIO scratch space for pipelines (7-day TTL)
#   persistent  — durable S3/MinIO for user-facing files + signed URLs
#   static      — Django collectstatic target (public-read bucket)
#
# Environment mapping (set via env vars):
#   Local:        both roles → local_minio  (different buckets)
#   Staging/Prod: processing → cluster_minio, persistent/static → aws_s3
# =============================================================================

STORAGE_BACKENDS = {
    'local_minio': {
        'ENDPOINT_URL': os.getenv('MINIO_ENDPOINT_URL', 'http://localhost:9000'),
        'ACCESS_KEY_ID': os.getenv('MINIO_ACCESS_KEY', 'minioadmin'),
        'SECRET_ACCESS_KEY': os.getenv('MINIO_SECRET_KEY', 'minioadmin'),
        'REGION_NAME': 'us-east-1',
        'CONNECT_TIMEOUT': 5,
        'READ_TIMEOUT': 15,
        'MAX_RETRIES': 3,
    },
    'cluster_minio': {
        'ENDPOINT_URL': os.getenv('MINIO_CLUSTER_ENDPOINT_URL', ''),
        'ACCESS_KEY_ID': os.getenv('MINIO_CLUSTER_ACCESS_KEY', ''),
        'SECRET_ACCESS_KEY': os.getenv('MINIO_CLUSTER_SECRET_KEY', ''),
        'REGION_NAME': os.getenv('MINIO_CLUSTER_REGION', 'us-east-1'),
        'CONNECT_TIMEOUT': 5,
        'READ_TIMEOUT': 15,
        'MAX_RETRIES': 3,
    },
    'aws_s3': {
        'ENDPOINT_URL': None,
        'ACCESS_KEY_ID': os.getenv('AWS_S3_ACCESS_KEY_ID', ''),
        'SECRET_ACCESS_KEY': os.getenv('AWS_S3_SECRET_ACCESS_KEY', ''),
        'REGION_NAME': os.getenv('AWS_S3_REGION', 'us-east-1'),
        'CONNECT_TIMEOUT': 5,
        'READ_TIMEOUT': 15,
        'MAX_RETRIES': 3,
    },
}

PROCESSING_STORAGE = {
    'BACKEND': os.getenv('PROCESSING_STORAGE_BACKEND', 'local_minio'),
    'BUCKET': os.getenv('PROCESSING_STORAGE_BUCKET', 'upg-processing'),
    'LIFECYCLE_TTL_DAYS': int(os.getenv('PROCESSING_LIFECYCLE_TTL_DAYS', '7')),
}

PERSISTENT_STORAGE = {
    'BACKEND': os.getenv('PERSISTENT_STORAGE_BACKEND', 'local_minio'),
    'BUCKET': os.getenv('PERSISTENT_STORAGE_BUCKET', 'upg-persistent'),
}

STATIC_STORAGE = {
    'BACKEND': os.getenv('STATIC_STORAGE_BACKEND', 'local_minio'),
    'BUCKET': os.getenv('STATIC_STORAGE_BUCKET', 'upg-static'),
}

# Django staticfiles backend — writes to S3/MinIO static bucket
STORAGES = {
    'staticfiles': {
        'BACKEND': 'server.storage.django_static.S3StaticStorage',
    },
}

# =============================================================================
# MEDIA UPLOAD LIMITS (validation constants — not backend config)
# =============================================================================

MEDIA_UPLOAD_CONFIG = {
    'MAX_FILES_PER_MESSAGE': 20,
    'MAX_FILE_SIZE_BYTES': 10 * 1024 * 1024,  # 10 MB
    'ALLOWED_CONTENT_TYPES': {
        'image/jpeg', 'image/png', 'image/gif', 'image/webp',
        'image/bmp', 'image/tiff', 'image/svg+xml',
        'application/pdf',
        'text/csv',
        'application/csv',
        'application/vnd.ms-excel',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain',
    },
    'ALLOWED_EXTENSIONS': {
        '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.svg',
        '.pdf', '.csv', '.doc', '.docx', '.txt',
    },
    'SIGNED_URL_EXPIRY_SECONDS': 3600,
}


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------

QDRANT_CONFIG = {
    'HOST': os.getenv('QDRANT_HOST', 'localhost'),
    'PORT': int(os.getenv('QDRANT_PORT', '6333')),
    'GRPC_PORT': int(os.getenv('QDRANT_GRPC_PORT', '6334')),
    'API_KEY': os.getenv('QDRANT_API_KEY') or None,
    'HTTPS': os.getenv('QDRANT_HTTPS', 'false').lower() == 'true',
    'TIMEOUT': 30.0,
    'PREFER_GRPC': False,
}

# ---------------------------------------------------------------------------
# AI / Embeddings
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
UPG_FLOW_MODEL = os.getenv('UPG_FLOW_MODEL', 'gpt-4o-mini')

EMBEDDING_CONFIG = {
    'PROVIDER': 'openai',
    'OPENAI_MODEL': 'text-embedding-3-small',
    'OPENAI_DIMENSIONS': 1536,
    'OPENAI_BATCH_SIZE': 100,
    'ACTIVE_DIMENSIONS': 1536,
}

# ---------------------------------------------------------------------------
# Org context
# ---------------------------------------------------------------------------

ORG_CONTEXT_CONFIG = {
    'COLLECTION_NAME': os.getenv('ORG_CONTEXT_COLLECTION', 'org_context_documents'),
    'VECTOR_SIZE': EMBEDDING_CONFIG['ACTIVE_DIMENSIONS'],
    'DEFAULT_TOP_K': int(os.getenv('ORG_CONTEXT_TOP_K', 10)),
    'MAX_CANDIDATES': int(os.getenv('ORG_CONTEXT_MAX_CANDIDATES', 50)),
    'MIN_SCORE': float(os.getenv('ORG_CONTEXT_MIN_SCORE', 0.3)),
    'TOKEN_BUDGETS': {
        'prd': 3000,
        'ux_spec': 4000,
        'architecture': 2500,
        'implementation': 3000,
        'default': 3000,
    },
    'CANONICAL_TAGS': [
        'AUTH', 'BILLING', 'NOTIFICATIONS', 'ANALYTICS',
        'ONBOARDING', 'SETTINGS', 'DASHBOARD', 'REPORTING',
        'COLLABORATION', 'INTEGRATIONS', 'ADMIN', 'USER_MANAGEMENT',
        'SEARCH', 'MESSAGING', 'SCHEDULING', 'WORKFLOW',
    ],
    'COMPLETENESS_THRESHOLDS': {
        'minimal': 0.3,
        'basic': 0.5,
        'good': 0.7,
        'excellent': 0.9,
    },
}

EVIDENCE_MATRIX_CONFIG = {
    'ACTIVE_WEIGHT_PROFILE': os.getenv('EVIDENCE_MATRIX_WEIGHT_PROFILE', 'balanced_v1'),
    'OCCUPATION_PRIOR_POLICY': os.getenv('EVIDENCE_MATRIX_OCCUPATION_PRIOR_POLICY', 'direct_and_ancestor'),
    'OCCUPATION_PRIOR_LIMIT': int(os.getenv('EVIDENCE_MATRIX_OCCUPATION_PRIOR_LIMIT', '2')),
    'OCCUPATION_PRIOR_DISTANCE_DECAY': float(os.getenv('EVIDENCE_MATRIX_OCCUPATION_PRIOR_DISTANCE_DECAY', '0.82')),
    'WEIGHT_PROFILES': deepcopy(DEFAULT_WEIGHT_PROFILES),
}

# ---------------------------------------------------------------------------
# Blackboard streaming
# ---------------------------------------------------------------------------

BLACKBOARD_STREAMABLE_PREFIXES = (
    'org.',
)


# ---------------------------------------------------------------------------
# Stripe / Billing
# ---------------------------------------------------------------------------

_is_production = ENVIRONMENT == 'production'

STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY') or (
    os.getenv('STRIPE_LIVE_SECRET_KEY', '') if _is_production
    else os.getenv('STRIPE_TEST_SECRET_KEY', '')
)
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY') or (
    os.getenv('STRIPE_LIVE_PUBLISHABLE_KEY', '') if _is_production
    else os.getenv('STRIPE_TEST_PUBLISHABLE_KEY', '')
)
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET') or (
    os.getenv('STRIPE_LIVE_WEBHOOK_SECRET', '') if _is_production
    else os.getenv('STRIPE_TEST_WEBHOOK_SECRET', '')
)

STRIPE_TEST_MODE = STRIPE_SECRET_KEY.startswith('sk_test_') if STRIPE_SECRET_KEY else True

FRONTEND_URL = os.getenv('FRONTEND_URL', '')
if not FRONTEND_URL:
    FRONTEND_URL = 'https://localhost:3000' if DEBUG else ''

FREE_ORG_FEATURE_CHATS = int(os.getenv('FREE_ORG_FEATURE_CHATS', '1'))
DEFAULT_MONTHLY_FEATURE_CHATS = int(os.getenv('DEFAULT_MONTHLY_FEATURE_CHATS', '5'))
DEFAULT_MAX_FREE_MEMBERS = int(os.getenv('DEFAULT_MAX_FREE_MEMBERS', '5'))
DEFAULT_CURRENCY = os.getenv('DEFAULT_CURRENCY', 'usd')


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

try:
    from server.observability.sentry_config import init_sentry
    init_sentry()
except Exception as _e:
    print(f'WARNING: Sentry init skipped: {_e}')

try:
    from server.observability.logging_config import configure as _configure_logging
    _configure_logging()
except Exception as _e:
    print(f'WARNING: Logging config fallback to defaults: {_e}')
