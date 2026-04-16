# File location: /server/gunicorn.conf.py
"""Production Gunicorn configuration for Mula AI server."""
import multiprocessing
import os


# --- Bind & network ---
bind = os.getenv('WEB_BIND', '0.0.0.0:8000')
backlog = int(os.getenv('WEB_BACKLOG', '2048'))

# --- Workers ---
_default_workers = multiprocessing.cpu_count() * 2 + 1
workers = int(os.getenv('WEB_WORKERS', str(_default_workers)))
threads = int(os.getenv('WEB_THREADS', '1'))
worker_class = os.getenv('WEB_WORKER_CLASS', 'uvicorn.workers.UvicornWorker')
worker_connections = int(os.getenv('WEB_WORKER_CONNECTIONS', '1000'))

# --- Lifecycle ---
preload_app = os.getenv('WEB_PRELOAD', 'true').lower() == 'true'
timeout = int(os.getenv('WEB_TIMEOUT', '120'))
graceful_timeout = int(os.getenv('WEB_GRACEFUL_TIMEOUT', '30'))
keepalive = int(os.getenv('WEB_KEEPALIVE', '5'))
max_requests = int(os.getenv('WEB_MAX_REQUESTS', '500'))
max_requests_jitter = int(os.getenv('WEB_MAX_REQUESTS_JITTER', '50'))
worker_tmp_dir = os.getenv('WEB_WORKER_TMP_DIR', '/dev/shm')

# --- Request limits ---
limit_request_line = int(os.getenv('WEB_LIMIT_REQUEST_LINE', '4094'))
limit_request_fields = int(os.getenv('WEB_LIMIT_REQUEST_FIELDS', '100'))
limit_request_field_size = int(os.getenv('WEB_LIMIT_REQUEST_FIELD_SIZE', '8190'))
forwarded_allow_ips = os.getenv('WEB_FORWARDED_ALLOW_IPS', '*')

# --- Logging ---
accesslog = os.getenv('WEB_ACCESS_LOG', '-')
errorlog = os.getenv('WEB_ERROR_LOG', '-')
loglevel = os.getenv('WEB_LOG_LEVEL', 'info')


# --- Hooks ---
def when_ready(server):
    """Log when the master process is ready."""
    server.log.info(
        f'[gunicorn] master ready (pid {os.getpid()}), '
        f'workers={workers}, threads={threads}'
    )


def post_fork(server, worker):
    """Log when a worker is forked."""
    server.log.info(f'[gunicorn] worker forked pid={worker.pid}')


def worker_exit(server, worker):
    """Log when a worker exits."""
    server.log.warning(f'[gunicorn] worker exited pid={worker.pid}')
