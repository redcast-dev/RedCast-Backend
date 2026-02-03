import yt_dlp
import os
import subprocess
import json
import re
import time
import logging
from pathlib import Path

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
        # Extractor args for YouTube specifically - 'ios' is often more resilient in data centers
        'extractor_args': {
            'youtube': {
                'player_client': ['ios'],
                'player_skip': ['webpage', 'configs'],
                'skip': ['hls', 'dash'],
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
        return None
        
    target_height = int(target_height)
    # Check for exact matches first
    exact_matches = [f for f in video_formats if f['height'] == target_height]
    
    if exact_matches:
        def score_format(f):
            score = 0
            vcodec = (f.get('vcodec') or '').lower()
            
            # For 4K/2K, we MUST use VP9/AV1 for true quality as H264 often doesn't exist or is poor
            if target_height >= 1440:
                if 'av01' in vcodec: # AV1 is best quality but hardest to decode
                    score += 15000
                elif 'vp9' in vcodec or 'vp09' in vcodec: # VP9 is standard for high quality YT
                    score += 10000
                elif 'avc' in vcodec or 'h264' in vcodec:
                    score += 5000
            else:
                # For 1080p and below, prefer H.264 for maximum compatibility
                if 'avc' in vcodec or 'h264' in vcodec:
                    score += 15000
                elif 'vp9' in vcodec or 'vp09' in vcodec:
                    score += 10000
            
            # Bitrate helps choose the "cleanest" version
            score += (f.get('tbr') or 0)
            return score
            
        best = max(exact_matches, key=score_format)
        return best['format_id']
        
    # Fallback to closest resolution BELOW target if exact not found
    candidates = sorted(video_formats, key=lambda f: abs(f['height'] - target_height))
    if candidates:
        return candidates[0]['format_id']
        
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

def _stream_subprocess(process):
    """Yields bytes from a subprocess stdout"""
    try:
        while True:
            chunk = process.stdout.read(128 * 1024) # Increased chunk size
            if not chunk:
                break
            yield chunk
        
        if process.poll() is not None and process.returncode != 0:
            stderr = process.stderr.read().decode('utf-8', errors='replace')
            logger.error(f"FFmpeg process failed with code {process.returncode}: {stderr}")
    except Exception as e:
        logger.error(f"Streaming yield error: {e}")
    finally:
        try:
            process.stdout.close()
            process.stderr.close()
            process.terminate()
            process.wait(timeout=1)
        except Exception as e:
            logger.debug(f"Process cleanup error: {e}")

def stream_media(url, quality, mode):
    """
    Generates a stream of data using ffmpeg pipe.
    """
    # 1. Fetch info
    ydl_opts_info = get_ydl_base_opts()
    ydl_opts_info.update({'skip_download': True})
    
    target_height = quality if quality else '1080'
    
    with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
        try:
            info_full = ydl.extract_info(url, download=False)
        except Exception as e:
            raise Exception(f"Failed to extract video info: {str(e)}")

    video_url = None
    audio_url = None
    selected_video_fmt = None
    
    mode = mode.lower()
    is_video = "video" in mode or "vid" == mode or "webm" in mode

    if is_video:
        ext = "webm" if "webm" in mode else "mp4"
        content_type = "video/webm" if ext == "webm" else "video/mp4"
        target_height_int = int(target_height) if str(target_height).isdigit() else 1080
        video_id = _get_best_video_format_id(info_full, target_height_int)
        formats = info_full.get('formats', [])
        
        if video_id:
            selected_video_fmt = next((f for f in formats if f['format_id'] == video_id), None)
            
        if not selected_video_fmt:
            # Last resort fallback
            candidates = [f for f in formats if f.get('vcodec') != 'none' and f.get('height')]
            if candidates:
                selected_video_fmt = max(candidates, key=lambda f: (f.get('height') or 0)) 
            
        if selected_video_fmt:
            video_url = selected_video_fmt['url']
        
        # Audio selection
        audio_candidates = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if audio_candidates:
            if ext == "webm":
                # For WebM, opus/vorbis is better
                opus_candidates = [f for f in audio_candidates if 'opus' in f.get('acodec', '')]
                best_audio = max(opus_candidates if opus_candidates else audio_candidates, key=lambda f: (f.get('tbr') or 0))
            else:
                # For MP4, AAC is best
                aac_candidates = [f for f in audio_candidates if 'mp4a' in f.get('acodec', '')]
                best_audio = max(aac_candidates if aac_candidates else audio_candidates, key=lambda f: (f.get('tbr') or 0))
            audio_url = best_audio['url']
    else:
        ext = "mp3"
        content_type = "audio/mpeg"
        audio_candidates = [f for f in info_full.get('formats', []) if f.get('acodec') != 'none']
        if audio_candidates:
            best_audio = max(audio_candidates, key=lambda f: (f.get('tbr') or 0))
            audio_url = best_audio['url']
            
    title = info_full.get('title', 'video')
    safe_title = re.sub(r'[^\w\-_\. ]', '_', title)[:200]
    filename = f"{safe_title}.{ext}"

    ffmpeg_binary = "ffmpeg"
    input_args = []
    map_args = []
    codec_args = []
    
    # Enhanced network args for stability
    network_args = [
        '-reconnect', '1',
        '-reconnect_at_eof', '1', # Critical for handling quick disconnections
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '10',
        '-multiple_requests', '1',
        '-thread_queue_size', '16384', # Even larger queue
        '-err_detect', 'ignore_err' # Help skip minor packet corruption
    ]
    
    if is_video:
        if video_url and audio_url:
             input_args.extend(['-probesize', '32M', '-analyzeduration', '10M'])
             input_args.extend(network_args + ['-i', video_url])
             input_args.extend(network_args + ['-i', audio_url])
             map_args.extend(['-map', '0:v:0', '-map', '1:a:0'])
             
             if ext == "webm":
                 codec_args.extend(['-c:v', 'copy', '-c:a', 'libopus', '-b:a', '192k'])
             else:
                 # Logic for MP4: if source is already compatible, copy.
                 # If source is VP9/AV1, we still copy to MP4 (supported by many) but add better flags.
                 codec_args.extend(['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k'])
        elif video_url:
             input_args.extend(network_args + ['-i', video_url])
             codec_args = ['-c', 'copy']
        else:
             raise Exception("Could not find suitable video stream")
        
        if ext == "mp4":
            output_args = [
                '-f', 'mp4',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof+global_sidx',
                '-bsf:v', 'dump_extra',
                '-brand', 'mp42',
                'pipe:1'
            ]
        else:
            output_args = ['-f', 'webm', '-dash', '1', 'pipe:1']
    else:
        target_url = audio_url if audio_url else video_url
        if not target_url:
            raise Exception("No suitable stream found")
        input_args.extend(network_args + ['-i', target_url])
        bitrate = '192k'
        if '320' in mode: bitrate = '320k'
        elif '128' in mode: bitrate = '128k'
        elif '64' in mode: bitrate = '64k'
        codec_args = ['-vn', '-c:a', 'libmp3lame', '-b:a', bitrate]
        output_args = ['-f', 'mp3', 'pipe:1']
        
    full_cmd = [ffmpeg_binary, '-hide_banner', '-loglevel', 'error'] + input_args + map_args + codec_args + output_args
    
    # Start process
    process = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, 
        bufsize=2*1024*1024 # 2MB buffer
    )
    
    time.sleep(0.5)
    if process.poll() is not None:
        stderr = process.stderr.read().decode('utf-8', errors='replace')
        raise Exception(f"FFmpeg failed to start: {stderr}")

    return (_stream_subprocess(process), filename, content_type)

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
