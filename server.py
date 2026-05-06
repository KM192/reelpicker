#!/usr/bin/env python3
"""
Reelpicker - tool for quick editing of short video clips

Usage: python server.py [path/to/folder/with/videos]
  If folder is not provided, it can be selected in the UI.
"""

import sys
import os
import json
import uuid
import subprocess
import threading
import datetime
import webbrowser
import re
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from socketserver import ThreadingMixIn

PORT = 8000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WIN = sys.platform == 'win32'
_NO_WINDOW = 0x08000000  # Windows: CREATE_NO_WINDOW


def _find_tool(name):
    """Looks for tool next to server.py first, then in PATH."""
    local = os.path.join(SCRIPT_DIR, name + ('.exe' if IS_WIN else ''))
    if os.path.isfile(local):
        return local
    return name  # fallback: PATH


FFMPEG  = _find_tool('ffmpeg')
FFPROBE = _find_tool('ffprobe')

# ─── Global state ────────────────────────────────────────────────────────────

state_lock = threading.Lock()
state = {
    'folder': None,
    'clips': [],               # [{id, filename, duration, modified, width, height}]
    'selections': {},          # filename -> {filename, start_time, enabled}
    'title': '',
    'title2': '',
    'subtitle': '',
    'music': [],               # [{filename, duration}]
    'disabled_day_cards': set(),   # set of date strings 'YYYY-MM-DD'
    'day_card_titles': {},         # date_str -> {'title': str, 'subtitle': str}
    'end_card_title': '',          # custom end-card title ('' = use 'The End')
    'end_card_subtitle': '',       # custom end-card subtitle
    'clip_duration': 3.0,          # seconds each clip is cut to (0.5–10)
}

export_lock   = threading.Lock()
export_status = {'status': 'idle', 'progress': '', 'percent': 0, 'output': ''}
export_cancel = False          # set to True to request cancellation
export_proc   = None           # current long-running subprocess.Popen (merge step)

# Single-client session guard: only the most recently connected tab is active
_session = {'id': None}
_session_lock = threading.Lock()

# HEVC preview cache: filename -> preview_path (or None if transcode failed)
_preview_cache       = {}
_preview_in_progress = set()   # filenames currently being transcoded
_preview_lock        = threading.Lock()


def pregenerate_hevc_previews(folder, clips):
    """Background thread: pre-generate H.264 previews for all HEVC clips that
    don't already have one cached on disk."""
    hevc = [c for c in clips if c.get('codec', '') in ('hevc', 'h265')]
    if not hevc:
        return
    print(f'[preview] Starting pre-generation for {len(hevc)} HEVC clips...')
    for c in hevc:
        ensure_h264_preview(folder, c['filename'])
    print('[preview] All previews ready.')


def ensure_h264_preview(folder, filename):
    """Return path to H.264 preview of a HEVC file, transcoding if needed.
    Returns None on failure or if another thread is already transcoding this file."""
    with _preview_lock:
        if filename in _preview_cache:
            return _preview_cache[filename]
        if filename in _preview_in_progress:
            return None  # another thread is working on it
        _preview_in_progress.add(filename)

    outcome = None
    try:
        preview_dir  = os.path.join(folder, '.preview')
        preview_path = os.path.join(preview_dir, filename)

        if os.path.isfile(preview_path):
            outcome = preview_path
            return preview_path

        try:
            os.makedirs(preview_dir, exist_ok=True)
        except Exception:
            return None

        source = os.path.join(folder, filename)
        _, _, _, _, color_transfer = ffprobe_info(source)
        if is_hdr_transfer(color_transfer):
            if FFMPEG_HAS_ZSCALE:
                vf_args = ['-vf', ('zscale=t=linear:npl=1000,format=gbrpf32le,'
                                   'zscale=p=bt709,tonemap=tonemap=mobius:desat=0,'
                                   'zscale=t=bt709:m=bt709:r=tv,format=yuv420p')]
            else:
                vf_args = ['-vf', 'colorspace=space=bt709:trc=bt709:primaries=bt709:range=mpeg']
            color_args = ['-color_primaries', 'bt709', '-color_trc', 'bt709',
                          '-colorspace', 'bt709', '-color_range', '1']
        else:
            vf_args    = ['-vf', 'format=yuv420p']
            color_args = []
        print(f'  Transcoding HEVC→H264{"  HDR" if is_hdr_transfer(color_transfer) else ""}: {filename}...', flush=True)
        cmd = [
            FFMPEG, '-y', '-i', source,
            *vf_args,
            *_video_enc_args(23),
            *color_args,
            '-c:a', 'aac', '-b:a', '128k',
            preview_path,
        ]
        rc, _, err = run_cmd(cmd, timeout=300)
        if rc != 0:
            print(f'  WARN: Transcoding failed: {filename}')
            return None

        print(f'  H264 preview ready: {filename}')
        outcome = preview_path
        return preview_path
    finally:
        with _preview_lock:
            _preview_cache[filename] = outcome      # None on failure, path on success
            _preview_in_progress.discard(filename)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def run_cmd(cmd, timeout=60, cwd=None):
    kwargs = {'capture_output': True, 'timeout': timeout}
    if cwd:
        kwargs['cwd'] = cwd
    if IS_WIN:
        kwargs['creationflags'] = _NO_WINDOW
    r = subprocess.run(cmd, **kwargs)
    return r.returncode, r.stdout, r.stderr


def ffprobe_info(path):
    """Returns (duration, video_codec_name, width, height, color_transfer).
    color_transfer may be None; HDR clips have e.g. 'smpte2084' or 'arib-std-b67'."""
    try:
        rc, out, _ = run_cmd(
            [FFPROBE, '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', path],
            timeout=30,
        )
        if rc == 0:
            d = json.loads(out)
            codec = None
            duration = 0.0
            width = 0
            height = 0
            color_transfer = None
            for s in d.get('streams', []):
                if s.get('codec_type') == 'video':
                    codec = s.get('codec_name', '').lower() or None
                    width  = s.get('width',  0)
                    height = s.get('height', 0)
                    color_transfer = s.get('color_transfer') or None
                    # Swap for 90°/270° rotation (phone portrait videos stored as landscape).
                    # Newer ffprobe: rotation in side_data_list; older: tags.rotate.
                    rotate = 0
                    for sd in s.get('side_data_list', []):
                        if 'rotation' in sd:
                            try:
                                rotate = int(sd['rotation']) % 360
                            except (ValueError, TypeError):
                                pass
                            break
                    if not rotate:
                        try:
                            rotate = int(s.get('tags', {}).get('rotate', 0) or 0) % 360
                        except (ValueError, TypeError):
                            rotate = 0
                    if rotate in (90, 270):
                        width, height = height, width
                    if 'duration' in s:
                        duration = float(s['duration'])
            if not duration:
                fmt_dur = d.get('format', {}).get('duration')
                if fmt_dur:
                    duration = float(fmt_dur)
            return duration, codec, width, height, color_transfer
    except Exception as e:
        print(f'  ffprobe error for {os.path.basename(path)}: {e}')
    return 0.0, None, 0, 0, None


# HDR color transfer functions that require tone-mapping to SDR
_HDR_TRANSFERS = {'smpte2084', 'arib-std-b67', 'bt2020-10', 'bt2020-12'}

def is_hdr_transfer(color_transfer):
    return bool(color_transfer and color_transfer.lower() in _HDR_TRANSFERS)


def scan_folder(folder):
    """Return sorted list of clip dicts."""
    entries = []
    try:
        for name in os.listdir(folder):
            if name.lower().endswith('.mp4'):
                path = os.path.join(folder, name)
                mtime = os.path.getmtime(path)
                entries.append((mtime, name.lower(), name))
    except Exception as e:
        print(f'Scan error: {e}')
        return []

    entries.sort()  # chronological (mtime), then alpha
    clips = []
    n = len(entries)
    for i, (mtime, _, name) in enumerate(entries):
        path = os.path.join(folder, name)
        print(f'  [{i+1}/{n}] {name} ...', end='', flush=True)
        dur, codec, w, h, color_transfer = ffprobe_info(path)
        hdr = is_hdr_transfer(color_transfer)
        tag = f' [{codec}{"  HDR" if hdr else ""}]' if codec else ''
        print(f' {dur:.1f}s{tag}')
        clips.append({
            'id': i,
            'filename': name,
            'duration': round(dur, 3),
            'codec': codec or '',
            'modified': datetime.datetime.fromtimestamp(mtime).isoformat(),
            'width':  w,
            'height': h,
            'is_hdr': hdr,
        })
    return clips


MUSIC_EXTS = {'.mp3', '.flac', '.wav', '.aac', '.ogg', '.m4a'}

def scan_music(folder):
    """Return list of music track dicts from folder/music/, sorted alphabetically."""
    music_dir = os.path.join(folder, 'music')
    if not os.path.isdir(music_dir):
        return []
    tracks = []
    try:
        names = sorted(n for n in os.listdir(music_dir) if os.path.splitext(n.lower())[1] in MUSIC_EXTS)
    except Exception as e:
        print(f'Music scan error: {e}')
        return []
    for name in names:
        path = os.path.join(music_dir, name)
        dur, _, _, _, _ = ffprobe_info(path)
        tracks.append({'filename': name, 'duration': round(dur, 3)})
        print(f'  Music: {name} ({dur:.1f}s)')
    return tracks


def _clip_cache_path(folder, filename, start_time, clip_duration=3.0):
    """Return deterministic cache path for a cut clip segment.
    Includes source-file mtime and clip_duration in the name → automatic invalidation
    if source changes or clip duration changes."""
    cache_dir = os.path.join(folder, '.clip_cache')
    stem      = os.path.splitext(filename)[0]
    src       = os.path.join(folder, filename)
    try:
        mtime_ms = int(os.path.getmtime(src) * 1000)
    except Exception:
        mtime_ms = 0
    start_ms = int(round(start_time * 1000))
    dur_ms   = int(round(clip_duration * 1000))
    return os.path.join(cache_dir, f'{stem}_{mtime_ms}_{start_ms:09d}_{dur_ms}ms.mp4')


def _title_card_cache_path(folder, title, subtitle, title2=''):
    """Return cache path for a title card keyed by title+title2+subtitle content."""
    h = hashlib.md5(f'{title}|{title2}|{subtitle}'.encode()).hexdigest()[:12]
    return os.path.join(folder, '.clip_cache', f'title_{h}.mp4')


def _end_card_cache_path(folder, title='', subtitle=''):
    """Return cache path for the end card, keyed by custom title/subtitle if provided."""
    if title or subtitle:
        h = hashlib.md5(f'{title}|{subtitle}'.encode()).hexdigest()[:12]
        return os.path.join(folder, '.clip_cache', f'end_card_{h}.mp4')
    return os.path.join(folder, '.clip_cache', 'end_card.mp4')


def _delete_end_card_cache(folder):
    """Delete all cached end-card files."""
    cache_dir = os.path.join(folder, '.clip_cache')
    if not os.path.isdir(cache_dir):
        return
    for name in os.listdir(cache_dir):
        if name.startswith('end_card') and name.endswith('.mp4'):
            try:
                os.remove(os.path.join(cache_dir, name))
            except OSError:
                pass


def _day_card_cache_path(folder, date_str, custom_title='', custom_subtitle=''):
    """Return cache path for a day separator card.  Includes a hash of any
    custom title/subtitle so changing the text automatically invalidates cache."""
    if custom_title or custom_subtitle:
        h = hashlib.md5(f'{custom_title}|{custom_subtitle}'.encode()).hexdigest()[:8]
        return os.path.join(folder, '.clip_cache', f'day_{date_str}_{h}.mp4')
    return os.path.join(folder, '.clip_cache', f'day_{date_str}.mp4')


def _delete_day_card_cache(folder, date_str):
    """Delete all cached day-card files for the given date (default + all hash variants)."""
    cache_dir = os.path.join(folder, '.clip_cache')
    if not os.path.isdir(cache_dir):
        return
    prefix = f'day_{date_str}'
    for name in os.listdir(cache_dir):
        if name.startswith(prefix) and name.endswith('.mp4'):
            try:
                os.remove(os.path.join(cache_dir, name))
            except OSError:
                pass


DAYS_PL = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def load_selections(folder):
    """Returns (selections_dict, title, subtitle, title2, disabled_day_cards, day_card_titles,
    end_card_title, end_card_subtitle, music_ends, music_offsets, clip_order, clip_duration, has_saved).
    music_ends: dict filename->track_end_seconds.
    music_offsets: dict filename->track_offset_seconds (silence before track in film timeline).
    clip_order: list of filenames in user-defined order (empty = use default mtime order).
    clip_duration: seconds each clip is cut to (default 3.0).
    has_saved=True means a selections.json was found for this folder."""
    path = os.path.join(folder, 'selections.json')
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if data.get('source_folder') == folder:
                sel           = {s['filename']: s for s in data.get('selections', [])}
                disabled      = set(data.get('disabled_day_cards', []))
                day_titles    = data.get('day_card_titles', {})
                end_title     = data.get('end_card_title', '')
                end_sub       = data.get('end_card_subtitle', '')
                music_ends    = data.get('music_ends', {})
                music_offsets = data.get('music_offsets', data.get('music_starts', {}))  # back-compat
                clip_order    = data.get('clip_order', [])
                clip_dur      = float(data.get('clip_duration', 3.0))
                return (sel, data.get('title', ''), data.get('subtitle', ''), data.get('title2', ''),
                        disabled, day_titles, end_title, end_sub, music_ends, music_offsets, clip_order, clip_dur, True)
        except Exception as e:
            print(f'Load selections error: {e}')
    return {}, '', '', '', set(), {}, '', '', {}, {}, [], 3.0, False


def save_selections(folder, selections, title='', subtitle='', disabled_day_cards=None, day_card_titles=None, end_card_title='', end_card_subtitle='', music_ends=None, music_offsets=None, clip_order=None, clip_duration=3.0, title2=''):
    path = os.path.join(folder, 'selections.json')
    data = {
        'source_folder': folder,
        'created': datetime.datetime.now().isoformat(),
        'clip_duration': clip_duration,
        'title': title,
        'title2': title2,
        'subtitle': subtitle,
        'selections': list(selections.values()),
        'disabled_day_cards': sorted(disabled_day_cards or []),
        'day_card_titles': day_card_titles or {},
        'end_card_title': end_card_title,
        'end_card_subtitle': end_card_subtitle,
        'music_ends': music_ends or {},
        'music_offsets': music_offsets or {},
        'clip_order': clip_order or [],
    }
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f'Save selections error: {e}')


def apply_clip_order(clips, clip_order):
    """Merge saved clip_order with freshly scanned clips.
    Known clips keep their saved order; new clips are inserted at their mtime position."""
    if not clip_order:
        return clips
    by_name = {c['filename']: c for c in clips}
    # Known clips that still exist, in saved order
    ordered = [by_name[fn] for fn in clip_order if fn in by_name]
    # New clips not present in saved order
    known = set(clip_order)
    new_clips = [c for c in clips if c['filename'] not in known]
    if not new_clips:
        return ordered
    # Insert each new clip at its natural mtime position (clips from scan_folder are mtime-sorted)
    result = list(ordered)
    for new_clip in new_clips:
        new_mtime = new_clip['modified']  # ISO string – sorts lexicographically = chronologically
        insert_at = len(result)
        for j, c in enumerate(result):
            if c['modified'] > new_mtime:
                insert_at = j
                break
        result.insert(insert_at, new_clip)
    return result

# ─── GPU / encoder helpers ────────────────────────────────────────────────────

def _video_enc_args(crf=18):
    """Return FFmpeg video-encoder argument list.

    Uses GPU encoder (NVENC / AMF / QSV) when one was detected at startup,
    otherwise falls back to libx264 (CPU).  The quality parameter maps to:
      libx264  → -crf  (lower = better quality)
      NVENC    → -cq   (lower = better quality, VBR mode)
      AMF      → -qp_i / -qp_p
      QSV      → -global_quality (ICQ mode)
    """
    tail = ['-pix_fmt', 'yuv420p', '-profile:v', 'high']
    if GPU_ENCODER == 'h264_nvenc':
        return ['-c:v', 'h264_nvenc', '-preset', 'p4',
                '-rc', 'vbr', '-cq', str(crf), '-b:v', '0'] + tail
    if GPU_ENCODER == 'h264_amf':
        return ['-c:v', 'h264_amf', '-quality', 'speed',
                '-qp_i', str(crf), '-qp_p', str(crf + 2)] + tail
    if GPU_ENCODER == 'h264_qsv':
        return ['-c:v', 'h264_qsv', '-preset', 'fast',
                '-global_quality', str(crf)] + tail
    # CPU fallback
    return ['-c:v', 'libx264', '-preset', 'fast', '-crf', str(crf)] + tail


def _detect_gpu_encoder():
    """Probe for a working hardware H.264 encoder and return its name, or None."""
    rc, out, _ = run_cmd([FFMPEG, '-encoders'], timeout=5)
    for enc in ('h264_nvenc', 'h264_amf', 'h264_qsv'):
        if enc.encode() not in out:
            continue
        # Verify the encoder actually works (GPU driver may be absent even if listed)
        rc, _, _ = run_cmd([
            FFMPEG, '-y',
            '-f', 'lavfi', '-i', 'color=black:s=64x64:r=1',
            '-t', '0.1', '-c:v', enc, '-f', 'null', '-',
        ], timeout=10)
        if rc == 0:
            return enc
    return None


# ─── Title/End card helpers ───────────────────────────────────────────────────

def esc_drawtext(s):
    """Escape special characters for FFmpeg drawtext filter (unquoted option value)."""
    return (s.replace('\\', '\\\\')
             .replace(':', '\\:')
             .replace("'", "\\'")
             .replace('%', '%%')
             .replace(',', '\\,'))


def _esc_font(path):
    """Escape font path for FFmpeg drawtext fontfile option (unquoted).
    Forward slashes are assumed. Escapes colon for Windows drive letter (C: → C\\:)."""
    return path.replace(':', '\\:')


def find_text_font():
    candidates = [
        'C:/Windows/Fonts/segoeui.ttf',
        'C:/Windows/Fonts/arial.ttf',
        'C:/Windows/Fonts/calibri.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/Library/Fonts/Arial.ttf',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p.replace('\\', '/')
    return None


def find_icon_font():
    """Find font with PLAY icon. Segoe UI Symbol has U+23F5 ⏵; fallback uses ▶ from any font."""
    candidates = [
        'C:/Windows/Fonts/seguisym.ttf',   # Segoe UI Symbol – has ⏵
        'C:/Windows/Fonts/segmdl2.ttf',    # MDL2 Assets – has ⏵
        'C:/Windows/Fonts/segoeui.ttf',    # fallback – will use ▶
        'C:/Windows/Fonts/arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/Library/Fonts/Arial.ttf',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p.replace('\\', '/')
    return None


def generate_title_card(title, subtitle, out_path, title2=''):
    """Generate 4-second title card MP4 (1080x1920).
    Play icon visible 0-1s, title+subtitle visible 0-3s (static, no animation).
    Returns out_path on success, or None on failure."""
    text_font = find_text_font()
    if not text_font:
        print('WARN: No font found – intro card skipped')
        return None

    # All Windows fonts share C:/Windows/Fonts/, so one cwd works for all.
    font_dir  = os.path.dirname(text_font)
    text_name = os.path.basename(text_font)  # e.g. 'segoeui.ttf'

    # Separate icon font: Segoe UI Symbol has proper filled play icons.
    icon_font  = find_icon_font() or text_font
    icon_name  = os.path.basename(icon_font)
    name_lower = icon_name.lower()
    # Segoe UI Symbol / MDL2 have ⏵ (U+23F5); everything else uses ▶ (U+25B6)
    icon_char  = '\u23f5' if ('seguisym' in name_lower or 'segmdl2' in name_lower) else '\u25b6'

    title_esc  = esc_drawtext(title)
    has_title2 = bool(title2.strip())

    # Two title lines: shift block up so it stays centred vertically in the frame
    title_y    = 970  if has_title2 else 1020
    subtitle_y = 1190 if has_title2 else 1140

    # \, inside enable= escapes the comma so it's not treated as a filter separator
    vf_parts = [
        f"drawtext=fontfile={icon_name}:text={icon_char}:fontsize=320:"
        f"fontcolor=white:x=(w-tw)/2:y=580:enable=lt(t\\,1)",
        f"drawtext=fontfile={text_name}:text={title_esc}:fontsize=90:"
        f"fontcolor=white:x=(w-tw)/2:y={title_y}:enable=lt(t\\,3)",
    ]
    if has_title2:
        title2_esc = esc_drawtext(title2.strip())
        vf_parts.append(
            f"drawtext=fontfile={text_name}:text={title2_esc}:fontsize=90:"
            f"fontcolor=white:x=(w-tw)/2:y={title_y + 105}:enable=lt(t\\,3)"
        )
    if subtitle.strip():
        sub_esc = esc_drawtext(subtitle.strip())
        vf_parts.append(
            f"drawtext=fontfile={text_name}:text={sub_esc}:fontsize=60:"
            f"fontcolor=white:x=(w-tw)/2:y={subtitle_y}:enable=lt(t\\,3)"
        )

    cmd = [
        FFMPEG, '-y',
        '-f', 'lavfi', '-i', 'color=c=black:s=1080x1920:r=30',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-t', '4',
        '-vf', ','.join(vf_parts),
        '-map', '0:v', '-map', '1:a',
        *_video_enc_args(18),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        out_path,
    ]
    rc, _, err = run_cmd(cmd, timeout=60, cwd=font_dir)
    if rc != 0:
        print('WARN: Error generating intro card:', err.decode('utf-8', errors='replace')[-600:])
        return None
    # Extract first frame as thumbnail for UI preview
    thumb = out_path.replace('.mp4', '_thumb.jpg')
    run_cmd([FFMPEG, '-y', '-ss', '0', '-i', out_path,
             '-vframes', '1', '-q:v', '3', thumb], timeout=15)
    return out_path


def generate_end_card(out_path, title='', subtitle=''):
    """Generate 5-second end card MP4 (1080x1920).
    title defaults to 'The End' if not provided; subtitle is optional.
    Returns out_path on success, or None on failure."""
    font = find_text_font()
    if not font:
        print('WARN: No font found – end card skipped')
        return None
    font_dir  = os.path.dirname(font)
    font_name = os.path.basename(font)
    main_text = title or 'The End'
    main_esc  = esc_drawtext(main_text)
    if subtitle:
        sub_esc   = esc_drawtext(subtitle)
        vf_parts = [
            f"drawtext=fontfile={font_name}:text={main_esc}:fontsize=120:"
            f"fontcolor=white:x=(w-tw)/2:y=(h-th)/2-50",
            f"drawtext=fontfile={font_name}:text={sub_esc}:fontsize=60:"
            f"fontcolor=#aaaaaa:x=(w-tw)/2:y=(h-th)/2+70",
        ]
    else:
        vf_parts = [
            f"drawtext=fontfile={font_name}:text={main_esc}:fontsize=120:"
            f"fontcolor=white:x=(w-tw)/2:y=(h-th)/2",
        ]
    cmd = [
        FFMPEG, '-y',
        '-f', 'lavfi', '-i', 'color=c=black:s=1080x1920:r=30',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-t', '5',
        '-vf', ','.join(vf_parts),
        '-map', '0:v', '-map', '1:a',
        *_video_enc_args(18),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        out_path,
    ]
    rc, _, err = run_cmd(cmd, timeout=60, cwd=font_dir)
    if rc != 0:
        print('WARN: Error generating end card:', err.decode('utf-8', errors='replace')[-600:])
        return None
    return out_path

def generate_day_card(date_str, day_name, out_path, title_override='', subtitle_override=''):
    """Generate 2-second day separator card MP4 (1080x1920).
    Main line: title_override if set, otherwise DD-MM-YYYY date.
    Sub line:  subtitle_override if set, otherwise day_name.
    Returns out_path on success, or None on failure."""
    font = find_text_font()
    if not font:
        print('WARN: No font found – day card skipped')
        return None
    font_dir  = os.path.dirname(font)
    font_name = os.path.basename(font)

    # Main text: custom title overrides the date
    if title_override:
        main_text = title_override
    else:
        try:
            parts = date_str.split('-')
            main_text = f'{parts[2]}-{parts[1]}-{parts[0]}'
        except Exception:
            main_text = date_str

    # Sub text: custom subtitle overrides the day name
    sub_text = subtitle_override if subtitle_override else day_name

    main_esc = esc_drawtext(main_text)
    vf_parts = [
        f"drawtext=fontfile={font_name}:text={main_esc}:fontsize=96:"
        f"fontcolor=white:x=(w-tw)/2:y=(h-th)/2-50",
    ]
    if sub_text:
        sub_esc = esc_drawtext(sub_text)
        vf_parts.append(
            f"drawtext=fontfile={font_name}:text={sub_esc}:fontsize=60:"
            f"fontcolor=#aaaaaa:x=(w-tw)/2:y=(h-th)/2+70"
        )

    cmd = [
        FFMPEG, '-y',
        '-f', 'lavfi', '-i', 'color=c=#121220:s=1080x1920:r=30',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-t', '2',
        '-vf', ','.join(vf_parts),
        '-map', '0:v', '-map', '1:a',
        *_video_enc_args(18),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        out_path,
    ]
    rc, _, err = run_cmd(cmd, timeout=60, cwd=font_dir)
    if rc != 0:
        print('WARN: Error generating day card:', err.decode('utf-8', errors='replace')[-400:])
        return None
    return out_path


# ─── Music mixing ─────────────────────────────────────────────────────────────

def _add_music_to_video(folder, video_path, out_path, music_tracks):
    """Mix music_tracks (from folder/music/) with video_path, write to out_path.
    Returns out_path on success, None on failure."""
    if not music_tracks:
        return None

    music_dir = os.path.join(folder, 'music')
    video_dur, _, _, _, _ = ffprobe_info(video_path)
    if not video_dur:
        print('WARN: Cannot read video duration – skipping music')
        return None

    fade_dur   = min(10.0, video_dur)
    fade_start = max(0.0, video_dur - fade_dur)

    music_paths = [os.path.join(music_dir, t['filename']) for t in music_tracks]
    n = len(music_paths)

    inputs = [FFMPEG, '-y', '-i', video_path]
    for p in music_paths:
        inputs += ['-i', p]

    # Build filter_complex: trim each track to track_end, add offset silence, concat, mix with video
    # track_offset = silence (ms) before the track in the film timeline
    parts = []
    processed = []
    for i, t in enumerate(music_tracks):
        src    = f'[{i+1}:a]'
        label  = f'[mus_t{i}]'
        te     = t.get('track_end')
        offset = t.get('track_offset') or 0
        filters = []
        if te is not None and te < t['duration']:
            filters.append(f'atrim=0:{te:.3f},asetpts=PTS-STARTPTS')
        if offset > 0:
            delay_ms = int(round(offset * 1000))
            filters.append(f'adelay={delay_ms}|{delay_ms}')
        if filters:
            parts.append(f'{src}' + ','.join(filters) + f'{label}')
            processed.append(label)
        else:
            processed.append(src)

    if n == 1:
        prefix = processed[0]
    else:
        concat_ins = ''.join(processed)
        parts.append(f'{concat_ins}concat=n={n}:v=0:a=1[mus_raw]')
        prefix = '[mus_raw]'

    trim_filter = (f'{prefix}atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS,'
                   f'afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}[mus]')
    mix_filter  = '[0:a][mus]amix=inputs=2:duration=first:dropout_transition=0[audio_out]'

    parts.extend([trim_filter, mix_filter])
    full_filter = ';'.join(parts)

    cmd = inputs + [
        '-filter_complex', full_filter,
        '-map', '0:v',
        '-map', '[audio_out]',
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k',
        out_path,
    ]

    names = ', '.join(t['filename'] for t in music_tracks)
    print(f'  Mixing music ({n} track(s)): {names}  |  fade @{fade_start:.1f}s', flush=True)
    rc, _, err = run_cmd(cmd, timeout=600)
    if rc != 0:
        print('WARN: Music mixing error:', err.decode('utf-8', errors='replace')[-400:])
        return None
    return out_path


def _embed_mp4_thumbnail(video_path, thumb_path):
    """Embed thumb_path as cover art (attached_pic) inside video_path, in-place."""
    temp = video_path + '._thumb_tmp.mp4'
    cmd = [
        FFMPEG, '-y',
        '-i', video_path,
        '-i', thumb_path,
        '-map', '0',
        '-map', '1',
        '-c', 'copy',
        '-c:v:1', 'mjpeg',
        '-disposition:v:1', 'attached_pic',
        temp,
    ]
    rc, _, err = run_cmd(cmd, timeout=600)
    if rc == 0:
        try:
            os.replace(temp, video_path)
            return True
        except Exception as e:
            print(f'WARN: Could not replace file after thumbnail embed: {e}')
            try:
                os.remove(temp)
            except Exception:
                pass
    else:
        print('WARN: Could not embed thumbnail:', err.decode('utf-8', errors='replace')[-200:])
        try:
            os.remove(temp)
        except Exception:
            pass
    return False


# ─── Export ───────────────────────────────────────────────────────────────────

def export_worker(folder, clips, selections, out_name, title='', subtitle='', music_tracks=None, include_day_cards=True, disabled_day_cards=None, day_card_titles=None, end_card_title='', end_card_subtitle='', clip_duration=3.0, title2=''):
    global export_status, export_cancel, export_proc

    def set_status(s, msg, pct, output=None):
        with export_lock:
            export_status.update({'status': s, 'progress': msg, 'percent': pct})
            if output is not None:
                export_status['output'] = output

    def cancelled():
        with export_lock:
            return export_cancel

    set_status('working', 'Preparing...', 0)

    selected = [
        (c, selections[c['filename']])
        for c in clips
        if c['filename'] in selections and selections[c['filename']].get('enabled')
    ]

    if not selected:
        set_status('error', 'No clips selected', 0)
        return

    cache_dir = os.path.join(folder, '.clip_cache')
    temp_dir  = os.path.join(folder, 'temp')
    out_dir   = os.path.join(folder, 'output')
    try:
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(temp_dir,  exist_ok=True)
        os.makedirs(out_dir,   exist_ok=True)
    except Exception as e:
        set_status('error', f'Error creating directories: {e}', 0)
        return

    # Title card — check cache first
    title_file = None
    if title:
        title_cache = _title_card_cache_path(folder, title, subtitle, title2)
        if os.path.isfile(title_cache):
            print('  [cache] intro card')
            title_file = title_cache
        else:
            set_status('working', 'Generating intro card...', 2)
            title_file = generate_title_card(title, subtitle, title_cache, title2)

    cut_clips = []  # list of (date_str, cache_path)
    # Count total slots (primary + duplicates) for progress reporting
    total_slots = sum(1 + len(sel.get('extra_starts') or []) for _, sel in selected)
    slot_idx = 0

    for clip, sel in selected:
        inp      = os.path.join(folder, clip['filename'])
        # Group by day with 5:00 AM cutoff — clips before 5 AM belong to the previous day
        modified_str = clip.get('modified') or ''
        try:
            dt = datetime.datetime.fromisoformat(modified_str)
            if dt.hour < 5:
                dt -= datetime.timedelta(days=1)
            clip_day = dt.strftime('%Y-%m-%d')
        except Exception:
            clip_day = modified_str[:10]
        starts   = [sel.get('start_time') or 0.0] + [float(t) for t in (sel.get('extra_starts') or [])]

        for j, start in enumerate(starts):
            label      = clip['filename'] + (f' [{j+1}/{len(starts)}]' if len(starts) > 1 else '')
            cache_path = _clip_cache_path(folder, clip['filename'], start, clip_duration)

            if os.path.isfile(cache_path):
                print(f'  [cache] {label}')
                cut_clips.append((clip_day, cache_path))
                slot_idx += 1
                continue

            set_status('working', f'Cutting {slot_idx+1}/{total_slots}: {label}', int(slot_idx / total_slots * 70))
            slot_idx += 1

            clip_is_hdr = clip.get('is_hdr') or is_hdr_transfer(ffprobe_info(inp)[4])
            if clip_is_hdr:
                if FFMPEG_HAS_ZSCALE:
                    # HDR→SDR tone-mapping via zscale; npl=1000 matches modern phone HDR peak
                    color_vf = ('zscale=t=linear:npl=1000,format=gbrpf32le,'
                                'zscale=p=bt709,tonemap=tonemap=mobius:desat=0,'
                                'zscale=t=bt709:m=bt709:r=tv,format=yuv420p')
                else:
                    # Fallback: colorspace matrix conversion (no tone-mapping)
                    color_vf = 'colorspace=space=bt709:trc=bt709:primaries=bt709:range=mpeg'
            else:
                # SDR clip — just ensure correct pixel format, no tone-mapping
                color_vf = 'format=yuv420p'
            vf = (f'{color_vf},'
                  'scale=1080:1920:force_original_aspect_ratio=decrease,'
                  'pad=1080:1920:-1:-1:color=black')
            fade_start = max(0.0, clip_duration - 0.3)
            cmd = [
                FFMPEG, '-y',
                '-ss', str(start),
                '-i', inp,
                '-t', str(clip_duration),
                *_video_enc_args(18),
                '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', '1',
                '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2',
                '-vf', vf,
                '-af', f'afade=t=out:st={fade_start:.2f}:d=0.3',
                '-r', '30',
                cache_path,
            ]
            if cancelled():
                set_status('error', 'Cancelled', 0)
                return
            rc, _, err = run_cmd(cmd, timeout=120)
            if cancelled():
                set_status('error', 'Cancelled', 0)
                return
            if rc != 0:
                msg = err.decode('utf-8', errors='replace')[-400:]
                set_status('error', f'Cutting error {label}: {msg}', 0)
                return
            cut_clips.append((clip_day, cache_path))

    # End card — check cache first
    end_cache = _end_card_cache_path(folder, end_card_title, end_card_subtitle)
    if os.path.isfile(end_cache):
        print('  [cache] end card')
        end_file = end_cache
    else:
        set_status('working', 'Generating end card...', 72)
        end_file = generate_end_card(end_cache, end_card_title, end_card_subtitle)

    # Day separator cards — one per unique day in chronological order
    _disabled    = disabled_day_cards or set()
    _day_titles  = day_card_titles or {}
    day_files = {}  # date_str -> path or None
    if include_day_cards:
        for (day, _) in cut_clips:
            if day and day not in day_files:
                if day in _disabled:
                    day_files[day] = None  # explicitly disabled by user
                    continue
                custom     = _day_titles.get(day, {})
                cust_title = custom.get('title', '')
                cust_sub   = custom.get('subtitle', '')
                day_cache  = _day_card_cache_path(folder, day, cust_title, cust_sub)
                if os.path.isfile(day_cache):
                    print(f'  [cache] day card {day}')
                    day_files[day] = day_cache
                else:
                    set_status('working', f'Generating day card {day}...', 78)
                    try:
                        dt_obj   = datetime.datetime.strptime(day, '%Y-%m-%d')
                        day_name = DAYS_PL[dt_obj.weekday()]
                    except Exception:
                        day_name = ''
                    day_files[day] = generate_day_card(day, day_name, day_cache,
                                                        title_override=cust_title,
                                                        subtitle_override=cust_sub)

    # Assemble final file list: [title_card?] + [day_card + clips_of_day...]* + [end_card?]
    # First day card is skipped when a title card precedes it (clips flow directly after title).
    concat_list = []
    if title_file:
        concat_list.append(title_file)
    last_day = None
    is_first_day = True
    for (day, cache_path) in cut_clips:
        if day != last_day:
            last_day = day
            if include_day_cards and day_files.get(day) and not (is_first_day and title_file):
                concat_list.append(day_files[day])
            is_first_day = False
        concat_list.append(cache_path)
    if end_file:
        concat_list.append(end_file)

    # Write filelist.txt (FFmpeg concat demuxer)
    flist = os.path.join(temp_dir, 'filelist.txt')
    try:
        with open(flist, 'w', encoding='utf-8') as f:
            for p in concat_list:
                p_unix = os.path.abspath(p).replace('\\', '/')
                f.write(f"file '{p_unix}'\n")
    except Exception as e:
        set_status('error', f'Filelist error: {e}', 0)
        return

    has_music   = bool(music_tracks)
    out_path    = os.path.join(out_dir, out_name)
    concat_path = os.path.join(temp_dir, 'concat_out.mp4') if has_music else out_path

    # Estimate total duration so we can track merge progress
    day_file_set = {v for v in day_files.values() if v}
    total_merge_dur = sum(
        4.0 if f == title_file else
        5.0 if f == end_file    else
        2.0 if f in day_file_set else
        clip_duration
        for f in concat_list
    )

    set_status('working', f'Merging {len(concat_list)} segments...', 86)
    merge_cmd = [FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', flist,
                 '-c', 'copy', '-progress', 'pipe:1', '-nostats', concat_path]
    merge_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if IS_WIN:
        merge_kw['creationflags'] = _NO_WINDOW

    proc = subprocess.Popen(merge_cmd, **merge_kw)
    with export_lock:
        export_proc = proc

    # Drain stderr in background to prevent pipe deadlock
    stderr_buf = []
    def _drain():
        try:
            for chunk in iter(lambda: proc.stderr.read(4096), b''):
                stderr_buf.append(chunk)
        except Exception:
            pass
    t_err = threading.Thread(target=_drain, daemon=True)
    t_err.start()

    try:
        for raw in proc.stdout:
            if cancelled():
                proc.kill()
                proc.wait()
                set_status('error', 'Cancelled', 0)
                return
            line = raw.decode('utf-8', errors='replace').strip()
            if line.startswith('out_time_ms='):
                try:
                    us = int(line.split('=', 1)[1])
                    if total_merge_dur > 0 and us > 0:
                        frac = min(us / 1_000_000 / total_merge_dur, 1.0)
                        pct  = int(86 + frac * 6)   # 86 → 92
                        set_status('working',
                                   f'Merging {len(concat_list)} segments... {int(frac * 100)}%',
                                   pct)
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        proc.kill()
        proc.wait()
        set_status('error', f'Merging error: {e}', 0)
        return
    finally:
        proc.stdout.close()
        t_err.join(timeout=5)
        with export_lock:
            export_proc = None

    rc = proc.wait(timeout=3600)
    err = b''.join(stderr_buf)
    if rc != 0:
        msg = err.decode('utf-8', errors='replace')[-400:]
        set_status('error', f'Merging error: {msg}', 0)
        return

    if has_music:
        set_status('working', 'Adding music...', 92)
        result = _add_music_to_video(folder, concat_path, out_path, music_tracks)
        if result:
            try:
                os.remove(concat_path)
            except Exception:
                pass
        else:
            # Fallback: use concat result without music
            try:
                os.replace(concat_path, out_path)
            except Exception:
                pass
            print('WARN: Music was not added – film saved without music')

    # Embed title card thumbnail as cover art so Windows Explorer shows it
    if title_file:
        thumb_path = title_file.replace('.mp4', '_thumb.jpg')
        if not os.path.isfile(thumb_path):
            # Title card was served from cache but _thumb.jpg is missing — generate it now
            run_cmd([FFMPEG, '-y', '-ss', '0', '-i', title_file,
                     '-vframes', '1', '-q:v', '3', thumb_path], timeout=15)
        if os.path.isfile(thumb_path):
            set_status('working', 'Embedding thumbnail...', 94)
            ok = _embed_mp4_thumbnail(out_path, thumb_path)
            if ok and IS_WIN:
                # Tell Windows Explorer to discard its cached thumbnail and re-read the file
                try:
                    import ctypes
                    abs_out = os.path.abspath(out_path)
                    ctypes.windll.shell32.SHChangeNotify(
                        0x00002000,          # SHCNE_UPDATEITEM
                        0x0005,              # SHCNF_PATHW
                        ctypes.c_wchar_p(abs_out), None)
                except Exception:
                    pass

    # Cleanup — only temp dir (clips live in .clip_cache/, not in temp)
    set_status('working', 'Cleanup...', 96)
    try:
        os.remove(flist)
        os.rmdir(temp_dir)
    except Exception:
        pass

    set_status('done', f'Done! Saved: output/{out_name}', 100, output=out_name)
    print(f'Export done: output/{out_name}')

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    # Paths that don't require a valid session
    _OPEN_PATHS = frozenset({
        ('GET',  '/'),
        ('GET',  '/index.html'),
        ('POST', '/api/session'),
    })
    # Path prefixes that don't require session (browser-native fetches)
    _OPEN_PREFIXES = ('/api/video/', '/api/output/', '/api/frame/')

    def log_message(self, fmt, *args):
        pass  # silence access log

    def handle_error(self, request, client_address):
        # Silence noisy connection-reset/abort errors from the browser
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def _session_ok(self, method, path):
        """Return True if the request has the current session token, or if
        no session has been registered yet, or if the path is always open."""
        if (method, path) in self._OPEN_PATHS:
            return True
        # Video/output files are fetched by the browser's native <video> element
        # which cannot add custom headers – allow these without session check.
        if method == 'GET' and any(path.startswith(p) for p in self._OPEN_PREFIXES):
            return True
        with _session_lock:
            if _session['id'] is None:
                return True  # no session registered – allow all
            return self.headers.get('X-Session-Id') == _session['id']

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(n)
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if not self._session_ok('GET', path):
            self.send_json({'error': 'session_expired'}, 409)
            return

        if path in ('/', '/index.html'):
            self._serve_static('index.html')
        elif path == '/api/state':
            with state_lock:
                self.send_json({
                    'loaded': state['folder'] is not None,
                    'folder': state['folder'],
                    'count': len(state['clips']),
                    'title': state['title'],
                    'title2': state['title2'],
                    'subtitle': state['subtitle'],
                    'end_card_title': state['end_card_title'],
                    'end_card_subtitle': state['end_card_subtitle'],
                })
        elif path == '/api/clips':
            with state_lock:
                self.send_json({'clips': state['clips']})
        elif path == '/api/selections':
            with state_lock:
                self.send_json({
                    'selections': state['selections'],
                    'disabled_day_cards': list(state['disabled_day_cards']),
                    'day_card_titles': dict(state['day_card_titles']),
                    'end_card_title': state['end_card_title'],
                    'end_card_subtitle': state['end_card_subtitle'],
                    'clip_duration': state['clip_duration'],
                })
        elif path == '/api/preview_status':
            with state_lock:
                clips = list(state['clips'])
            with _preview_lock:
                cache    = dict(_preview_cache)
                in_prog  = set(_preview_in_progress)
            status = {}
            for c in clips:
                fname = c['filename']
                codec = c.get('codec', '')
                if codec not in ('hevc', 'h265'):
                    status[fname] = 'ready'
                elif fname in cache:
                    status[fname] = 'ready' if cache[fname] else 'error'
                elif fname in in_prog:
                    status[fname] = 'working'
                else:
                    status[fname] = 'pending'
            all_ready = all(v in ('ready', 'error') for v in status.values())
            self.send_json({'status': status, 'all_ready': all_ready})
        elif path == '/api/music':
            with state_lock:
                self.send_json({'tracks': state['music']})
        elif path == '/api/export/status':
            with export_lock:
                self.send_json(dict(export_status))
        elif path == '/api/title_thumbnail':
            with state_lock:
                folder   = state['folder']
                title    = state['title']
                subtitle = state['subtitle']
            if not folder or not title:
                self.send_error(404, 'No title card')
                return
            title_cache = _title_card_cache_path(folder, title, subtitle)
            thumb_path  = title_cache.replace('.mp4', '_thumb.jpg')
            if not os.path.isfile(thumb_path):
                self.send_error(404, 'Thumbnail not ready')
                return
            with open(thumb_path, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', len(body))
            self.send_header('Cache-Control', 'public, max-age=30')
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith('/api/frame/'):
            filename = unquote(path[len('/api/frame/'):])
            qs = urlparse(self.path).query
            t = 0.0
            cs = -1.0  # cache_start: if >=0 try to use pre-cut clip cache
            for part in qs.split('&'):
                if part.startswith('t='):
                    try: t = float(part[2:])
                    except Exception: pass
                elif part.startswith('cs='):
                    try: cs = float(part[3:])
                    except Exception: pass
            self._serve_frame(filename, t, cs)
        elif path.startswith('/api/video/'):
            filename = unquote(path[len('/api/video/'):])
            self._serve_video(filename)
        elif path.startswith('/api/output/'):
            filename = unquote(path[len('/api/output/'):])
            self._serve_output(filename)
        else:
            self.send_error(404, 'Not Found')

    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()

        if path == '/api/session':
            new_id = str(uuid.uuid4())
            with _session_lock:
                _session['id'] = new_id
            print(f'New session: {new_id[:8]}...')
            self.send_json({'session_id': new_id})
            return

        if not self._session_ok('POST', path):
            self.send_json({'error': 'session_expired'}, 409)
            return

        if path == '/api/folder':
            folder = data.get('folder', '').strip()
            if not os.path.isdir(folder):
                self.send_json({'error': f'Folder does not exist: {folder}'}, 400)
                return
            print(f'Scanning: {folder}')
            clips = scan_folder(folder)
            if not clips:
                self.send_json({'error': 'No MP4 files found in folder'}, 400)
                return
            sel, title, subtitle, title2, disabled_days, day_titles, end_title, end_sub, music_ends, music_offsets, clip_order, clip_dur, has_saved = load_selections(folder)
            if not has_saved:
                # First load – apply defaults: title = folder basename, subtitle = date of first clip
                title = os.path.basename(folder.rstrip('/\\'))
                clip_dur = 3.0
                try:
                    first = min(clips, key=lambda c: c.get('modified', ''))
                    dt    = datetime.datetime.fromisoformat(first['modified'])
                    subtitle = dt.strftime('%d-%m-%Y')
                except Exception:
                    pass
            clips = apply_clip_order(clips, clip_order)
            music = scan_music(folder)
            # Apply saved track_end and track_offset values to music tracks
            for t in music:
                if t['filename'] in music_ends:
                    t['track_end'] = float(music_ends[t['filename']])
                if t['filename'] in music_offsets:
                    t['track_offset'] = float(music_offsets[t['filename']])
            with state_lock:
                state['folder']             = folder
                state['clips']              = clips
                state['selections']         = sel
                state['title']              = title
                state['title2']             = title2
                state['subtitle']           = subtitle
                state['music']              = music
                state['disabled_day_cards'] = disabled_days
                state['day_card_titles']    = day_titles
                state['end_card_title']     = end_title
                state['end_card_subtitle']  = end_sub
                state['clip_duration']      = clip_dur
            if not has_saved and folder:
                save_selections(folder, sel, title, subtitle, disabled_days, day_titles, end_title, end_sub, clip_order=[c['filename'] for c in clips], clip_duration=clip_dur, title2=title2)
            print(f'Loaded {len(clips)} clips, {len(sel)} saved decisions, {len(music)} tracks')
            threading.Thread(target=pregenerate_hevc_previews, args=(folder, clips), daemon=True).start()
            self.send_json({'ok': True, 'count': len(clips)})

        elif path == '/api/select':
            fname = data.get('filename')
            if not fname:
                self.send_json({'error': 'Missing filename'}, 400)
                return
            with state_lock:
                folder    = state['folder']
                title     = state['title']
                subtitle  = state['subtitle']
                end_title = state['end_card_title']
                end_sub   = state['end_card_subtitle']
                clip_dur  = state['clip_duration']
                state['selections'][fname] = {
                    'filename': fname,
                    'start_time': data.get('start_time'),
                    'enabled': bool(data.get('enabled', True)),
                    'extra_starts': [float(t) for t in (data.get('extra_starts') or [])],
                }
                sel_copy   = dict(state['selections'])
                disabled   = list(state['disabled_day_cards'])
                day_titles = dict(state['day_card_titles'])
                cur_order  = [c['filename'] for c in state['clips']]
            if folder:
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, end_title, end_sub, clip_order=cur_order, clip_duration=clip_dur)
            self.send_json({'ok': True})

        elif path == '/api/settings':
            title    = data.get('title', '').strip()
            title2   = data.get('title2', '').strip()
            subtitle = data.get('subtitle', '').strip()
            with state_lock:
                state['title']    = title
                state['title2']   = title2
                state['subtitle'] = subtitle
                folder     = state['folder']
                sel_copy   = dict(state['selections'])
                disabled   = list(state['disabled_day_cards'])
                day_titles = dict(state['day_card_titles'])
                end_title  = state['end_card_title']
                end_sub    = state['end_card_subtitle']
                clip_dur   = state['clip_duration']
                cur_order  = [c['filename'] for c in state['clips']]
            if folder:
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, end_title, end_sub, clip_order=cur_order, clip_duration=clip_dur, title2=title2)
            self.send_json({'ok': True})

        elif path == '/api/export':
            with state_lock:
                if not state['folder']:
                    self.send_json({'error': 'No folder loaded'}, 400)
                    return
                folder           = state['folder']
                clips            = list(state['clips'])
                sel              = dict(state['selections'])
                music            = list(state['music'])
                disabled_days    = set(state['disabled_day_cards'])
                day_titles       = dict(state['day_card_titles'])
                end_card_title   = state['end_card_title']
                end_card_subtitle= state['end_card_subtitle']
                clip_dur         = state['clip_duration']

            global export_cancel, export_proc
            with export_lock:
                if export_status['status'] == 'working':
                    self.send_json({'error': 'Export already in progress'}, 400)
                    return
                export_cancel = False   # clear any previous cancel flag

            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            out_name = data.get('output_filename') or f'final_{ts}.mp4'
            out_name = re.sub(r'[^\w\-_. ]', '_', out_name).strip('_') or f'final_{ts}.mp4'
            if not out_name.lower().endswith('.mp4'):
                out_name += '.mp4'

            title             = data.get('title', '').strip()
            title2            = data.get('title2', '').strip()
            subtitle          = data.get('subtitle', '').strip()
            include_day_cards = bool(data.get('include_day_cards', True))

            t = threading.Thread(
                target=export_worker,
                args=(folder, clips, sel, out_name, title, subtitle, music, include_day_cards, disabled_days, day_titles, end_card_title, end_card_subtitle, clip_dur, title2),
                daemon=True,
            )
            t.start()
            self.send_json({'ok': True, 'output': out_name})

        elif path == '/api/export/cancel':
            with export_lock:
                if export_status['status'] != 'working':
                    self.send_json({'ok': False, 'error': 'No export in progress'})
                    return
                export_cancel = True
                proc = export_proc
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            self.send_json({'ok': True})

        elif path == '/api/shutdown':
            self.send_json({'ok': True})
            def _quit():
                import time as _time
                _time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=_quit, daemon=True).start()

        elif path == '/api/day_card_toggle':
            date_str = data.get('date', '').strip()
            enabled  = bool(data.get('enabled', True))
            if not date_str:
                self.send_json({'error': 'Missing date'}, 400)
                return
            with state_lock:
                if enabled:
                    state['disabled_day_cards'].discard(date_str)
                else:
                    state['disabled_day_cards'].add(date_str)
                folder    = state['folder']
                sel_copy  = dict(state['selections'])
                title     = state['title']
                subtitle  = state['subtitle']
                disabled  = list(state['disabled_day_cards'])
                day_titles= dict(state['day_card_titles'])
                end_title = state['end_card_title']
                end_sub   = state['end_card_subtitle']
                clip_dur  = state['clip_duration']
                cur_order = [c['filename'] for c in state['clips']]
            if folder:
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, end_title, end_sub, clip_order=cur_order, clip_duration=clip_dur)
            self.send_json({'ok': True})

        elif path == '/api/day_card_title':
            date_str      = data.get('date', '').strip()
            title_text    = data.get('title', '').strip()
            subtitle_text = data.get('subtitle', '').strip()
            if not date_str:
                self.send_json({'error': 'Missing date'}, 400)
                return
            with state_lock:
                if title_text or subtitle_text:
                    state['day_card_titles'][date_str] = {'title': title_text, 'subtitle': subtitle_text}
                else:
                    state['day_card_titles'].pop(date_str, None)
                folder     = state['folder']
                sel_copy   = dict(state['selections'])
                title      = state['title']
                subtitle   = state['subtitle']
                disabled   = list(state['disabled_day_cards'])
                day_titles = dict(state['day_card_titles'])
                end_title  = state['end_card_title']
                end_sub    = state['end_card_subtitle']
                clip_dur   = state['clip_duration']
                cur_order  = [c['filename'] for c in state['clips']]
            if folder:
                _delete_day_card_cache(folder, date_str)
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, end_title, end_sub, clip_order=cur_order, clip_duration=clip_dur)
            self.send_json({'ok': True})

        elif path == '/api/end_card_title':
            title_text    = data.get('title', '').strip()
            subtitle_text = data.get('subtitle', '').strip()
            with state_lock:
                state['end_card_title']    = title_text
                state['end_card_subtitle'] = subtitle_text
                folder     = state['folder']
                sel_copy   = dict(state['selections'])
                title      = state['title']
                subtitle   = state['subtitle']
                disabled   = list(state['disabled_day_cards'])
                day_titles = dict(state['day_card_titles'])
                clip_dur   = state['clip_duration']
                cur_order  = [c['filename'] for c in state['clips']]
            if folder:
                _delete_end_card_cache(folder)
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, title_text, subtitle_text, clip_order=cur_order, clip_duration=clip_dur)
            self.send_json({'ok': True})

        elif path == '/api/music_ends':
            # music_ends: {filename: track_end_seconds}, music_offsets: {filename: track_offset_seconds}
            ends    = data.get('music_ends', {})
            offsets = data.get('music_offsets', {})
            with state_lock:
                for track in state['music']:
                    fname = track['filename']
                    if fname in ends:
                        track['track_end'] = float(ends[fname])
                    elif 'track_end' in track:
                        del track['track_end']
                    if fname in offsets:
                        track['track_offset'] = float(offsets[fname])
                    elif 'track_offset' in track:
                        del track['track_offset']
                folder     = state['folder']
                sel_copy   = dict(state['selections'])
                title      = state['title']
                subtitle   = state['subtitle']
                disabled   = list(state['disabled_day_cards'])
                day_titles = dict(state['day_card_titles'])
                end_title  = state['end_card_title']
                end_sub    = state['end_card_subtitle']
                clip_dur   = state['clip_duration']
                music_ends_save    = {t['filename']: t['track_end']    for t in state['music'] if 'track_end'    in t}
                music_offsets_save = {t['filename']: t['track_offset'] for t in state['music'] if 'track_offset' in t}
                cur_order  = [c['filename'] for c in state['clips']]
            if folder:
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, end_title, end_sub, music_ends_save, music_offsets_save, clip_order=cur_order, clip_duration=clip_dur)
            self.send_json({'ok': True})

        elif path == '/api/clip_order':
            order = data.get('clip_order', [])
            with state_lock:
                folder     = state['folder']
                by_name    = {c['filename']: c for c in state['clips']}
                if order:
                    state['clips'] = [by_name[fn] for fn in order if fn in by_name]
                else:
                    # Reset: sort by mtime (modified ISO string)
                    state['clips'].sort(key=lambda c: c['modified'])
                sel_copy   = dict(state['selections'])
                title      = state['title']
                subtitle   = state['subtitle']
                disabled   = list(state['disabled_day_cards'])
                day_titles = dict(state['day_card_titles'])
                end_title  = state['end_card_title']
                end_sub    = state['end_card_subtitle']
                clip_dur   = state['clip_duration']
                music_ends_save    = {t['filename']: t['track_end']    for t in state['music'] if 'track_end'    in t}
                music_offsets_save = {t['filename']: t['track_offset'] for t in state['music'] if 'track_offset' in t}
            if folder:
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, end_title, end_sub, music_ends_save, music_offsets_save, clip_order=order, clip_duration=clip_dur)
            self.send_json({'ok': True})

        elif path == '/api/clip_duration':
            _allowed = [0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
            try:
                requested = float(data.get('duration', 3.0))
            except (TypeError, ValueError):
                requested = 3.0
            dur = min(_allowed, key=lambda x: abs(x - requested))
            with state_lock:
                state['clip_duration'] = dur
                folder     = state['folder']
                sel_copy   = dict(state['selections'])
                title      = state['title']
                subtitle   = state['subtitle']
                disabled   = list(state['disabled_day_cards'])
                day_titles = dict(state['day_card_titles'])
                end_title  = state['end_card_title']
                end_sub    = state['end_card_subtitle']
                music_ends_save    = {t['filename']: t['track_end']    for t in state['music'] if 'track_end'    in t}
                music_offsets_save = {t['filename']: t['track_offset'] for t in state['music'] if 'track_offset' in t}
                cur_order  = [c['filename'] for c in state['clips']]
            if folder:
                save_selections(folder, sel_copy, title, subtitle, disabled, day_titles, end_title, end_sub, music_ends_save, music_offsets_save, clip_order=cur_order, clip_duration=dur)
            self.send_json({'ok': True, 'clip_duration': dur})

        elif path == '/api/clear-cache':
            with state_lock:
                folder = state['folder']
            if not folder:
                self.send_json({'error': 'No folder loaded'}, 400)
                return
            with export_lock:
                if export_status['status'] == 'working':
                    self.send_json({'error': 'Export in progress — cannot clear cache now'}, 400)
                    return
            cache_dir = os.path.join(folder, '.clip_cache')
            deleted = 0
            if os.path.isdir(cache_dir):
                for f in os.listdir(cache_dir):
                    try:
                        os.remove(os.path.join(cache_dir, f))
                        deleted += 1
                    except Exception:
                        pass
            print(f'Cache cleared: {deleted} files deleted')
            self.send_json({'ok': True, 'deleted': deleted})

        else:
            self.send_json({'error': 'Not found'}, 404)

    def _serve_frame(self, filename, t, cache_start=-1.0):
        """Extract a single JPEG frame at time t from a clip and return it."""
        with state_lock:
            folder = state['folder']
            clips  = state['clips']
        if not folder:
            self.send_error(404, 'No folder loaded')
            return
        filename = os.path.basename(filename)
        fpath = os.path.join(folder, filename)
        if not os.path.isfile(fpath):
            self.send_error(404, 'Video not found')
            return

        # Prefer the pre-cut clip cache — it's a short H.264 file, seek is instant
        used_cache = False
        if cache_start >= 0:
            cache_path = _clip_cache_path(folder, filename, cache_start)
            if os.path.isfile(cache_path):
                fpath = cache_path
                t = max(0.0, min(t - cache_start, 2.9))
                used_cache = True
        if not used_cache:
            # Fall back: use H.264 preview for HEVC clips
            codec = next((c.get('codec', '') for c in clips if c['filename'] == filename), '')
            if codec in ('hevc', 'h265'):
                with _preview_lock:
                    preview = _preview_cache.get(filename)
                if preview and os.path.isfile(preview):
                    fpath = preview

        # Choose scale to match clip orientation (portrait vs landscape)
        clip_info = next((c for c in clips if c['filename'] == filename), None)
        cw = (clip_info or {}).get('width',  1080)
        ch = (clip_info or {}).get('height', 1920)
        scale = 'scale=960:540' if cw > ch else 'scale=540:960'

        kwargs = {}
        if IS_WIN:
            kwargs['creationflags'] = _NO_WINDOW
        try:
            cmd = [FFMPEG, '-y', '-ss', f'{t:.3f}', '-i', fpath,
                   '-vframes', '1', '-an', '-vf', scale,
                   '-f', 'image2', '-vcodec', 'mjpeg', '-q:v', '4', 'pipe:1']
            r = subprocess.run(cmd, capture_output=True, timeout=10, **kwargs)
            if r.returncode == 0 and r.stdout:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', len(r.stdout))
                self.send_header('Cache-Control', 'public, max-age=60')
                self.end_headers()
                self.wfile.write(r.stdout)
                return
        except Exception:
            pass
        try:
            self.send_error(500, 'Frame extraction failed')
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, name):
        fpath = os.path.join(SCRIPT_DIR, name)
        if not os.path.isfile(fpath):
            self.send_error(404, 'File not found')
            return
        with open(fpath, 'rb') as f:
            body = f.read()
        ext = name.rsplit('.', 1)[-1].lower()
        ct = {
            'html': 'text/html; charset=utf-8',
            'js': 'application/javascript',
            'css': 'text/css',
        }.get(ext, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_video(self, filename):
        with state_lock:
            folder = state['folder']
            clips  = state['clips']
        if not folder:
            self.send_error(404, 'No folder loaded')
            return

        # Prevent path traversal
        filename = os.path.basename(filename)
        fpath = os.path.join(folder, filename)
        if not os.path.isfile(fpath):
            self.send_error(404, 'Video not found')
            return

        # If HEVC/H.265 – transcode to H.264 for browser compatibility
        codec = next((c.get('codec', '') for c in clips if c['filename'] == filename), '')
        if not codec:
            # Codec not in clip list (e.g. fresh scan without codec field) – detect now
            _, codec, _, _, _ = ffprobe_info(fpath)
            codec = codec or ''
        if codec in ('hevc', 'h265'):
            preview = ensure_h264_preview(folder, filename)
            if preview:
                fpath = preview

        size = os.path.getsize(fpath)
        rng = self.headers.get('Range')

        if rng:
            m = re.match(r'bytes=(\d+)-(\d*)', rng)
            if not m:
                self.send_response(416)
                self.end_headers()
                return
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', length)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    f.seek(start)
                    left = length
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except Exception:
                pass
        else:
            self.send_response(200)
            self.send_header('Content-Length', size)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception:
                pass

    def _serve_output(self, filename):
        """Serve a generated output file (MP4) from the output/ subfolder."""
        with state_lock:
            folder = state['folder']
        if not folder:
            self.send_error(404, 'No folder loaded')
            return
        filename = os.path.basename(filename)
        fpath = os.path.join(folder, 'output', filename)
        if not os.path.isfile(fpath):
            self.send_error(404, 'File not found')
            return
        size = os.path.getsize(fpath)
        rng  = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d+)-(\d*)', rng)
            if not m:
                self.send_response(416); self.end_headers(); return
            start  = int(m.group(1))
            end    = int(m.group(2)) if m.group(2) else size - 1
            end    = min(end, size - 1)
            if start > end:
                self.send_response(416); self.end_headers(); return
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range',  f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', length)
            self.send_header('Content-Type',   'video/mp4')
            self.send_header('Accept-Ranges',  'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    f.seek(start)
                    left = length
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk: break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except Exception:
                pass
        else:
            self.send_response(200)
            self.send_header('Content-Length', size)
            self.send_header('Content-Type',   'video/mp4')
            self.send_header('Accept-Ranges',  'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
            except Exception:
                pass

# ─── Entry point ──────────────────────────────────────────────────────────────

FFMPEG_HAS_ZSCALE = False  # set by check_ffmpeg()
GPU_ENCODER = None          # set by check_ffmpeg(); 'h264_nvenc' | 'h264_amf' | 'h264_qsv' | None

def check_ffmpeg():
    global FFMPEG_HAS_ZSCALE, GPU_ENCODER
    for tool, path in (('ffmpeg', FFMPEG), ('ffprobe', FFPROBE)):
        try:
            rc, _, _ = run_cmd([path, '-version'], timeout=5)
            src = 'local' if os.path.isabs(path) else 'PATH'
            print(f'{tool}: OK ({src})')
        except FileNotFoundError:
            print(f'ERROR: {tool} not found!')
            print(f'  Place next to server.py: {tool}.exe')
            print(f'  or add to PATH. Download: https://ffmpeg.org/download.html')
            sys.exit(1)
    # Detect zscale (libzimg) for HDR→SDR tone mapping
    rc, out, _ = run_cmd([FFMPEG, '-filters'], timeout=5)
    FFMPEG_HAS_ZSCALE = b'zscale' in out
    print(f'HDR tone-mapping (zscale): {"yes" if FFMPEG_HAS_ZSCALE else "no (colorspace fallback)"}')
    # Detect GPU encoder (NVENC / AMF / QSV)
    GPU_ENCODER = _detect_gpu_encoder()
    label = GPU_ENCODER if GPU_ENCODER else 'libx264 (CPU)'
    print(f'Video encoder: {label}')


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    check_ffmpeg()
    print('FFmpeg: OK')

    if len(sys.argv) > 1:
        folder = os.path.abspath(sys.argv[1])
        if not os.path.isdir(folder):
            print(f'ERROR: Folder does not exist: {folder}')
            sys.exit(1)
        print(f'Scanning: {folder}')
        clips = scan_folder(folder)
        if not clips:
            print(f'ERROR: No MP4 files in: {folder}')
            sys.exit(1)
        sel, title, subtitle, title2, disabled_days, day_titles, end_title, end_sub, music_ends, music_offsets, clip_order, clip_dur, has_saved = load_selections(folder)
        if not has_saved:
            title    = os.path.basename(folder.rstrip('/\\'))
            clip_dur = 3.0
            try:
                first = min(clips, key=lambda c: c.get('modified', ''))
                dt    = datetime.datetime.fromisoformat(first['modified'])
                subtitle = dt.strftime('%d-%m-%Y')
            except Exception:
                pass
        clips = apply_clip_order(clips, clip_order)
        music = scan_music(folder)
        # Apply saved track_end and track_offset values to music tracks
        for t in music:
            if t['filename'] in music_ends:
                t['track_end'] = float(music_ends[t['filename']])
            if t['filename'] in music_offsets:
                t['track_offset'] = float(music_offsets[t['filename']])
        with state_lock:
            state['folder']             = folder
            state['clips']              = clips
            state['selections']         = sel
            state['title']              = title
            state['title2']             = title2
            state['subtitle']           = subtitle
            state['music']              = music
            state['disabled_day_cards'] = disabled_days
            state['day_card_titles']    = day_titles
            state['end_card_title']     = end_title
            state['end_card_subtitle']  = end_sub
            state['clip_duration']      = clip_dur
        if not has_saved and folder:
            save_selections(folder, sel, title, subtitle, disabled_days, day_titles, end_title, end_sub, clip_order=[c['filename'] for c in clips], clip_duration=clip_dur)
        print(f'Loaded {len(clips)} clips, {len(music)} tracks')
        threading.Thread(target=pregenerate_hevc_previews, args=(folder, clips), daemon=True).start()

    server = ThreadedHTTPServer(('localhost', PORT), Handler)
    url = f'http://localhost:{PORT}'
    print(f'Server started: {url}')
    print('Ctrl+C to stop.')

    def _open():
        import time
        time.sleep(0.6)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
