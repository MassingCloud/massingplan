"""Gunicorn configuration.

Threads rather than processes for the extra concurrency: the work here is a
mixture of CPU (the CPM passes) and blocking database I/O, and a thread pool
handles the second without the memory cost of another interpreter for the first.
"""

import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"

# Two per core plus one is the usual starting point. Overridable because the
# right number depends on the database's connection limit, not on this process.
workers = int(os.getenv("WEB_CONCURRENCY", (multiprocessing.cpu_count() * 2) + 1))
threads = int(os.getenv("WEB_THREADS", "4"))
worker_class = "gthread"

# A two-thousand-activity Monte Carlo run is CPU-bound and can legitimately take
# a while. The default of 30s would kill it mid-simulation and return nothing.
timeout = int(os.getenv("WEB_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# Recycle workers, with jitter so they do not all recycle at once. Bounds any
# slow leak in a native extension without anyone having to find it first.
max_requests = 1000
max_requests_jitter = 100

accesslog = None  # the app logs structured JSON; a second access log is noise
errorlog = "-"
loglevel = os.getenv("MASSINGPLAN_LOG_LEVEL", "info").lower()

# Behind a load balancer, trust its forwarded headers -- but only from the
# addresses given. Trusting `*` lets any client spoof its own source IP, which
# is what the rate limiter and the audit log record.
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
