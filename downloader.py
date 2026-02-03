import yt_dlp
import os
import subprocess
import json
import re
import time
import logging
from pathlib import Path
import tempfile
import glob

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Use environment variables or defaults
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", 1800)) # 30 minutes

# Enhanced yt-dlp options to bypass bot detection
def get_ydl_base_opts():
    """
    Returns base yt-dlp options with anti-bot measures.
    This includes cookie extraction from browser and user-agent spoofing.
    """
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        # User-Agent spoofing - use iPhone to match the ios player client
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1',
        # Additional headers to appear more like a mobile device
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
        # Retry and timeout settings
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'socket_timeout': 30,
        # Extractor args for YouTube specifically
        # - Avoid 'web' client to prevent "Sign in" errors
        # - Use 'android' and 'ios' for best compatibility
        # - Do NOT skip HLS/DASH as they are needed for these clients
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios'],
                'player_skip': ['webpage', 'configs'],
            }
        },
    }
    
    # Note: Cookie extraction is disabled by default because it often fails when browsers are running
    # If you need cookie authentication, you can:
    # 1. Close all browser instances before running
    # 2. Manually export cookies to a file and use 'cookiefile' option
    # 3. Sign in to YouTube in a browser and the session may persist
    
    # Uncomment below to enable cookie extraction (may cause errors if browser is running):
    # opts['cookiesfrombrowser'] = ('chrome',)  # or 'firefox', 'edge', etc.
    
    logger.info("Using enhanced anti-bot configuration (without cookie extraction)")
    
    return opts

def _get_best_video_format_id(info, target_height):
    """
    Manually analyze formats to find the BEST version of the EXACT resolution requested.
    """
    formats = info.get('formats', [])
    video_formats = [
        f for f in formats 
        if f.get('vcodec') != 'none' and f.get('height') is not None
    ]
    
    if not video_formats:
        logger.warning("No video formats found")
        return None
        
    target_height = int(target_height)
    # Check for exact matches first
    exact_matches = [f for f in video_formats if f['height'] == target_height]
    
    if exact_matches:
        def score_format(f):
            score = 0
            vcodec = (f.get('vcodec') or '').lower()
            
            # Prefer VP9/AV1 for high quality (we'll re-encode to H.264 if needed)
            if 'av01' in vcodec:
                score += 15000
            elif 'vp9' in vcodec or 'vp09' in vcodec:
                score += 10000
            elif 'avc' in vcodec or 'h264' in vcodec:
                score += 8000
            
            # Bitrate is critical for quality
            score += (f.get('tbr') or 0)
            return score
            
        best = max(exact_matches, key=score_format)
        logger.info(f"Selected exact match: {target_height}p, codec: {best.get('vcodec')}, bitrate: {best.get('tbr')}kbps")
        return best['format_id']
        
    # Fallback: prefer highest quality available (not closest)
    logger.warning(f"No exact match for {target_height}p, selecting best available quality")
    best_available = max(video_formats, key=lambda f: (f.get('height') or 0, f.get('tbr') or 0))
    logger.info(f"Selected fallback: {best_available.get('height')}p, codec: {best_available.get('vcodec')}, bitrate: {best_available.get('tbr')}kbps")
    return best_available['format_id']
        
    return None


def get_video_info(url):
    ydl_opts = get_ydl_base_opts()
    ydl_opts.update({
        'skip_download': True,
        'noplaylist': False,  # Enable playlist analysis
        'extract_flat': True,  # Don't extract full details for every video in playlist yet (too slow)
    })
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.error(f"yt-dlp error: {e}")
            raise Exception(f"Error fetching video info: {str(e)}")

        if 'entries' in info:
            # It's a playlist
            entries = list(info['entries'])
            return {
                'type': 'playlist',
                'title': info.get('title', 'Unknown Playlist'),
                'count': len(entries),
                'videos': [
                    {'url': f"https://www.youtube.com/watch?v={entry['id']}", 'title': entry.get('title', 'Unknown')}
                    for entry in entries if entry.get('id')
                ]
            }

        # It's a single video
        duration = info.get('duration', 0)
        # No limitation check
        
        return {
            'type': 'video',
            'title': info.get('title', 'Unknown'),
            'duration': duration,
            'thumbnail': info.get('thumbnail', ''),
            'has_subtitles': bool(info.get('subtitles') or info.get('automatic_captions'))
        }

def _build_yt_dlp_options_for_mode(url, quality, mode):
    """
    Build a yt-dlp configuration for the selected mode and quality.
    This lets yt-dlp (and its internal ffmpeg calls) handle all merging
    and container details, which is far more reliable than manually
    piping via ffmpeg.
    """
    base_opts = get_ydl_base_opts()
    q = int(quality) if str(quality).isdigit() else 1080
    mode = (mode or "video").lower()

    # Temporary directory & filename pattern; actual temp dir is injected later.
    opts = {
        **base_opts,
        'noplaylist': True,
    }

    is_audio = mode.startswith("audio")
    is_webm = "webm" in mode

    if is_audio:
        # Audio-only: use best audio and convert to MP3 with requested bitrate.
        bitrate = "192"
        if "320" in mode:
            bitrate = "320"
        elif "128" in mode:
            bitrate = "128"
        elif "64" in mode:
            bitrate = "64"

        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': bitrate,
            }],
            'preferredquality': bitrate,
        })
        ext = "mp3"
        content_type = "audio/mpeg"
    else:
        # Video modes
        if is_webm:
            # Prefer VP9/Opus WebM, capped by height
            fmt = (
                f"bestvideo[height<={q}][ext=webm]+bestaudio[ext=webm]/"
                f"best[height<={q}][ext=webm]/best[ext=webm]/best"
            )
            opts.update({
                'format': fmt,
                'merge_output_format': 'webm',
            })
            ext = "webm"
            content_type = "video/webm"
        else:
            # Default: MP4 video – constrain by height and prefer mp4 container.
            fmt = (
                f"bestvideo[height<={q}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={q}][ext=mp4]/best[ext=mp4]/"
                f"bestvideo[height<={q}]+bestaudio/best"
            )
            opts.update({
                'format': fmt,
                'merge_output_format': 'mp4',
            })
            ext = "mp4"
            content_type = "video/mp4"

    return opts, ext, content_type


def stream_media(url, quality, mode):
    """
    Download media with yt-dlp into a temporary file, then stream that file
    back to the client in chunks. This avoids fragile ffmpeg piping and
    dramatically reduces the risk of 0-byte or corrupted outputs.
    """
    quality = quality or "1080"

    # Configure yt-dlp based on mode/quality
    ydl_opts, ext, content_type = _build_yt_dlp_options_for_mode(url, quality, mode)

    # Create a temp directory to hold the downloaded file(s)
    tmpdir = tempfile.mkdtemp(prefix="redcast_")
    outtmpl = os.path.join(tmpdir, "download.%(ext)s")
    ydl_opts['outtmpl'] = outtmpl

    logger.info(f"Starting yt-dlp download for URL={url}, quality={quality}, mode={mode}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        # Cleanup temp directory on failure
        try:
            for f in glob.glob(os.path.join(tmpdir, "*")):
                os.remove(f)
            os.rmdir(tmpdir)
        except Exception:
            pass
        logger.error(f"yt-dlp download failed: {e}")
        raise Exception(f"Download failed: {str(e)}")

    # Locate the actual output file
    files = glob.glob(os.path.join(tmpdir, "download.*"))
    if not files:
        # Fallback: try to infer from info
        logger.error("No output files found after yt-dlp download.")
        try:
            for f in glob.glob(os.path.join(tmpdir, "*")):
                os.remove(f)
            os.rmdir(tmpdir)
        except Exception:
            pass
        raise Exception("Internal error: no downloaded file found.")

    filepath = files[0]
    filename = os.path.basename(filepath)

    def file_generator(path, directory):
        """Yield file contents in chunks, then clean up temp files."""
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)  # 1 MB
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
            try:
                # Remove any stray files and the temp directory itself
                for f in glob.glob(os.path.join(directory, "*")):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
                os.rmdir(directory)
            except Exception:
                pass

    return (file_generator(filepath, tmpdir), filename, content_type)

def download_subtitles(url, lang='en'):
    import glob
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = get_ydl_base_opts()
        ydl_opts.update({
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [lang],
            'subtitlesformat': 'srt',
            'outtmpl': f'{tmpdir}/%(title)s.%(ext)s',
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                ydl.extract_info(url, download=True)
            except Exception as e:
                raise Exception(f"Subtitle download failed: {str(e)}")
                
            files = glob.glob(f"{tmpdir}/*.srt")
            if not files:
                raise Exception("No subtitles found.")
            
            with open(files[0], 'rb') as f:
                content = f.read()
            
            return content, os.path.basename(files[0])
