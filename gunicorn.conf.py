"""
Gunicorn configuration for DTIP production deployment.
SocketIO requires exactly 1 worker (use eventlet/gevent for concurrency).
Scale horizontally with multiple processes + Redis SocketIO adapter.
"""
import os

# Workers: MUST be 1 for SocketIO (threading model)
workers     = 1
worker_class = 'eventlet'
threads     = int(os.environ.get('GUNICORN_THREADS', 4))

# Binding
bind        = f"0.0.0.0:{os.environ.get('PORT', 5000)}"

# Timeouts
timeout          = 120
keepalive        = 5
graceful_timeout = 30

# Logging
accesslog   = '-'
errorlog    = '-'
loglevel    = 'info'
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)s'

# Security
limit_request_line   = 4094
limit_request_fields = 100
