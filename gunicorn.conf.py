import os

# Gunicorn configuration
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 2  # Adjust based on Railway tier (Free tier is limited)
threads = 4
timeout = 300  # Longer timeout for streaming/downloads
worker_class = "gthread"
loglevel = "info"
accesslog = "-"
errorlog = "-"
