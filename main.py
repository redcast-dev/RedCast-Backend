from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from downloader import get_video_info, stream_media, download_subtitles
from security import setup_security
import os
import logging
import traceback

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Strict CORS configuration
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
CORS(app, origins=[FRONTEND_URL])

# Setup rate limiting and security headers
try:
    setup_security(app)
    logger.info("Security and Rate Limiting initialized.")
except Exception as e:
    logger.error(f"Failed to initialize security: {e}")

@app.route("/")
def index():
    return jsonify({
        "message": "RedCast API is running",
        "endpoints": {
            "info": "/api/info (POST)",
            "download": "/api/download (GET)",
            "subtitles": "/api/subtitles (GET)",
            "health": "/api/health (GET)"
        }
    }), 200

@app.route("/api/info", methods=["POST"])
def video_info():
    """Get video information"""
    data = request.json
    url = data.get("url")
    
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    try:
        info = get_video_info(url)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["GET"])
def download():
    """
    True streaming download endpoint.
    Usage: /api/download?url=...&quality=...&mode=...
    """
    url = request.args.get("url")
    quality = request.args.get("quality", "1080")
    mode = request.args.get("mode", "video")

    if not url:
        return "URL is required", 400

    try:
        generator, filename, content_type = stream_media(url, quality, mode)
        
        return Response(
            stream_with_context(generator),
            mimetype=content_type,
            headers={
                "Content-Disposition": f"attachment; filename=\"{filename}\"",
                "Access-Control-Expose-Headers": "Content-Disposition"
            }
        )
    except Exception as e:
        error_msg = traceback.format_exc()
        logger.error(f"Download Error: {str(e)}\n{error_msg}")
        # Always return error message for debugging, but only traceback if DEBUG is set
        return jsonify({
            "error": str(e),
            "traceback": error_msg if os.getenv("DEBUG", "true").lower() == "true" else "Traceback hidden. Set DEBUG=true to see it."
        }), 500

@app.route("/api/subtitles", methods=["GET"])
def subtitles():
    """Download subtitles"""
    url = request.args.get("url")
    lang = request.args.get("lang", "en")
    
    if not url:
        return "URL is required", 400

    try:
        content, filename = download_subtitles(url, lang)
        return Response(
            content,
            mimetype="text/plain",
            headers={
                "Content-Disposition": f"attachment; filename=\"{filename}\"",
                "Content-Security-Policy": "default-src 'self'; script-src 'none'; object-src 'none';"
            }
        )
    except Exception as e:
        error_msg = traceback.format_exc()
        logger.error(f"Subtitle Error: {str(e)}\n{error_msg}")
        return jsonify({
            "error": str(e),
            "traceback": error_msg if os.getenv("DEBUG", "true").lower() == "true" else None
        }), 500

@app.route("/api/health")
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    # For local development
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
