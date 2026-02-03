from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os

def setup_security(app):
    # Use Redis for rate limiting if available (Railway often provides Redis)
    storage_uri = os.getenv("REDIS_URL", "memory://")
    
    try:
        limiter = Limiter(
            get_remote_address,
            app=app,
            default_limits=["1000 per hour", "5000 per day"],
            storage_uri=storage_uri,
            strategy="fixed-window",
        )
    except Exception as e:
        print(f"Redis connection failed ({e}), falling back to memory storage.")
        limiter = Limiter(
            get_remote_address,
            app=app,
            default_limits=["1000 per hour", "5000 per day"],
            storage_uri="memory://",
            strategy="fixed-window",
        )

    @app.after_request
    def add_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'none'; object-src 'none';"
        return response

    return limiter
