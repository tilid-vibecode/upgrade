# File location: /server/server/asgi.py
import os
import sys
import logging

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')

logger = logging.getLogger('server.asgi')

import django
try:
    django.setup()
    logger.info('Django setup completed in asgi.py.')
except Exception as err:
    print(f'CRITICAL: Django setup failed in asgi.py: {err}')
    sys.exit(1)

from django.core.asgi import get_asgi_application
django_asgi_app = get_asgi_application()

try:
    from server.fastapi_main import app as application
    logger.info('FastAPI application imported.')
except Exception as err:
    print(f'CRITICAL: Could not import FastAPI application: {err}')
    sys.exit(1)

application.mount('/', django_asgi_app, name='django')

try:
    from server.observability.tracing import init_tracing
    init_tracing()
except Exception as err:
    logger.warning('Django OTel tracing not configured: %s', err)

logger.info('ASGI application ready. FastAPI serves /api/*, Django handles the rest.')
