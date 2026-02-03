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

    # --- Cookie handling for bot / sign-in checks ---
    # If you provide a cookie file path via YT_COOKIES_FILE, yt-dlp will use it.
    cookie_file = os.getenv("YT_COOKIES_FILE")
    if cookie_file:
        # This is equivalent to using --cookies on the yt-dlp CLI
        opts["cookiefile"] = cookie_file
        logger.info(f"Using YouTube cookies from file: {cookie_file}")
    else:
        # Optional: allow using cookies from a local browser when running
        # the backend on a desktop (not recommended/usable on Railway).
        browser = os.getenv("YT_COOKIES_FROM_BROWSER")
        if browser:
            # e.g. YT_COOKIES_FROM_BROWSER=chrome or firefox
            opts["cookiesfrombrowser"] = (browser,)
            logger.info(f"Using YouTube cookies from browser: {browser}")

    logger.info("Using enhanced anti-bot configuration")
    
    return opts

def _choose_video_and_audio_formats(info, target_height: int, prefer_webm: bool = False):
    """
    Pick concrete video+audio formats for the requested height.

    Strategy:
      1. Try exact height match (height == target_height).
      2. Then closest higher resolution (min height > target_height).
      3. Finally closest lower resolution (max height < target_height).

    We optionally prefer WebM streams when requested, but fall back to any
    container if necessary so we never drop unnecessarily to a low resolution.
    """
    formats = info.get('formats', [])
    video_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('height')]
    audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']

    if not video_formats:
        logger.warning("No video formats found for this video.")
        return None, None

    target_height = int(target_height)

    def score_video(f):
        # Higher bitrate & newer codecs get higher score at the same height
        score = 0
        vcodec = (f.get('vcodec') or '').lower()
        if 'av01' in vcodec or 'av1' in vcodec:
            score += 300
        elif 'vp9' in vcodec or 'vp09' in vcodec:
            score += 200
        elif 'avc' in vcodec or 'h264' in vcodec:
            score += 150
        score += (f.get('tbr') or 0) / 10.0
        return score

    # Apply container preference if needed
    def filter_by_container(vfs):
        if prefer_webm:
            webm = [f for f in vfs if (f.get('ext') or '').lower() == 'webm']
            if webm:
                return webm
        return vfs

    video_formats = filter_by_container(video_formats)

    exact = [f for f in video_formats if f.get('height') == target_height]
    above = [f for f in video_formats if f.get('height') and f['height'] > target_height]
    below = [f for f in video_formats if f.get('height') and f['height'] < target_height]

    chosen_v = None
    if exact:
        # Best score among exact height candidates
        chosen_v = max(exact, key=score_video)
    elif below:
        # Prefer the closest LOWER resolution to truly respect the user's choice
        max_height = max(f['height'] for f in below)
        closest_below = [f for f in below if f['height'] == max_height]
        chosen_v = max(closest_below, key=score_video)
    elif above:
        # Only if nothing <= target exists, go to the smallest higher resolution
        min_height = min(f['height'] for f in above)
        closest_above = [f for f in above if f['height'] == min_height]
        chosen_v = max(closest_above, key=score_video)

    if not chosen_v:
        logger.warning("Falling back to absolute best available video format.")
        chosen_v = max(video_formats, key=score_video)

    # Choose audio: prefer Opus/Vorbis for WebM, AAC/M4A for MP4, else best tbr
    chosen_a = None
    if audio_formats:
        if prefer_webm:
            opus = [a for a in audio_formats if 'opus' in (a.get('acodec') or '') or 'vorbis' in (a.get('acodec') or '')]
            if opus:
                chosen_a = max(opus, key=lambda a: (a.get('tbr') or 0))
        else:
            aac = [a for a in audio_formats if 'mp4a' in (a.get('acodec') or '') or 'aac' in (a.get('acodec') or '')]
            if aac:
                chosen_a = max(aac, key=lambda a: (a.get('tbr') or 0))

        if not chosen_a:
            chosen_a = max(audio_formats, key=lambda a: (a.get('tbr') or 0))

    logger.info(
        f"Chosen video format: id={chosen_v.get('format_id')} "
        f"height={chosen_v.get('height')} codec={chosen_v.get('vcodec')}"
    )
    if chosen_a:
        logger.info(
            f"Chosen audio format: id={chosen_a.get('format_id')} "
            f"codec={chosen_a.get('acodec')} bitrate={chosen_a.get('tbr')}kbps"
        )

    v_id = chosen_v.get('format_id')
    a_id = chosen_a.get('format_id') if chosen_a else None
    return v_id, a_id


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

def _build_yt_dlp_options_for_mode(info, quality, mode):
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
        # Video modes – we now select explicit format IDs using the probed
        # metadata so that we are in full control of the chosen resolution.
        prefer_webm = is_webm
        v_id, a_id = _choose_video_and_audio_formats(info, q, prefer_webm=prefer_webm)
        if not v_id:
            raise Exception("Unable to choose a suitable video stream for the requested quality.")

        if a_id:
            fmt = f"{v_id}+{a_id}"
        else:
            fmt = v_id

        if is_webm:
            opts.update({
                'format': fmt,
                'merge_output_format': 'webm',
            })
            ext = "webm"
            content_type = "video/webm"
        else:
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

    # First, probe video info so we can choose exact formats for the target height.
    probe_opts = get_ydl_base_opts()
    probe_opts.update({'skip_download': True})

    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.error(f"Failed to probe video info for {url}: {e}")
            raise Exception(f"Failed to extract video info: {str(e)}")

    # Configure yt-dlp based on mode/quality and the probed info
    ydl_opts, ext, content_type = _build_yt_dlp_options_for_mode(info, quality, mode)

    # Create a temp directory to hold the downloaded file(s)
    tmpdir = tempfile.mkdtemp(prefix="redcast_")

    # Use the original video title as filename; yt-dlp will also sanitize it.
    # We still scope it to the temp directory so multiple downloads don't clash.
    title = info.get('title') or 'video'
    # yt-dlp will sanitize invalid characters; we just provide a pattern.
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
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

    # Locate the actual output file matching our expected extension
    files = glob.glob(os.path.join(tmpdir, f"*.{ext}"))
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

    # Build a header-safe download filename based on the original title.
    # Many servers (and Gunicorn itself) reject non-ASCII or control
    # characters in header values, so we aggressively sanitize here while
    # still keeping something readable for the user.
    raw_title = info.get('title') or Path(filepath).stem
    safe_title = re.sub(r'[^A-Za-z0-9_\-\. ]+', '_', raw_title).strip()
    if not safe_title:
        safe_title = "video"
    # Keep filename reasonably short for headers
    safe_title = safe_title[:180]
    filename = f"{safe_title}.{ext}"

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
