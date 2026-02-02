import yt_dlp
import os
import subprocess
import json
import re

# Use environment variables or defaults
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", 1800)) # 30 minutes

def _get_best_video_format_id(info, target_height):
    """
    Manually analyze formats to find the EXACT resolution requested.
    Returns: explicit format_id string for video
    """
    formats = info.get('formats', [])
    video_formats = [
        f for f in formats 
        if f.get('vcodec') != 'none' and f.get('height') is not None
    ]
    
    if not video_formats:
        return None
        
    target_height = int(target_height)
    exact_matches = [f for f in video_formats if f['height'] == target_height]
    
    if exact_matches:
        def score_format(f):
            score = 0
            vcodec = f.get('vcodec', '')
            if 'avc1' in vcodec or 'h264' in vcodec:
                score += 10000 
            elif 'vp9' in vcodec or 'av01' in vcodec:
                score -= 5000
            tbr = f.get('tbr') or 0
            score += tbr
            return score
            
        best = max(exact_matches, key=score_format)
        return best['format_id']
        
    candidates_below = [f for f in video_formats if f['height'] < target_height]
    if candidates_below:
        def score_fallback(f):
            score = (f.get('height') or 0) * 100
            if 'avc1' in f.get('vcodec', '') or 'h264' in f.get('vcodec', ''):
                score += 50000
            return score
        best = max(candidates_below, key=score_fallback)
        return best['format_id']
        
    return 'bestvideo[vcodec^=avc1]'

def get_video_info(url):
    ydl_opts = {
        'skip_download': True, 
        'quiet': True,
        'noplaylist': True, # Strictly no playlists
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise Exception(f"Error fetching video info: {str(e)}")

        if 'entries' in info:
            raise Exception("Playlist downloads are disabled.")

        duration = info.get('duration', 0)
        if duration > MAX_DURATION:
            raise Exception(f"Video is too long ({duration}s). Max allowed is {MAX_DURATION}s.")

        return {
            'type': 'video',
            'title': info.get('title', 'Unknown'),
            'duration': duration,
            'thumbnail': info.get('thumbnail', ''),
            'has_subtitles': bool(info.get('subtitles') or info.get('automatic_captions'))
        }

def stream_media(url, quality, mode):
    """
    Generates a stream of data using ffmpeg pipe.
    """
    # 1. Fetch info and check constraints
    info = get_video_info(url)
    
    ydl_opts_info = {'skip_download': True, 'quiet': True, 'no_warnings': True, 'noplaylist': True}
    target_height = quality if quality else '1080'
    ext = "mp4"
    content_type = "video/mp4"

    # Get fresh info with formats
    with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
        info_full = ydl.extract_info(url, download=False)

    video_url = None
    audio_url = None
    selected_video_fmt = None
    
    if mode == "video":
        target_height_int = int(target_height) if str(target_height).isdigit() else 1080
        video_id = _get_best_video_format_id(info_full, target_height_int)
        formats = info_full.get('formats', [])
        
        if video_id and video_id != 'bestvideo':
            selected_video_fmt = next((f for f in formats if f['format_id'] == video_id), None)
            
        if not selected_video_fmt:
            candidates = [f for f in formats if f.get('vcodec') != 'none' and f.get('height')]
            if candidates:
                def stream_score(f):
                    score = (f.get('height') or 0)
                    if 'avc1' in f.get('vcodec', '') or 'h264' in f.get('vcodec', ''):
                        score += 50000 
                    return score
                selected_video_fmt = max(candidates, key=stream_score) 
            
        if selected_video_fmt:
            video_url = selected_video_fmt['url']
        
        audio_candidates = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if audio_candidates:
            aac_candidates = [f for f in audio_candidates if 'mp4a' in f.get('acodec', '')]
            if aac_candidates:
                 best_audio = max(aac_candidates, key=lambda f: (f.get('tbr') or 0))
            else:
                 best_audio = max(audio_candidates, key=lambda f: (f.get('tbr') or 0))
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

    # Use 'ffmpeg' command directly (installed via apt in Docker)
    ffmpeg_binary = "ffmpeg"
    
    input_args = []
    map_args = []
    codec_args = []
    
    network_args = [
        '-reconnect', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',
        '-multiple_requests', '1',
        '-thread_queue_size', '8192'
    ]
    
    if mode == "video":
        if video_url and audio_url:
             input_args.extend(network_args + ['-i', video_url])
             input_args.extend(network_args + ['-i', audio_url])
             map_args.extend(['-map', '0:v:0', '-map', '1:a:0'])
             codec_args.extend(['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k'])
        elif video_url:
             input_args.extend(network_args + ['-i', video_url])
             codec_args = ['-c', 'copy']
        else:
             raise Exception("Could not find suitable video stream")
        
        output_args = [
            '-f', 'mp4',
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            '-bsf:v', 'dump_extra',
            'pipe:1'
        ]
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
    
    return (_stream_subprocess(full_cmd), filename, content_type)

def _stream_subprocess(cmd):
    """Yields bytes from a subprocess stdout"""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, 
        bufsize=1024*1024 
    )
    try:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        process.stdout.close()
        process.stderr.close()
        process.terminate()

def download_subtitles(url, lang='en'):
    """Simplified subtitle download (will save to temp and return bytes or handle differently)"""
    # For now, we can keep it similar but remove the local bin paths
    import glob
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [lang],
            'subtitlesformat': 'srt',
            'outtmpl': f'{tmpdir}/%(title)s.%(ext)s',
            'quiet': True,
            'noplaylist': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            files = glob.glob(f"{tmpdir}/*.srt")
            if not files:
                raise Exception("No subtitles found.")
            
            with open(files[0], 'rb') as f:
                content = f.read()
            
            return content, os.path.basename(files[0])
