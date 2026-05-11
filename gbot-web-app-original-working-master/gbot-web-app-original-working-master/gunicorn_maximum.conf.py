# Gunicorn configuration for MAXIMUM resource utilization
# 4 vCPU, 16GB RAM server - PUSH TO THE LIMIT
import multiprocessing
import os

# Server socket - MAXIMUM for high load
bind = "127.0.0.1:5000"
backlog = 8192  # Maximum backlog for extreme concurrency

# Worker processes - MAXIMUM for 4 vCPU, 16GB RAM
workers = 16  # 4x CPU cores for maximum performance
worker_class = "sync"
worker_connections = 5000  # Maximum connections per worker
max_requests = 20000  # Very high before restart
max_requests_jitter = 500

# Timeouts - Optimized for high load
timeout = 1200  # 20 minutes for very long operations
keepalive = 10  # Keep connections alive much longer
graceful_timeout = 120  # More time for graceful shutdown

# Memory management - MAXIMUM
preload_app = True
max_requests_jitter = 100

# Logging
accesslog = "logs/gunicorn_access.log"
errorlog = "logs/gunicorn_error.log"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'gbot_web_app_maximum'

# Security - Relaxed for maximum performance
limit_request_line = 8192
limit_request_fields = 200
limit_request_field_size = 16384

# Performance tuning - MAXIMUM
worker_tmp_dir = "/dev/shm"  # Use shared memory for worker temp files

# Environment variables
raw_env = [
    'FLASK_ENV=production',
    'PYTHONPATH=/opt/gbot-web-app',
    'PYTHONUNBUFFERED=1',
    'OMP_NUM_THREADS=4',  # Use all CPU cores
]

# Pre-fork optimization
def when_ready(server):
    server.log.info("Server is ready. Spawning %d workers for maximum performance", server.cfg.workers)

def worker_int(worker):
    worker.log.info("worker received INT or QUIT signal")

def pre_fork(server, worker):
    server.log.info("Worker spawned (pid: %s)", worker.pid)

def post_fork(server, worker):
    server.log.info("Worker spawned (pid: %s)", worker.pid)

def worker_abort(worker):
    worker.log.info("worker received SIGABRT signal")

# Additional performance settings
def on_starting(server):
    server.log.info("Starting GBot Web App with MAXIMUM resource utilization")

def on_reload(server):
    server.log.info("Reloading GBot Web App")

def when_ready(server):
    server.log.info("GBot Web App is ready with %d workers", server.cfg.workers)

def worker_exit(server, worker):
    server.log.info("Worker exited (pid: %s)", worker.pid)
