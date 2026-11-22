"""
7Stream - Android Video Streaming Server
Built with Flet and aiohttp.
Supports direct Mobile Hotspot (offline) and Wi-Fi Router modes.
"""

import asyncio
import base64
import html
import inspect
import mimetypes
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import quote, unquote
import zlib

import aiohttp
from aiohttp import web
import flet as ft
import qrcode

# Platform-specific Linux/Android socket ioctl for SIOCGIFADDR
try:
    import fcntl
except ImportError:
    fcntl = None

SIOCGIFADDR = 0x8915

# Supported video extensions for streaming and playlist generation
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv", ".m4v",
    ".3gp", ".wmv", ".m2ts", ".vob", ".ogv", ".mpg", ".mpeg", ".m4p",
    ".mp2", ".mpe", ".mpv", ".m2v", ".divx", ".xvid", ".asf", ".rm",
    ".rmvb", ".m3u8", ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg",
    ".opus",
}

# Ensure comprehensive MIME mappings for mobile and desktop media
EXTRA_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
    ".flv": "video/x-flv",
    ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".m3u": "audio/x-mpegurl",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}
for ext, mime in EXTRA_MIME_TYPES.items():
    mimetypes.add_type(mime, ext)


# ============================================================================
# 1. Pure-Python QR Code PNG Generator (Zero Pillow / PIL Dependency)
# ============================================================================
def generate_qr_base64(data: str, scale: int = 6, border: int = 4) -> str:
    """
    Generates a pure-Python, RFC 2083-compliant PNG image of a QR code
    encoded in Base64. Uses only standard library zlib & struct.
    Avoids native Pillow / C-extensions that can break Android APK builds.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    orig_h = len(matrix)
    orig_w = len(matrix[0]) if orig_h > 0 else 0
    width = orig_w * scale
    height = orig_h * scale

    raw_rows = []
    for row in matrix:
        pixel_row = bytearray()
        for cell in row:
            # 0 for black module, 255 for white background
            val = 0 if cell else 255
            pixel_row.extend([val] * scale)
        # In PNG, each scanline begins with a filter-type byte (0 = None)
        scanline = b"\x00" + bytes(pixel_row)
        for _ in range(scale):
            raw_rows.append(scanline)

    raw_data = b"".join(raw_rows)
    compressed_data = zlib.compress(raw_data)

    def make_chunk(chunk_type: bytes, chunk_payload: bytes) -> bytes:
        length = struct.pack(">I", len(chunk_payload))
        crc = struct.pack(">I", zlib.crc32(chunk_type + chunk_payload) & 0xFFFFFFFF)
        return length + chunk_type + chunk_payload + crc

    png_bytes = bytearray(b"\x89PNG\r\n\x1a\n")
    # IHDR chunk: width (4), height (4), bit_depth (1=8), color_type (1=0 grayscale)
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    png_bytes.extend(make_chunk(b"IHDR", ihdr_data))
    png_bytes.extend(make_chunk(b"IDAT", compressed_data))
    png_bytes.extend(make_chunk(b"IEND", b""))

    return base64.b64encode(png_bytes).decode("ascii")


# ============================================================================
# 2. Offline Network Interface & IP Detection Engine
# ============================================================================
def get_interface_ip_ioctl(ifname: str) -> str | None:
    """Reads interface IP using Linux / Android SIOCGIFADDR ioctl."""
    if not fcntl:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifname_bytes = ifname[:15].encode("utf-8")
        res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", ifname_bytes))
        ip_bytes = res[20:24]
        s.close()
        ip = socket.inet_ntoa(ip_bytes)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return None


def get_ips_from_ip_command() -> dict[str, str]:
    """Inspects network interfaces using 'ip -4 -o addr' or 'ifconfig' fallback."""
    ip_map: dict[str, str] = {}
    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).decode("utf-8", errors="ignore")
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 4:
                ifname = parts[1].split("@")[0]
                ip_cidr = parts[3]
                ip = ip_cidr.split("/")[0]
                if not ip.startswith("127."):
                    ip_map[ifname] = ip
    except Exception:
        pass

    if not ip_map:
        try:
            output = subprocess.check_output(
                ["ifconfig"],
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).decode("utf-8", errors="ignore")
            current_if = None
            for line in output.splitlines():
                m_if = re.match(r"^([a-zA-Z0-9_\-]+)", line)
                if m_if:
                    current_if = m_if.group(1)
                m_ip = re.search(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", line)
                if m_ip and current_if and not m_ip.group(1).startswith("127."):
                    ip_map[current_if] = m_ip.group(1)
        except Exception:
            pass

    return ip_map


def get_all_host_ips() -> list[str]:
    """Gathers all available non-loopback IPv4 addresses on this device."""
    ips = []
    # Method A: socket.getaddrinfo
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = item[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    # Method B: socket.gethostbyname_ex
    try:
        _, _, host_ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in host_ips:
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    # Method C: Subprocess check
    cmd_ips = get_ips_from_ip_command()
    for ip in cmd_ips.values():
        if ip not in ips:
            ips.append(ip)

    return ips


def get_hotspot_ip() -> str:
    """
    Detects the phone's IP address when running as a Mobile Hotspot.
    Works 100% offline without mobile data or internet access.
    """
    hotspot_interfaces = ["ap0", "swlan0", "wlan1", "rndis0", "wlan0", "tether"]

    # 1. Direct Linux / Android ioctl inspection
    for iface in hotspot_interfaces:
        ip = get_interface_ip_ioctl(iface)
        if ip:
            return ip

    # 2. Command output inspection
    cmd_map = get_ips_from_ip_command()
    for iface in hotspot_interfaces:
        if iface in cmd_map:
            return cmd_map[iface]

    # 3. Check for typical Android tethering subnet (192.168.43.x)
    for ip in get_all_host_ips():
        if ip.startswith("192.168.43."):
            return ip

    # 4. UDP probe targeting local tethering broadcast
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.43.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # 5. Standard Android hotspot default gateway
    return "192.168.43.1"


def get_wifi_ip() -> str:
    """
    Detects the local IP address when connected to a standard Wi-Fi router.
    """
    wifi_interfaces = ["wlan0", "eth0", "en0", "wlan1"]

    # 1. Direct ioctl
    for iface in wifi_interfaces:
        ip = get_interface_ip_ioctl(iface)
        if ip:
            return ip

    # 2. Command output
    cmd_map = get_ips_from_ip_command()
    for iface in wifi_interfaces:
        if iface in cmd_map:
            return cmd_map[iface]

    # 3. Local subnet UDP probe (works offline without internet)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # 4. External DNS probe fallback
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # 5. Generic host IP probe
    all_ips = get_all_host_ips()
    # Filter out hotspot 192.168.43.x if possible
    wifi_candidates = [ip for ip in all_ips if not ip.startswith("192.168.43.")]
    if wifi_candidates:
        return wifi_candidates[0]
    if all_ips:
        return all_ips[0]

    return "127.0.0.1"


def detect_ip(mode: str) -> str:
    """Returns detected IP for either 'hotspot' or 'wifi' mode."""
    if mode == "hotspot":
        return get_hotspot_ip()
    return get_wifi_ip()


def get_default_media_dir() -> str:
    """Returns the default directory for media files on Android or Desktop."""
    android_candidates = [
        Path("/storage/emulated/0/Movies"),
        Path("/storage/emulated/0/Download"),
        Path("/sdcard/Movies"),
        Path("/sdcard/Download"),
    ]
    for p in android_candidates:
        if p.exists() and p.is_dir():
            return str(p)

    if Path("/storage/emulated/0").exists():
        movies_dir = Path("/storage/emulated/0/Movies")
        try:
            movies_dir.mkdir(parents=True, exist_ok=True)
            return str(movies_dir)
        except Exception:
            pass

    home = Path.home()
    for folder_name in ["Videos", "Movies", "Downloads"]:
        cand = home / folder_name
        if cand.exists() and cand.is_dir():
            return str(cand)

    return str(home)


# ============================================================================
# 3. HTML Directory Listing & Streaming Web UI
# ============================================================================
def format_size(bytes_num: int) -> str:
    """Formats raw byte counts into human-readable strings."""
    if bytes_num < 1024:
        return f"{bytes_num} B"
    elif bytes_num < 1024 * 1024:
        return f"{bytes_num / 1024:.1f} KB"
    elif bytes_num < 1024 * 1024 * 1024:
        return f"{bytes_num / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_num / (1024 * 1024 * 1024):.2f} GB"


def srt_to_vtt(srt_content: str) -> str:
    """Converts SubRip (.srt) subtitles into WebVTT (.vtt) format for HTML5 video."""
    lines = ["WEBVTT", ""]
    # Replace comma in timestamps: 00:01:20,123 -> 00:01:20.123
    cleaned = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", srt_content)
    lines.append(cleaned)
    return "\n".join(lines)


class AndroidWakeLockManager:
    """Acquires a PARTIAL_WAKE_LOCK and High-Perf WifiLock on Android so streaming continues when minimized or locked."""

    def __init__(self):
        self.wake_lock = None
        self.wifi_lock = None

    def _get_app(self):
        try:
            from jnius import autoclass
            try:
                ActivityThread = autoclass("android.app.ActivityThread")
                app = ActivityThread.currentApplication()
                if app:
                    return app
            except Exception:
                pass
            for act_class in [
                "org.kivy.android.PythonActivity",
                "com.sevenstream.app.MainActivity",
                "io.flutter.embedding.android.FlutterActivity",
            ]:
                try:
                    act = autoclass(act_class).mActivity
                    if act:
                        return act
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def acquire(self):
        try:
            app = self._get_app()
            if app:
                from jnius import autoclass
                Context = autoclass("android.content.Context")
                # 1. CPU WakeLock (PARTIAL_WAKE_LOCK = 1)
                try:
                    pm = app.getSystemService(Context.POWER_SERVICE)
                    self.wake_lock = pm.newWakeLock(1, "7Stream::BackgroundStreaming")
                    if self.wake_lock and not self.wake_lock.isHeld():
                        self.wake_lock.acquire()
                except Exception:
                    pass

                # 2. Wi-Fi Lock (WIFI_MODE_FULL_HIGH_PERF = 3)
                try:
                    wm = app.getSystemService(Context.WIFI_SERVICE)
                    self.wifi_lock = wm.createWifiLock(3, "7Stream::WifiLock")
                    if self.wifi_lock and not self.wifi_lock.isHeld():
                        self.wifi_lock.acquire()
                except Exception:
                    pass
        except Exception:
            pass

    def release(self):
        try:
            if self.wake_lock and hasattr(self.wake_lock, "isHeld") and self.wake_lock.isHeld():
                self.wake_lock.release()
        except Exception:
            pass
        self.wake_lock = None

        try:
            if self.wifi_lock and hasattr(self.wifi_lock, "isHeld") and self.wifi_lock.isHeld():
                self.wifi_lock.release()
        except Exception:
            pass
        self.wifi_lock = None


def count_videos_in_dir(d_path: Path) -> int:
    """Quickly counts video files directly in or inside d_path."""
    count = 0
    try:
        for root, dirs, files in os.walk(d_path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if Path(f).suffix.lower() in VIDEO_EXTENSIONS:
                    count += 1
    except Exception:
        pass
    return count


def render_html_page(
    base_dir: Path, current_path: Path, rel_path: str, host_header: str, view_mode: str = "folders"
) -> str:
    """
    Renders a responsive, modern dark-mode media browser.
    Supports both Folder-by-Folder Navigation and All-Videos Recursive View.
    Zero external CDNs so it operates 100% offline.
    """
    total_library_videos = count_videos_in_dir(base_dir)

    if view_mode == "all":
        # Recursive view of ALL video files in the library
        all_videos_list = []
        try:
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in sorted(files):
                    if fname.startswith("."):
                        continue
                    ext = Path(fname).suffix.lower()
                    if ext in VIDEO_EXTENSIONS:
                        full_fpath = Path(root) / fname
                        try:
                            rel = full_fpath.relative_to(base_dir)
                            url_rel = str(rel).replace(chr(92), "/")
                            all_videos_list.append((full_fpath, fname, url_rel, ext))
                        except Exception:
                            continue
        except Exception:
            all_videos_list = []

        videos_html = []
        for full_p, fname, url_rel, ext in all_videos_list:
            try:
                stat = full_p.stat()
                size_str = format_size(stat.st_size)
                mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
            except Exception:
                size_str = "Unknown"
                mtime_str = ""

            stream_url = f"/video/{quote(url_rel)}"
            full_stream_url = f"http://{host_header}{stream_url}"
            safe_name = html.escape(fname)
            parent_rel = str(Path(url_rel).parent).replace(chr(92), "/")
            folder_tag = f"📁 {parent_rel}" if parent_rel and parent_rel != "." else "📁 Root"

            videos_html.append(f"""
            <div class="card card-video" data-name="{safe_name.lower()}">
                <div class="card-icon">🎬</div>
                <div class="card-info">
                    <div class="card-title" title="{safe_name}">{safe_name}</div>
                    <div class="card-meta">
                        <span class="badge badge-ext">{ext.upper().replace('.', '')}</span>
                        <span class="badge badge-size">{size_str}</span>
                        <span class="badge badge-folder" title="Location">{folder_tag}</span>
                        <span class="meta-date">{mtime_str}</span>
                    </div>
                </div>
                <div class="card-actions">
                    <button class="btn btn-play" data-url="{stream_url}" data-title="{quote(fname)}" data-rel="{quote(url_rel)}" onclick="handlePlay(this)">
                        ▶ Play
                    </button>
                    <button class="btn btn-vlc" data-url="{full_stream_url}" onclick="handleVlc(this)" title="Play directly in VLC Player">
                        🎬 VLC
                    </button>
                    <a href="{stream_url}" download="{safe_name}" class="btn btn-download" title="Download Video">
                        ⬇
                    </a>
                    <button class="btn btn-copy" data-url="{full_stream_url}" onclick="handleCopy(this)" title="Copy VLC Stream URL">
                        🔗 Copy
                    </button>
                </div>
            </div>
            """)

        main_items_html = "\n".join(videos_html) if videos_html else (
            '<div class="empty-state">No video files found anywhere in this library.<br><br>'
            '💡 If you have videos in your phone, ensure "All files access" is granted in 7Stream app settings.</div>'
        )
        breadcrumbs_html = '<span class="crumb-current">🎥 All Videos (Entire Library)</span>'
        stats_text = f"{len(all_videos_list)} Video(s) across all folders"

    else:
        # Standard folder-by-folder view
        try:
            entries = sorted(
                os.scandir(current_path),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except Exception:
            entries = []

        subdirs = []
        video_files = []
        other_files = []

        for entry in entries:
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    subdirs.append(entry)
                elif entry.is_file():
                    ext = Path(entry.name).suffix.lower()
                    if ext in VIDEO_EXTENSIONS:
                        video_files.append(entry)
                    else:
                        other_files.append(entry)
            except Exception:
                continue

        # Breadcrumbs
        parts = [p for p in rel_path.strip("/").split("/") if p]
        breadcrumbs_html = '<a href="/?view=folders" class="crumb-link">🏠 Root</a>'
        built_path = ""
        for idx, part in enumerate(parts):
            built_path += "/" + quote(part)
            safe_part = html.escape(part)
            if idx == len(parts) - 1:
                breadcrumbs_html += f' <span class="sep">/</span> <span class="crumb-current">{safe_part}</span>'
            else:
                breadcrumbs_html += f' <span class="sep">/</span> <a href="/browse{built_path}?view=folders" class="crumb-link">{safe_part}</a>'

        # Parent directory link
        parent_link_html = ""
        if parts:
            parent_rel = "/".join(parts[:-1])
            parent_url = f"/browse/{quote(parent_rel)}?view=folders" if parent_rel else "/?view=folders"
            parent_link_html = f"""
            <a href="{parent_url}" class="card card-parent">
                <div class="card-icon">📁⬆️</div>
                <div class="card-info">
                    <div class="card-title">.. (Parent Directory)</div>
                    <div class="card-meta">Go up one level</div>
                </div>
            </a>
            """

        # Subdirectories with recursive video counts
        folders_html = []
        for d in subdirs:
            sub_rel = f"{rel_path.strip('/')}/{d.name}".strip("/") if rel_path else d.name
            folder_url = f"/browse/{quote(sub_rel)}?view=folders"
            safe_name = html.escape(d.name)
            try:
                vids_in_sub = count_videos_in_dir(Path(d.path))
                vids_meta = f"📁 Folder • {vids_in_sub} video(s) inside" if vids_in_sub > 0 else "📁 Folder"
            except Exception:
                vids_meta = "📁 Folder"

            folders_html.append(f"""
            <a href="{folder_url}" class="card card-folder" data-name="{safe_name.lower()}">
                <div class="card-icon">📁</div>
                <div class="card-info">
                    <div class="card-title">{safe_name}</div>
                    <div class="card-meta">{vids_meta}</div>
                </div>
                <div class="card-arrow">➔</div>
            </a>
            """)

        # Direct video files
        videos_html = []
        for f in video_files:
            try:
                stat = f.stat()
                size_str = format_size(stat.st_size)
                mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
            except Exception:
                size_str = "Unknown"
                mtime_str = ""

            f_rel = f"{rel_path.strip('/')}/{f.name}".strip("/") if rel_path else f.name
            stream_url = f"/video/{quote(f_rel)}"
            full_stream_url = f"http://{host_header}{stream_url}"
            safe_name = html.escape(f.name)
            ext = Path(f.name).suffix.lower()

            videos_html.append(f"""
            <div class="card card-video" data-name="{safe_name.lower()}">
                <div class="card-icon">🎬</div>
                <div class="card-info">
                    <div class="card-title" title="{safe_name}">{safe_name}</div>
                    <div class="card-meta">
                        <span class="badge badge-ext">{ext.upper().replace('.', '')}</span>
                        <span class="badge badge-size">{size_str}</span>
                        <span class="meta-date">{mtime_str}</span>
                    </div>
                </div>
                <div class="card-actions">
                    <button class="btn btn-play" data-url="{stream_url}" data-title="{quote(f.name)}" data-rel="{quote(f_rel)}" onclick="handlePlay(this)">
                        ▶ Play
                    </button>
                    <button class="btn btn-vlc" data-url="{full_stream_url}" onclick="handleVlc(this)" title="Play directly in VLC Player">
                        🎬 VLC
                    </button>
                    <a href="{stream_url}" download="{safe_name}" class="btn btn-download" title="Download Video">
                        ⬇
                    </a>
                    <button class="btn btn-copy" data-url="{full_stream_url}" onclick="handleCopy(this)" title="Copy VLC Stream URL">
                        🔗 Copy
                    </button>
                </div>
            </div>
            """)

        # Other media or files
        other_html = []
        for f in other_files:
            try:
                size_str = format_size(f.stat().st_size)
            except Exception:
                size_str = ""
            f_rel = f"{rel_path.strip('/')}/{f.name}".strip("/") if rel_path else f.name
            file_url = f"/video/{quote(f_rel)}"
            safe_name = html.escape(f.name)
            ext = Path(f.name).suffix.lower()
            other_html.append(f"""
            <div class="card card-other" data-name="{safe_name.lower()}">
                <div class="card-icon">📄</div>
                <div class="card-info">
                    <div class="card-title">{safe_name}</div>
                    <div class="card-meta">{ext.upper().replace('.', '')} • {size_str}</div>
                </div>
                <div class="card-actions">
                    <a href="{file_url}" download="{safe_name}" class="btn btn-download">⬇ Download</a>
                </div>
            </div>
            """)

        folder_block = "\n".join(folders_html)
        video_block = "\n".join(videos_html)
        other_block = "\n".join(other_html)

        empty_message = ""
        if not video_files and subdirs:
            empty_message = (
                f'<div class="info-note">📁 This directory has {len(subdirs)} subfolder(s). '
                f'Click any folder above, or switch to <a href="/?view=all" style="color:var(--primary);font-weight:bold;">All Videos View</a> '
                f'to see all {total_library_videos} video(s) at once.</div>'
            )
        elif not video_files and not subdirs:
            empty_message = '<div class="empty-state">No video files or folders found here.</div>'

        main_items_html = f"{parent_link_html}\n{folder_block}\n{video_block}\n{other_block}\n{empty_message}"
        stats_text = f"{len(video_files)} Video(s), {len(subdirs)} Folder(s)"

    playlist_param = f"?path={quote(rel_path)}" if rel_path else ""
    vlc_playlist_url = f"/playlist.m3u{playlist_param}"

    active_folders_class = "active" if view_mode != "all" else ""
    active_all_class = "active" if view_mode == "all" else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>7Stream Media Server - by Salman Sarod</title>
    <style>
        :root {{
            --bg: #0b0f19;
            --surface: #141c2e;
            --surface-hover: #1b263b;
            --border: #233554;
            --primary: #38bdf8;
            --primary-hover: #0ea5e9;
            --text: #f1f5f9;
            --text-muted: #94a3b8;
            --accent-green: #10b981;
            --accent-purple: #a855f7;
            --danger: #ef4444;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}
        body {{
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }}
        header {{
            background: linear-gradient(180deg, #162033 0%, var(--surface) 100%);
            border-bottom: 1px solid var(--border);
            padding: 14px 20px;
            position: sticky;
            top: 0;
            z-index: 50;
            box-shadow: 0 4px 20px rgba(0,0,0,0.4);
        }}
        .header-wrap {{
            max-width: 1100px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            flex-wrap: wrap;
        }}
        .logo-group {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .logo-icon {{
            font-size: 26px;
            background: rgba(56, 189, 248, 0.15);
            padding: 6px;
            border-radius: 10px;
            border: 1px solid rgba(56, 189, 248, 0.3);
        }}
        .logo-title {{
            font-size: 1.2rem;
            font-weight: 700;
            color: #ffffff;
        }}
        .logo-author {{
            font-size: 0.76rem;
            color: var(--primary);
            font-weight: 500;
        }}
        .view-switcher {{
            display: flex;
            gap: 6px;
            background: rgba(0,0,0,0.3);
            padding: 4px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .tab-btn {{
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.85rem;
            font-weight: 600;
            text-decoration: none;
            color: var(--text-muted);
            transition: all 0.2s;
        }}
        .tab-btn.active {{
            background: var(--primary);
            color: #0c4a6e;
        }}
        .tab-btn:hover:not(.active) {{
            color: #ffffff;
            background: rgba(255,255,255,0.08);
        }}
        .header-actions {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .btn-header {{
            background: var(--accent-green);
            color: #064e3b;
            font-weight: 600;
            padding: 8px 14px;
            border-radius: 8px;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 0.85rem;
        }}
        main {{
            max-width: 1100px;
            width: 100%;
            margin: 0 auto;
            padding: 20px;
            flex: 1;
        }}
        .breadcrumbs-bar {{
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 10px 16px;
            border-radius: 10px;
            margin-bottom: 16px;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 8px;
            overflow-x: auto;
            white-space: nowrap;
        }}
        .crumb-link {{
            color: var(--primary);
            text-decoration: none;
            font-weight: 500;
        }}
        .crumb-current {{
            color: var(--text);
            font-weight: 600;
        }}
        .sep {{
            color: var(--text-muted);
        }}
        .toolbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .search-box {{
            flex: 1;
            min-width: 240px;
            position: relative;
        }}
        .search-input {{
            width: 100%;
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 10px 14px 10px 36px;
            border-radius: 8px;
            font-size: 0.9rem;
            outline: none;
        }}
        .search-input:focus {{
            border-color: var(--primary);
        }}
        .search-icon {{
            position: absolute;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
            pointer-events: none;
        }}
        .stats-badge {{
            font-size: 0.85rem;
            color: var(--text-muted);
            background: var(--surface);
            padding: 8px 12px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .grid {{
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 14px 18px;
            display: flex;
            align-items: center;
            gap: 16px;
            text-decoration: none;
            color: inherit;
            transition: all 0.2s ease;
        }}
        .card:hover {{
            background: var(--surface-hover);
            border-color: rgba(56, 189, 248, 0.4);
            transform: translateY(-1px);
        }}
        .card-icon {{
            font-size: 24px;
            flex-shrink: 0;
        }}
        .card-info {{
            flex: 1;
            min-width: 0;
        }}
        .card-title {{
            font-size: 0.95rem;
            font-weight: 600;
            color: #ffffff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 4px;
        }}
        .card-meta {{
            font-size: 0.8rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .badge {{
            padding: 2px 7px;
            border-radius: 6px;
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-ext {{
            background: rgba(56, 189, 248, 0.15);
            color: var(--primary);
            border: 1px solid rgba(56, 189, 248, 0.3);
        }}
        .badge-size {{
            background: rgba(255, 255, 255, 0.08);
            color: #e2e8f0;
        }}
        .badge-folder {{
            background: rgba(168, 85, 247, 0.15);
            color: #d8b4fe;
            border: 1px solid rgba(168, 85, 247, 0.3);
            text-transform: none;
        }}
        .card-arrow {{
            color: var(--text-muted);
            font-size: 1.1rem;
        }}
        .card-actions {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-shrink: 0;
            flex-wrap: wrap;
        }}
        .btn {{
            padding: 7px 12px;
            border-radius: 8px;
            font-size: 0.84rem;
            font-weight: 600;
            cursor: pointer;
            border: none;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .btn-play {{
            background: var(--primary);
            color: #0c4a6e;
        }}
        .btn-vlc {{
            background: rgba(249, 115, 22, 0.18);
            color: #fdba74;
            border: 1px solid rgba(249, 115, 22, 0.35);
        }}
        .btn-vlc:hover {{
            background: rgba(249, 115, 22, 0.3);
            color: #ffedd5;
        }}
        .btn-download {{
            background: var(--surface-hover);
            color: var(--text);
            border: 1px solid var(--border);
        }}
        .btn-copy {{
            background: rgba(168, 85, 247, 0.15);
            color: #d8b4fe;
            border: 1px solid rgba(168, 85, 247, 0.3);
        }}
        .info-note {{
            background: rgba(56, 189, 248, 0.08);
            border: 1px solid rgba(56, 189, 248, 0.25);
            padding: 16px;
            border-radius: 12px;
            color: #cbd5e1;
            font-size: 0.9rem;
            line-height: 1.5;
        }}
        .empty-state {{
            text-align: center;
            padding: 40px 20px;
            color: var(--text-muted);
            background: var(--surface);
            border: 1px dashed var(--border);
            border-radius: 12px;
            font-size: 0.95rem;
            line-height: 1.6;
        }}
        footer {{
            border-top: 1px solid var(--border);
            padding: 14px 20px;
            text-align: center;
            font-size: 0.82rem;
            color: var(--text-muted);
            background: var(--surface);
        }}
        /* Video Player Modal */
        .modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.88);
            z-index: 100;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .modal-content {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 16px;
            max-width: 900px;
            width: 100%;
            overflow: hidden;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.7);
        }}
        .modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 18px;
            border-bottom: 1px solid var(--border);
        }}
        .modal-title {{
            font-size: 1rem;
            font-weight: 600;
            color: #ffffff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .modal-close {{
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 24px;
            cursor: pointer;
            padding: 4px 8px;
        }}
        .video-wrapper {{
            background: #000000;
            position: relative;
            padding-top: 56.25%;
        }}
        .video-wrapper video {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
        }}
        .modal-track-bar {{
            background: #0f172a;
            border-bottom: 1px solid var(--border);
            padding: 10px 18px;
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }}
        .track-control-group {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }}
        .track-label {{
            font-size: 0.82rem;
            font-weight: 600;
            color: #94a3b8;
        }}
        .track-select {{
            background: #1e293b;
            color: #f1f5f9;
            border: 1px solid var(--border);
            padding: 5px 10px;
            border-radius: 6px;
            font-size: 0.82rem;
            outline: none;
            max-width: 220px;
        }}
        .track-select:focus {{
            border-color: var(--primary);
        }}
        .btn-sub-load {{
            background: rgba(56, 189, 248, 0.15);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.3);
            padding: 5px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .btn-sub-load:hover {{
            background: rgba(56, 189, 248, 0.25);
        }}
        .modal-footer {{
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.84rem;
            color: var(--text-muted);
            background: #111827;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .modal-hint {{
            font-size: 0.78rem;
            color: #94a3b8;
        }}
        .modal-actions-bar {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .toast {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--accent-green);
            color: #064e3b;
            font-weight: 600;
            padding: 10px 18px;
            border-radius: 8px;
            opacity: 0;
            transform: translateY(100px);
            transition: all 0.3s ease;
            z-index: 200;
        }}
        .toast.show {{
            transform: translateY(0);
            opacity: 1;
        }}
    </style>
</head>
<body>
    <header>
        <div class="header-wrap">
            <div class="logo-group">
                <div class="logo-icon">🎬</div>
                <div>
                    <div class="logo-title">7Stream Server</div>
                    <div class="logo-author">By Salman Sarod</div>
                </div>
            </div>

            <div class="view-switcher">
                <a href="/?view=folders" class="tab-btn {active_folders_class}">📁 Folders</a>
                <a href="/?view=all" class="tab-btn {active_all_class}">🎥 All Videos ({total_library_videos})</a>
            </div>

            <div class="header-actions">
                <a href="{vlc_playlist_url}" class="btn-header" title="Download recursive .m3u playlist for VLC">
                    📋 Open in VLC (.m3u)
                </a>
            </div>
        </div>
    </header>

    <main>
        <div class="breadcrumbs-bar">
            {breadcrumbs_html}
        </div>

        <div class="toolbar">
            <div class="search-box">
                <span class="search-icon">🔍</span>
                <input type="text" id="filterInput" class="search-input" placeholder="Search videos or folders..." oninput="filterList()">
            </div>
            <div class="stats-badge" id="statsBadge">
                {stats_text}
            </div>
        </div>

        <div class="grid" id="itemsList">
            {main_items_html}
        </div>
    </main>

    <footer>
        7Stream Server &bull; Developed by Salman Sarod &bull; Ultra-Fast Local LAN &amp; Hotspot Streaming
    </footer>

    <!-- Video Player Modal -->
    <div id="videoModal" class="modal" onclick="onModalClick(event)">
        <div class="modal-content">
            <div class="modal-header">
                <div class="modal-title" id="playerTitle">Now Playing</div>
                <button class="modal-close" onclick="closePlayer()">&times;</button>
            </div>
            <div class="video-wrapper">
                <video id="htmlPlayer" controls autoplay playsinline preload="metadata">
                    Your browser does not support HTML5 video playback.
                </video>
            </div>
            <div class="modal-track-bar">
                <div class="track-control-group" id="audioTrackGroup">
                    <span class="track-label">🗣️ Audio:</span>
                    <select id="audioTrackSelect" class="track-select" onchange="onAudioTrackChange()">
                        <option value="default">Default Audio</option>
                    </select>
                </div>
                <div class="track-control-group">
                    <span class="track-label">💬 Subtitles:</span>
                    <select id="subTrackSelect" class="track-select" onchange="onSubTrackChange()">
                        <option value="off">Subtitles: Off</option>
                    </select>
                    <label for="subFileInput" class="btn btn-sub-load" title="Load custom subtitle file from your device">
                        📂 + Load Subtitle (.srt / .vtt)
                    </label>
                    <input type="file" id="subFileInput" accept=".srt,.vtt,.sub" style="display:none" onchange="loadCustomSubtitle(event)">
                </div>
            </div>
            <div class="modal-footer">
                <div class="modal-hint">
                    💡 Multi-language MKV? Tap <b>Open in VLC</b> for all audio tracks & embedded subtitles.
                </div>
                <div class="modal-actions-bar">
                    <button class="btn btn-vlc" onclick="openCurrentInVLC()" title="Open directly in VLC Media Player">
                        🎬 Open in VLC
                    </button>
                    <button class="btn btn-copy" id="copyModalBtn" onclick="copyCurrentVideoUrl()">
                        🔗 Copy Stream Link
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div id="toast" class="toast">Link copied to clipboard!</div>

    <script>
        let currentVideoUrl = "";
        let currentVideoRel = "";

        function handlePlay(btn) {{
            const url = btn.getAttribute("data-url");
            const title = decodeURIComponent(btn.getAttribute("data-title") || "");
            const rel = decodeURIComponent(btn.getAttribute("data-rel") || "");
            openPlayer(url, title, rel);
        }}

        function handleVlc(btn) {{
            const url = btn.getAttribute("data-url");
            openDirectlyInVLC(url);
        }}

        function handleCopy(btn) {{
            const url = btn.getAttribute("data-url");
            copyText(url);
        }}

        function openPlayer(url, title, relPath) {{
            currentVideoUrl = window.location.origin + url;
            currentVideoRel = relPath || "";
            document.getElementById("playerTitle").innerText = title;
            const video = document.getElementById("htmlPlayer");

            clearVideoTracks();
            video.src = url;
            video.load();

            document.getElementById("videoModal").style.display = "flex";
            video.play().catch(e => console.log("Autoplay prevented:", e));

            video.onloadedmetadata = () => {{
                updateAudioTracks();
            }};

            fetchSubtitlesForVideo(currentVideoRel);
        }}

        function clearVideoTracks() {{
            const video = document.getElementById("htmlPlayer");
            while (video.querySelector("track")) {{
                video.removeChild(video.querySelector("track"));
            }}
            const subSel = document.getElementById("subTrackSelect");
            subSel.innerHTML = '<option value="off">Subtitles: Off</option>';
            const audioSel = document.getElementById("audioTrackSelect");
            audioSel.innerHTML = '<option value="default">Default Audio</option>';
        }}

        function updateAudioTracks() {{
            const video = document.getElementById("htmlPlayer");
            const sel = document.getElementById("audioTrackSelect");
            sel.innerHTML = '';
            if (video.audioTracks && video.audioTracks.length > 0) {{
                for (let i = 0; i < video.audioTracks.length; i++) {{
                    const t = video.audioTracks[i];
                    const opt = document.createElement("option");
                    opt.value = i;
                    const langLabel = t.label || t.language || ("Track " + (i + 1));
                    opt.text = langLabel + (t.enabled ? ' (Active)' : '');
                    if (t.enabled) opt.selected = true;
                    sel.appendChild(opt);
                }}
            }} else {{
                const opt = document.createElement("option");
                opt.value = "default";
                opt.text = "Default Audio (Use VLC for Dual-Audio)";
                sel.appendChild(opt);
            }}
        }}

        function onAudioTrackChange() {{
            const video = document.getElementById("htmlPlayer");
            const sel = document.getElementById("audioTrackSelect");
            const idx = parseInt(sel.value);
            if (video.audioTracks && !isNaN(idx)) {{
                for (let i = 0; i < video.audioTracks.length; i++) {{
                    video.audioTracks[i].enabled = (i === idx);
                }}
                showToast("Switched to audio track " + (idx + 1));
            }}
        }}

        function fetchSubtitlesForVideo(relPath) {{
            if (!relPath) return;
            const subSel = document.getElementById("subTrackSelect");
            fetch("/api/subtitles?video=" + encodeURIComponent(relPath))
                .then(res => res.json())
                .then(items => {{
                    if (Array.isArray(items) && items.length > 0) {{
                        items.forEach(item => {{
                            const opt = document.createElement("option");
                            opt.value = item.url;
                            opt.text = "💬 " + item.label;
                            subSel.appendChild(opt);
                        }});
                        subSel.selectedIndex = 1;
                        onSubTrackChange();
                    }}
                }})
                .catch(e => console.log("Subtitle discovery note:", e));
        }}

        function onSubTrackChange() {{
            const video = document.getElementById("htmlPlayer");
            const sel = document.getElementById("subTrackSelect");
            const val = sel.value;

            while (video.querySelector("track")) {{
                video.removeChild(video.querySelector("track"));
            }}

            if (val === "off") {{
                for (let i = 0; i < video.textTracks.length; i++) {{
                    video.textTracks[i].mode = "disabled";
                }}
                showToast("Subtitles turned off");
                return;
            }}

            const track = document.createElement("track");
            track.kind = "subtitles";
            track.label = sel.options[sel.selectedIndex].text;
            track.srclang = "en";
            track.src = val;
            track.default = true;

            track.onload = function() {{
                this.track.mode = "showing";
            }};

            video.appendChild(track);
            try {{
                if (video.textTracks.length > 0) {{
                    video.textTracks[video.textTracks.length - 1].mode = "showing";
                }}
            }} catch (e) {{}}
            showToast("Subtitles loaded: " + sel.options[sel.selectedIndex].text);
        }}

        function loadCustomSubtitle(event) {{
            const file = event.target.files && event.target.files[0];
            if (!file) return;

            const reader = new FileReader();
            reader.onload = function(e) {{
                let content = e.target.result;
                if (file.name.toLowerCase().endsWith(".srt") || !content.startsWith("WEBVTT")) {{
                    const nl = String.fromCharCode(10);
                    const vttHeader = ["WEBVTT", "", ""].join(nl);
                    content = vttHeader + content.replace(new RegExp("(\\d{{2}}:\\d{{2}}:\\d{{2}}),(\\d{{3}})", "g"), "$1.$2");
                }}
                const blob = new Blob([content], {{ type: "text/vtt" }});
                const blobUrl = URL.createObjectURL(blob);

                const subSel = document.getElementById("subTrackSelect");
                const opt = document.createElement("option");
                opt.value = blobUrl;
                opt.text = "📂 " + file.name;
                subSel.appendChild(opt);
                subSel.value = blobUrl;
                onSubTrackChange();
                showToast("Loaded: " + file.name);
            }};
            reader.readAsText(file);
        }}

        function closePlayer() {{
            const modal = document.getElementById("videoModal");
            const video = document.getElementById("htmlPlayer");
            video.pause();
            video.removeAttribute("src");
            clearVideoTracks();
            video.load();
            modal.style.display = "none";
        }}

        function onModalClick(e) {{
            if (e.target.id === "videoModal") {{
                closePlayer();
            }}
        }}

        document.addEventListener("keydown", (e) => {{
            if (e.key === "Escape") {{
                closePlayer();
            }}
        }});

        function openDirectlyInVLC(url) {{
            const cleanUrl = url.replace("http://", "").replace("https://", "");
            const androidIntent = "intent://" + cleanUrl + "#Intent;scheme=http;package=org.videolan.vlc;type=video/*;end";
            const vlcUri = "vlc://" + url;

            const isAndroid = /Android/i.test(navigator.userAgent);
            if (isAndroid) {{
                window.location.href = androidIntent;
                setTimeout(() => {{
                    window.location.href = vlcUri;
                }}, 1200);
            }} else {{
                window.location.href = vlcUri;
            }}
            showToast("Opening VLC Player...");
        }}

        function openCurrentInVLC() {{
            if (currentVideoUrl) {{
                openDirectlyInVLC(currentVideoUrl);
            }}
        }}

        function copyText(text) {{
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(text).then(() => {{
                    showToast("Stream URL copied to clipboard!");
                }}).catch(() => {{
                    fallbackCopy(text);
                }});
            }} else {{
                fallbackCopy(text);
            }}
        }}

        function fallbackCopy(text) {{
            let successful = false;
            try {{
                const el = document.createElement('textarea');
                el.value = text;
                el.setAttribute('readonly', '');
                el.style.position = 'fixed';
                el.style.opacity = '0';
                el.style.left = '-9999px';
                document.body.appendChild(el);
                el.focus();
                el.select();
                successful = document.execCommand('copy');
                document.body.removeChild(el);
            }} catch (err) {{
                successful = false;
            }}
            if (successful) {{
                showToast("Stream URL copied to clipboard!");
            }} else {{
                window.prompt("Copy Video Stream Link:", text);
            }}
        }}

        function copyCurrentVideoUrl() {{
            if (currentVideoUrl) {{
                copyText(currentVideoUrl);
            }}
        }}

        function showToast(msg) {{
            const toast = document.getElementById("toast");
            toast.innerText = msg;
            toast.classList.add("show");
            setTimeout(() => {{
                toast.classList.remove("show");
            }}, 2600);
        }}

        function filterList() {{
            const query = document.getElementById("filterInput").value.toLowerCase().trim();
            const cards = document.querySelectorAll(".card[data-name]");
            cards.forEach(card => {{
                const name = card.getAttribute("data-name") || "";
                if (name.includes(query)) {{
                    card.style.display = "flex";
                }} else {{
                    card.style.display = "none";
                }}
            }});
        }}
    </script>
</body>
</html>
"""


class VideoServerManager:
    """
    Manages the background aiohttp web server inside a dedicated daemon thread.
    Natively supports HTTP 206 Partial Content byte ranges via web.FileResponse.
    Holds Android WakeLock so streaming persists even when phone is minimized.
    """

    def __init__(self):
        self.media_root: Path = Path(get_default_media_dir())
        self.host: str = "0.0.0.0"
        self.port: int = 8080
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.runner: web.AppRunner | None = None
        self.is_running: bool = False
        self.start_error: str | None = None
        self.wakelock = AndroidWakeLockManager()

    def set_config(self, media_root_path: str, port: int):
        self.media_root = Path(media_root_path).resolve()
        self.port = port

    def resolve_safe_path(self, req_path: str) -> Path:
        """
        Enforces strict path-traversal security.
        Ensures the requested relative path stays within self.media_root.
        """
        clean_rel = req_path.lstrip("/\\")
        target = (self.media_root / clean_rel).resolve()
        base = self.media_root.resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise web.HTTPForbidden(text="Access denied: Path traversal detected")
        return target

    async def handle_api_subtitles(self, request: web.Request) -> web.Response:
        """Returns matching subtitle files in the same directory as the video."""
        video_rel = unquote(request.query.get("video", ""))
        if not video_rel:
            return web.json_response([])
        try:
            target_vid = self.resolve_safe_path(video_rel)
            if not target_vid.exists():
                return web.json_response([])
            parent_d = target_vid.parent
            stem = target_vid.stem.lower()
            results = []
            for entry in os.scandir(parent_d):
                if entry.is_file():
                    ext = Path(entry.name).suffix.lower()
                    if ext in (".srt", ".vtt", ".sub"):
                        rel = Path(entry.path).relative_to(self.media_root)
                        url_rel = str(rel).replace(chr(92), "/")
                        label = entry.name
                        if ".en." in entry.name.lower() or entry.name.lower().endswith("eng.srt"):
                            label = "English"
                        elif ".es." in entry.name.lower():
                            label = "Spanish"
                        elif ".hi." in entry.name.lower():
                            label = "Hindi"
                        results.append({
                            "label": label,
                            "url": f"/subtitle/{quote(url_rel)}"
                        })
            return web.json_response(results, headers={"Access-Control-Allow-Origin": "*"})
        except Exception:
            return web.json_response([])

    async def handle_subtitle(self, request: web.Request) -> web.Response:
        """Serves WebVTT subtitle stream, converting SubRip (.srt) on the fly."""
        rel_path = unquote(request.match_info.get("tail", ""))
        target = self.resolve_safe_path(rel_path)
        if not target.exists() or not target.is_file():
            raise web.HTTPNotFound(text="Subtitle not found")
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                raw_text = f.read()
            if target.suffix.lower() == ".srt":
                vtt_text = srt_to_vtt(raw_text)
            else:
                vtt_text = raw_text
            return web.Response(
                text=vtt_text,
                content_type="text/vtt",
                charset="utf-8",
                headers={"Access-Control-Allow-Origin": "*"},
            )
        except Exception as e:
            raise web.HTTPInternalServerError(text=f"Error reading subtitle: {e}")

    async def handle_all_videos(self, request: web.Request) -> web.Response:
        """Serves recursive flat listing of all video files."""
        host_header = request.host
        html_content = render_html_page(
            self.media_root, self.media_root, "", host_header, view_mode="all"
        )
        return web.Response(
            text=html_content,
            content_type="text/html",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def handle_index(self, request: web.Request) -> web.Response:
        """Root endpoint: serves root directory listing."""
        return await self._serve_directory_or_file(request, "")

    async def handle_browse(self, request: web.Request) -> web.Response:
        """Subdirectory browsing endpoint."""
        rel_path = unquote(request.match_info.get("tail", ""))
        return await self._serve_directory_or_file(request, rel_path)

    async def handle_video(self, request: web.Request) -> web.StreamResponse:
        """Direct video streaming endpoint with native HTTP byte range support."""
        rel_path = unquote(request.match_info.get("tail", ""))
        target = self.resolve_safe_path(rel_path)

        if not target.exists() or not target.is_file():
            raise web.HTTPNotFound(text="Video file not found")

        ext = target.suffix.lower()
        content_type = EXTRA_MIME_TYPES.get(ext) or mimetypes.guess_type(target.name)[0] or "video/mp4"

        # web.FileResponse natively handles 'Range: bytes=...' headers with HTTP 206
        response = web.FileResponse(target)
        response.content_type = content_type
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Range, Content-Type"
        return response

    async def _serve_directory_or_file(
        self, request: web.Request, rel_path: str
    ) -> web.Response:
        target = self.resolve_safe_path(rel_path)

        if not target.exists():
            raise web.HTTPNotFound(text="Path does not exist")

        if target.is_file():
            # If user directly browsed to a file
            response = web.FileResponse(target)
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response

        # Target is a directory: generate HTML view
        host_header = request.host
        view_mode = request.query.get("view", "folders")
        html_content = render_html_page(
            self.media_root, target, rel_path, host_header, view_mode=view_mode
        )
        return web.Response(
            text=html_content,
            content_type="text/html",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def handle_playlist(self, request: web.Request) -> web.Response:
        """
        Generates an #EXTM3U playlist by recursively scanning the media directory.
        Allows VLC Media Player or smart TV players to stream the entire movie library.
        """
        sub_path = request.query.get("path", "")
        start_dir = self.resolve_safe_path(sub_path)
        if not start_dir.exists() or not start_dir.is_dir():
            start_dir = self.media_root

        host_header = request.host

        m3u_lines = ["#EXTM3U"]
        try:
            for root, dirs, files in os.walk(start_dir):
                # Exclude hidden directories
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in sorted(files):
                    if fname.startswith("."):
                        continue
                    ext = Path(fname).suffix.lower()
                    if ext in VIDEO_EXTENSIONS:
                        full_fpath = Path(root) / fname
                        try:
                            rel = full_fpath.relative_to(self.media_root)
                            url_rel = str(rel).replace(chr(92), "/")
                            stream_url = f"http://{host_header}/video/{quote(url_rel)}"
                            m3u_lines.append(f"#EXTINF:-1,{fname}")
                            m3u_lines.append(stream_url)
                        except Exception:
                            continue
        except Exception as err:
            m3u_lines.append(f"# Error generating playlist: {err}")

        playlist_content = "\n".join(m3u_lines) + "\n"
        return web.Response(
            text=playlist_content,
            content_type="audio/x-mpegurl",
            charset="utf-8",
            headers={
                "Content-Disposition": 'inline; filename="playlist.m3u"',
                "Access-Control-Allow-Origin": "*",
            },
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "server": "7Stream",
            "root": str(self.media_root),
        })

    def start(self) -> tuple[bool, str]:
        """Starts the server in a daemon background thread."""
        if self.is_running:
            return True, "Already running"

        if not self.media_root.exists():
            try:
                self.media_root.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return False, f"Invalid media directory: {e}"

        startup_event = threading.Event()
        self.start_error = None

        def run_thread():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            app = web.Application()
            app.router.add_get("/", self.handle_index)
            app.router.add_get("/all", self.handle_all_videos)
            app.router.add_get("/browse", self.handle_index)
            app.router.add_get(r"/browse/{tail:.*}", self.handle_browse)
            app.router.add_get(r"/video/{tail:.*}", self.handle_video)
            app.router.add_get(r"/subtitle/{tail:.*}", self.handle_subtitle)
            app.router.add_get("/api/subtitles", self.handle_api_subtitles)
            app.router.add_get("/playlist.m3u", self.handle_playlist)
            app.router.add_get("/health", self.handle_health)

            self.runner = web.AppRunner(app)
            try:
                self.loop.run_until_complete(self.runner.setup())
                site = web.TCPSite(self.runner, self.host, self.port)
                self.loop.run_until_complete(site.start())
                self.is_running = True
                startup_event.set()
            except Exception as e:
                self.start_error = str(e)
                self.is_running = False
                startup_event.set()
                return

            try:
                self.loop.run_forever()
            finally:
                self.loop.run_until_complete(self.runner.cleanup())
                self.loop.close()

        self.thread = threading.Thread(target=run_thread, daemon=True)
        self.thread.start()

        startup_event.wait(timeout=3.0)
        if self.is_running:
            self.wakelock.acquire()
            return True, "Server started successfully"
        return False, self.start_error or "Failed to start server"

    def stop(self):
        """Stops the server cleanly."""
        if not self.is_running or not self.loop:
            return

        async def shutdown():
            if self.runner:
                await self.runner.cleanup()
            if self.loop:
                self.loop.stop()

        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(shutdown()))
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.wakelock.release()
        self.is_running = False


# ============================================================================
# 5. Modern Flet Mobile GUI Application (Cinematic Dark Redesign)
# ============================================================================
def build_app(page: ft.Page):
    page.title = "7Stream - by Salman Sarod"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#070a12"
    page.padding = 16
    page.scroll = ft.ScrollMode.AUTO

    # Register Flet Clipboard Service
    clipboard = ft.Clipboard()
    try:
        page.services.append(clipboard)
    except Exception:
        pass

    # Android background execution & lifecycle management
    def on_app_lifecycle(e):
        pass

    try:
        page.on_app_lifecycle_state_change = on_app_lifecycle
    except Exception:
        pass

    server = VideoServerManager()
    active_mode = "hotspot"  # 'hotspot' or 'wifi'
    current_detected_ip = detect_ip(active_mode)

    # Safe cross-version border helper
    def make_border(color, width: float = 1.0):
        try:
            if hasattr(ft, "Border"):
                return ft.Border(
                    top=ft.BorderSide(width, color),
                    right=ft.BorderSide(width, color),
                    bottom=ft.BorderSide(width, color),
                    left=ft.BorderSide(width, color),
                )
        except Exception:
            pass
        return None

    # Snackbar notification
    def notify(msg: str):
        try:
            sb = ft.SnackBar(content=ft.Text(msg, color="#ffffff", size=13), bgcolor="#1e293b")
            if hasattr(page, "show_dialog"):
                try:
                    page.show_dialog(sb)
                    return
                except Exception:
                    pass
            if hasattr(page, "snack_bar"):
                page.snack_bar = sb
                sb.open = True
                page.update()
                return
            if hasattr(page, "overlay"):
                page.overlay.append(sb)
                sb.open = True
                page.update()
        except Exception:
            pass

    # Robust multi-engine Clipboard Copy
    def copy_to_clipboard(text_val: str):
        try:
            page.run_task(clipboard.set, text_val)
        except Exception:
            pass

        try:
            from jnius import autoclass
            app = None
            try:
                ActivityThread = autoclass("android.app.ActivityThread")
                app = ActivityThread.currentApplication()
            except Exception:
                pass
            if not app:
                for act_class in [
                    "org.kivy.android.PythonActivity",
                    "com.sevenstream.app.MainActivity",
                    "io.flutter.embedding.android.FlutterActivity",
                ]:
                    try:
                        act = autoclass(act_class).mActivity
                        if act:
                            app = act
                            break
                    except Exception:
                        pass
            if app:
                Context = autoclass("android.content.Context")
                ClipData = autoclass("android.content.ClipData")
                cm = app.getSystemService(Context.CLIPBOARD_SERVICE)
                clip = ClipData.newPlainText("7Stream", text_val)
                cm.setPrimaryClip(clip)
        except Exception:
            pass

        notify(f"Copied: {text_val}")

    # Launch VLC Player on Android
    def launch_vlc_app(stream_url: str):
        try:
            from jnius import autoclass
            app = None
            try:
                ActivityThread = autoclass("android.app.ActivityThread")
                app = ActivityThread.currentApplication()
            except Exception:
                pass
            if not app:
                for act_class in [
                    "org.kivy.android.PythonActivity",
                    "com.sevenstream.app.MainActivity",
                    "io.flutter.embedding.android.FlutterActivity",
                ]:
                    try:
                        act = autoclass(act_class).mActivity
                        if act:
                            app = act
                            break
                    except Exception:
                        pass
            if app:
                Intent = autoclass("android.content.Intent")
                Uri = autoclass("android.net.Uri")
                intent = Intent(Intent.ACTION_VIEW)
                intent.setDataAndType(Uri.parse(stream_url), "video/*")
                intent.setPackage("org.videolan.vlc")
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                app.startActivity(intent)
                notify("Launching VLC Player...")
                return
        except Exception:
            pass

        copy_to_clipboard(stream_url)
        notify("VLC playlist link copied! Open VLC > Stream to play.")

    # Android Background Battery Optimization Settings
    def open_battery_settings(e):
        try:
            from jnius import autoclass
            app = None
            try:
                ActivityThread = autoclass("android.app.ActivityThread")
                app = ActivityThread.currentApplication()
            except Exception:
                pass
            if not app:
                for act_class in [
                    "org.kivy.android.PythonActivity",
                    "com.sevenstream.app.MainActivity",
                    "io.flutter.embedding.android.FlutterActivity",
                ]:
                    try:
                        act = autoclass(act_class).mActivity
                        if act:
                            app = act
                            break
                    except Exception:
                        pass
            if app:
                Intent = autoclass("android.content.Intent")
                Settings = autoclass("android.provider.Settings")
                Uri = autoclass("android.net.Uri")
                pkg_name = app.getPackageName()
                try:
                    intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                    intent.setData(Uri.parse(f"package:{pkg_name}"))
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    app.startActivity(intent)
                    notify("Set 7Stream to Unrestricted in Battery settings")
                    return
                except Exception:
                    intent = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    app.startActivity(intent)
                    notify("Set 7Stream to Unrestricted in Battery settings")
                    return
        except Exception:
            pass
        notify("Phone Settings > Apps > 7Stream > Battery > Set to Unrestricted")

    # Android Direct Storage Permission Request
    def trigger_direct_permission_request(e=None):
        try:
            from jnius import autoclass
            app = None
            try:
                ActivityThread = autoclass("android.app.ActivityThread")
                app = ActivityThread.currentApplication()
            except Exception:
                pass
            if not app:
                for act_class in [
                    "org.kivy.android.PythonActivity",
                    "com.sevenstream.app.MainActivity",
                    "io.flutter.embedding.android.FlutterActivity",
                ]:
                    try:
                        act = autoclass(act_class).mActivity
                        if act:
                            app = act
                            break
                    except Exception:
                        pass
            if app:
                pkg_name = app.getPackageName()
                Intent = autoclass("android.content.Intent")
                Settings = autoclass("android.provider.Settings")
                Uri = autoclass("android.net.Uri")
                Build = autoclass("android.os.Build")

                # Android 11+ (API 30+) All Files Access Direct System Toggle
                if Build.VERSION.SDK_INT >= 30:
                    try:
                        intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                        intent.setData(Uri.parse(f"package:{pkg_name}"))
                        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        app.startActivity(intent)
                        notify("Please turn 'Allow access to manage all files' ON.")
                        return
                    except Exception:
                        pass

                # App Details Settings (Permissions -> Allow)
                try:
                    intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                    intent.setData(Uri.parse(f"package:{pkg_name}"))
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    app.startActivity(intent)
                    notify("Tap 'Permissions' > Allow 'Files and Media'")
                    return
                except Exception:
                    pass
        except Exception:
            pass
        notify("Open Settings > Apps > 7Stream > Permissions > Allow.")

    # ──────────────────────────────────────────────────────────────────────────
    # 1. TOP HEADER & APP BRANDING (Salman Sarod)
    # ──────────────────────────────────────────────────────────────────────────
    logo_path = Path(__file__).resolve().parent / "assets" / "logo.png"
    if logo_path.exists():
        brand_icon = ft.Image(
            src=str(logo_path), width=44, height=44,
            border_radius=12, fit=ft.BoxFit.CONTAIN,
        )
    else:
        brand_icon = ft.Text("🎬", size=32)

    status_pill_dot = ft.Container(width=8, height=8, border_radius=4, bgcolor="#94a3b8")
    status_pill_text = ft.Text("OFFLINE", size=11, weight=ft.FontWeight.BOLD, color="#94a3b8")
    status_pill = ft.Container(
        content=ft.Row([status_pill_dot, status_pill_text], spacing=6, alignment=ft.MainAxisAlignment.CENTER),
        bgcolor="#141c2e",
        padding=ft.Padding(top=6, bottom=6, left=12, right=12),
        border_radius=20,
        border=make_border("#233554"),
    )

    top_header = ft.Container(
        content=ft.Row([
            ft.Row([
                brand_icon,
                ft.Column([
                    ft.Row([
                        ft.Text("7Stream", size=22, weight=ft.FontWeight.BOLD, color="#ffffff"),
                        ft.Container(
                            content=ft.Text("PRO", size=10, weight=ft.FontWeight.BOLD, color="#0c4a6e"),
                            bgcolor="#38bdf8",
                            padding=ft.Padding(top=2, bottom=2, left=6, right=6),
                            border_radius=6,
                        ),
                    ], spacing=6),
                    ft.Text("Developed by Salman Sarod", size=11, weight=ft.FontWeight.W_500, color="#38bdf8"),
                ], spacing=2),
            ], spacing=12),
            status_pill,
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        padding=ft.Padding(top=4, bottom=8, left=0, right=0),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 2. CONNECTION MODE TOGGLE (Hotspot vs Wi-Fi)
    # ──────────────────────────────────────────────────────────────────────────
    mode_hotspot_btn = ft.FilledButton(
        "📡  Offline Hotspot",
        bgcolor="#2563eb",
        color="#ffffff",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
        expand=True,
    )
    mode_wifi_btn = ft.FilledButton(
        "📶  Home Wi-Fi",
        bgcolor="#141c2e",
        color="#94a3b8",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
        expand=True,
    )

    mode_tip_text = ft.Text(
        "Works 100% offline without mobile data or internet router.",
        size=11, color="#64748b", text_align=ft.TextAlign.CENTER,
    )

    def select_mode(m: str):
        nonlocal active_mode, current_detected_ip
        active_mode = m
        current_detected_ip = detect_ip(active_mode)
        if m == "hotspot":
            mode_hotspot_btn.bgcolor = "#2563eb"
            mode_hotspot_btn.color = "#ffffff"
            mode_wifi_btn.bgcolor = "#141c2e"
            mode_wifi_btn.color = "#94a3b8"
            mode_tip_text.value = "Works 100% offline without mobile data. Connect TV/PC to phone's hotspot."
        else:
            mode_wifi_btn.bgcolor = "#2563eb"
            mode_wifi_btn.color = "#ffffff"
            mode_hotspot_btn.bgcolor = "#141c2e"
            mode_hotspot_btn.color = "#94a3b8"
            mode_tip_text.value = "Connect phone and TV/PC to the same Wi-Fi router network."
        update_url_and_qr()
        page.update()

    mode_hotspot_btn.on_click = lambda e: select_mode("hotspot")
    mode_wifi_btn.on_click = lambda e: select_mode("wifi")

    mode_card = ft.Container(
        content=ft.Column([
            ft.Row([mode_hotspot_btn, mode_wifi_btn], spacing=8),
            mode_tip_text,
        ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor="#0d1424",
        padding=12,
        border_radius=14,
        border=make_border("#1a2742"),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 3. STREAMING BROADCAST DECK (URL, QR, VLC)
    # ──────────────────────────────────────────────────────────────────────────
    port_input = ft.TextField(
        label="Port",
        value="8080",
        width=90,
        keyboard_type=ft.KeyboardType.NUMBER,
        text_size=13,
        on_change=lambda e: (_lazy_qr_update()),
    )

    url_display = ft.Text(
        f"http://{current_detected_ip}:8080",
        size=17,
        weight=ft.FontWeight.BOLD,
        color="#38bdf8",
        selectable=True,
    )

    qr_image = ft.Image(
        src=f"data:image/png;base64,{generate_qr_base64(f'http://{current_detected_ip}:8080')}",
        width=150,
        height=150,
        fit=ft.BoxFit.CONTAIN,
    )
    qr_card = ft.Container(
        content=qr_image,
        bgcolor="#ffffff",
        padding=10,
        border_radius=14,
        alignment=ft.Alignment(0, 0),
        width=170,
        height=170,
    )

    def _lazy_qr_update():
        update_url_and_qr()
        page.update()

    def update_url_and_qr():
        port_v = (port_input.value or "8080").strip()
        full_u = f"http://{current_detected_ip}:{port_v}"
        url_display.value = full_u
        try:
            new_b64 = generate_qr_base64(full_u)
            qr_image.src = f"data:image/png;base64,{new_b64}"
        except Exception:
            pass

    def on_refresh_ip(e):
        nonlocal current_detected_ip
        current_detected_ip = detect_ip(active_mode)
        update_url_and_qr()
        page.update()
        notify(f"IP updated: {current_detected_ip}")

    def on_copy_url(e):
        port_v = (port_input.value or "8080").strip()
        copy_to_clipboard(f"http://{current_detected_ip}:{port_v}")

    broadcast_deck = ft.Container(
        content=ft.Column([
            ft.Text("BROADCAST ADDRESS", size=10, weight=ft.FontWeight.BOLD, color="#64748b"),
            ft.Row([
                url_display,
                ft.Row([
                    ft.IconButton(ft.Icons.COPY, tooltip="Copy URL", icon_color="#38bdf8", on_click=on_copy_url),
                    ft.IconButton(ft.Icons.REFRESH, tooltip="Refresh IP", icon_color="#38bdf8", on_click=on_refresh_ip),
                ], spacing=0),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Container(height=4),
            ft.Row([qr_card], alignment=ft.MainAxisAlignment.CENTER),
            ft.Text(
                "Scan with TV or phone camera to watch directly",
                size=11, color="#64748b", text_align=ft.TextAlign.CENTER,
            ),
            ft.Container(height=6),
            ft.Row([
                ft.FilledButton(
                    "🎬  Open in VLC App",
                    bgcolor="#f97316",
                    color="#ffffff",
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                    on_click=lambda e: launch_vlc_app(f"{url_display.value}/playlist.m3u"),
                ),
                ft.FilledButton(
                    "📋  Copy .m3u Link",
                    bgcolor="#1e293b",
                    color="#38bdf8",
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                    on_click=lambda e: copy_to_clipboard(f"{url_display.value}/playlist.m3u"),
                ),
            ], alignment=ft.MainAxisAlignment.CENTER, spacing=8),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        bgcolor="#0d1424",
        padding=16,
        border_radius=16,
        border=make_border("#1e2d4a"),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 4. MEDIA LIBRARY & VIDEO RADAR
    # ──────────────────────────────────────────────────────────────────────────
    dir_input = ft.TextField(
        label="Video Folder Path",
        value=get_default_media_dir(),
        expand=True,
        prefix_icon=ft.Icons.FOLDER,
        text_size=12,
        hint_text="/storage/emulated/0/Movies",
        on_change=lambda e: update_folder_stats(),
    )

    # Radar status indicator
    radar_title = ft.Text("Scanning media...", size=13, weight=ft.FontWeight.BOLD, color="#38bdf8")
    radar_subtitle = ft.Text("", size=11, color="#64748b")
    radar_pill = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.VIDEOCAM, color="#38bdf8", size=20),
            ft.Column([radar_title, radar_subtitle], spacing=1, expand=True),
        ], spacing=10),
        bgcolor="#10192e",
        padding=12,
        border_radius=12,
        border=make_border("#1e355c"),
    )

    def update_folder_stats():
        p_str = (dir_input.value or "").strip()
        if not p_str:
            radar_title.value = "No Folder Selected"
            radar_title.color = "#ef4444"
            radar_subtitle.value = "Tap a preset below or type a directory path"
            page.update()
            return

        p = Path(p_str)
        if not p.exists():
            radar_title.value = "Folder Not Found"
            radar_title.color = "#ef4444"
            radar_subtitle.value = f"Cannot find: {p_str}"
            page.update()
            return

        try:
            direct_vids = 0
            direct_size = 0
            sub_count = 0
            for e in os.scandir(p):
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    sub_count += 1
                elif e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTENSIONS:
                    direct_vids += 1
                    try:
                        direct_size += e.stat().st_size
                    except Exception:
                        pass

            total_vids = count_videos_in_dir(p)
            size_str = format_size(direct_size)

            if direct_vids > 0:
                radar_title.value = f"🎬 {direct_vids} Videos Ready ({size_str})"
                radar_title.color = "#22c55e"
                radar_subtitle.value = f"{total_vids} total videos including {sub_count} subfolders"
            elif total_vids > 0:
                radar_title.value = f"📁 {total_vids} Videos in Subfolders"
                radar_title.color = "#38bdf8"
                radar_subtitle.value = f"Found in {sub_count} subfolders. Use 'All Videos' in browser."
            else:
                radar_title.value = "⚠️ 0 Videos Found in Folder"
                radar_title.color = "#f59e0b"
                radar_subtitle.value = "If you have movies here, check storage permissions below."
        except Exception as ex:
            radar_title.value = "Access / Permission Restricted"
            radar_title.color = "#ef4444"
            radar_subtitle.value = f"Error: {ex}. Enable storage access below."

        page.update()

    # Preset Quick Buttons
    def set_folder_preset(preset_p: str):
        dir_input.value = preset_p
        update_folder_stats()
        notify(f"Selected: {preset_p}")

    is_android = Path("/storage/emulated/0").exists()
    movies_p = "/storage/emulated/0/Movies" if is_android else str(Path.home() / "Videos")
    download_p = "/storage/emulated/0/Download" if is_android else str(Path.home() / "Downloads")
    dcim_p = "/storage/emulated/0/DCIM" if is_android else str(Path.home() / "Pictures")
    storage_p = "/storage/emulated/0" if is_android else str(Path.home())

    preset_row = ft.Row([
        ft.FilledButton("🎬 Movies", bgcolor="#141c2e", color="#cbd5e1", on_click=lambda e: set_folder_preset(movies_p)),
        ft.FilledButton("📥 Download", bgcolor="#141c2e", color="#cbd5e1", on_click=lambda e: set_folder_preset(download_p)),
        ft.FilledButton("📷 DCIM", bgcolor="#141c2e", color="#cbd5e1", on_click=lambda e: set_folder_preset(dcim_p)),
        ft.FilledButton("📱 All Storage", bgcolor="#141c2e", color="#cbd5e1", on_click=lambda e: set_folder_preset(storage_p)),
    ], wrap=True, spacing=6)

    # In-App Pure-Python Directory Explorer
    folder_dialog = None
    browse_state = {"path": dir_input.value or get_default_media_dir()}
    folder_list_view = ft.ListView(expand=True, spacing=4, height=280)
    current_path_text = ft.Text(size=11, color="#38bdf8", weight=ft.FontWeight.BOLD)

    def refresh_folder_list():
        c_path = Path(browse_state["path"]).resolve()
        folder_list_view.controls.clear()

        # Up one level
        if c_path.parent != c_path and str(c_path) not in ("/", "C:" + chr(92)):
            def go_parent(e, p=c_path.parent):
                browse_state["path"] = str(p)
                refresh_folder_list()
                page.update()
            folder_list_view.controls.append(
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.ARROW_UPWARD, color="#38bdf8", size=20),
                    title=ft.Text(".. (Up one level)", color="#38bdf8", size=13),
                    on_click=go_parent,
                )
            )

        try:
            entries = sorted(os.scandir(c_path), key=lambda x: (not x.is_dir(), x.name.lower()))
            found_dirs = 0
            found_vids = 0

            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    found_dirs += 1
                    def on_dir_click(e, d=entry.path):
                        browse_state["path"] = d
                        refresh_folder_list()
                        page.update()
                    folder_list_view.controls.append(
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.FOLDER, color="#facc15", size=22),
                            title=ft.Text(entry.name, size=13, weight=ft.FontWeight.W_500),
                            subtitle=ft.Text("Folder • Tap to open", size=10, color="#64748b"),
                            on_click=on_dir_click,
                        )
                    )
                elif entry.is_file():
                    ext = Path(entry.name).suffix.lower()
                    if ext in VIDEO_EXTENSIONS:
                        found_vids += 1
                        try:
                            f_size = format_size(entry.stat().st_size)
                        except Exception:
                            f_size = ""
                        def on_video_click(e, parent_d=str(c_path)):
                            dir_input.value = parent_d
                            if hasattr(page, "pop_dialog"):
                                try: page.pop_dialog()
                                except Exception: pass
                            if folder_dialog: folder_dialog.open = False
                            update_folder_stats()
                            notify(f"Selected folder: {parent_d}")

                        folder_list_view.controls.append(
                            ft.ListTile(
                                leading=ft.Icon(ft.Icons.VIDEO_FILE, color="#38bdf8", size=22),
                                title=ft.Text(entry.name, size=13, color="#f1f5f9"),
                                subtitle=ft.Text(f"Video • {f_size} • Tap to select folder", size=10, color="#38bdf8"),
                                on_click=on_video_click,
                            )
                        )

            current_path_text.value = f"{c_path} | 📁 {found_dirs} folders • 🎬 {found_vids} videos"
            if found_dirs == 0 and found_vids == 0:
                folder_list_view.controls.append(
                    ft.Text("No video files or folders found here. Tap 'Up one level' or choose another directory.", size=12, color="#94a3b8")
                )
        except Exception as ex:
            folder_list_view.controls.append(
                ft.Text(f"Cannot access folder: {ex}", color="#ef4444", size=12)
            )

    def on_select_current_folder(e):
        dir_input.value = browse_state["path"]
        if hasattr(page, "pop_dialog"):
            try: page.pop_dialog()
            except Exception: pass
        if folder_dialog:
            folder_dialog.open = False
        update_folder_stats()
        notify(f"Selected: {dir_input.value}")

    def on_cancel_dialog(e):
        if hasattr(page, "pop_dialog"):
            try: page.pop_dialog()
            except Exception: pass
        if folder_dialog:
            folder_dialog.open = False
        page.update()

    def open_browse_dialog(e):
        nonlocal folder_dialog
        init_p = dir_input.value.strip() if dir_input.value else get_default_media_dir()
        if not Path(init_p).exists():
            init_p = get_default_media_dir()
        browse_state["path"] = init_p
        refresh_folder_list()

        folder_dialog = ft.AlertDialog(
            title=ft.Text("Select Video Folder", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    current_path_text,
                    ft.Divider(color="#233554", height=1),
                    folder_list_view,
                ], spacing=6),
                width=340,
                height=320,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=on_cancel_dialog),
                ft.FilledButton("Select This Folder", on_click=on_select_current_folder),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        if hasattr(page, "show_dialog"):
            page.show_dialog(folder_dialog)
        else:
            page.dialog = folder_dialog
            folder_dialog.open = True
            page.update()

    browse_btn = ft.IconButton(
        icon=ft.Icons.FOLDER_OPEN,
        tooltip="Browse Folders",
        icon_color="#38bdf8",
        on_click=open_browse_dialog,
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 5. STORAGE & BACKGROUND STREAMING HELP CARD
    # ──────────────────────────────────────────────────────────────────────────
    perm_guide_card = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.SECURITY, color="#f59e0b", size=18),
                ft.Text("Storage & Background Permission Guide", weight=ft.FontWeight.BOLD, size=12, color="#f59e0b"),
            ], spacing=6),
            ft.Text("• Allow 7Stream to find all video files on your phone.", size=11, color="#cbd5e1"),
            ft.Text("• Set battery to Unrestricted so streaming never sleeps.", size=11, color="#94a3b8"),
            ft.Row([
                ft.FilledButton(
                    "🔑 Grant Storage Access",
                    bgcolor="#2563eb",
                    color="#ffffff",
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                    on_click=trigger_direct_permission_request,
                ),
                ft.FilledButton(
                    "⚡ Background Streaming",
                    bgcolor="#1e293b",
                    color="#38bdf8",
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                    on_click=open_battery_settings,
                ),
                ft.FilledButton(
                    "🔄 Rescan",
                    bgcolor="#1e293b",
                    color="#ffffff",
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                    on_click=lambda e: (update_folder_stats(), notify("Rescanned storage.")),
                ),
            ], wrap=True, spacing=6, alignment=ft.MainAxisAlignment.END),
        ], spacing=6),
        bgcolor="#14170d",
        padding=12,
        border_radius=12,
        border=make_border("#423512"),
    )

    media_hub_card = ft.Container(
        content=ft.Column([
            ft.Text("MEDIA LIBRARY & STORAGE", size=10, weight=ft.FontWeight.BOLD, color="#64748b"),
            radar_pill,
            ft.Row([dir_input, browse_btn], spacing=4),
            preset_row,
            ft.Row([
                port_input,
                ft.Text("Port (default 8080).", size=11, color="#64748b", expand=True),
            ], spacing=8, alignment=ft.MainAxisAlignment.START),
            perm_guide_card,
        ], spacing=10),
        bgcolor="#0d1424",
        padding=16,
        border_radius=16,
        border=make_border("#1e2d4a"),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 6. MAIN POWER / TOGGLE BUTTON
    # ──────────────────────────────────────────────────────────────────────────
    start_stop_btn = ft.FilledButton(
        "▶   START STREAMING SERVER",
        bgcolor="#2563eb",
        color="#ffffff",
        height=52,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=14)),
    )

    def toggle_server(e):
        if not server.is_running:
            try:
                port_num = int((port_input.value or "8080").strip())
            except ValueError:
                notify("Port must be a valid number (e.g. 8080)")
                return

            media_path = (dir_input.value or "").strip()
            if not media_path:
                notify("Please select a video folder path first")
                return

            server.set_config(media_path, port_num)
            success, msg = server.start()
            if success:
                status_pill_dot.bgcolor = "#00e676"
                status_pill_text.value = "LIVE"
                status_pill_text.color = "#00e676"
                status_pill.bgcolor = "#052e16"
                status_pill.border = make_border("#14532d")

                start_stop_btn.text = "⏹   STOP SERVER"
                start_stop_btn.bgcolor = "#dc2626"

                dir_input.disabled = True
                port_input.disabled = True
                browse_btn.disabled = True
                notify("🟢 7Stream Server is LIVE! Stream to your TV or laptop now.")
            else:
                notify(f"Failed to start: {msg}")
        else:
            server.stop()
            status_pill_dot.bgcolor = "#94a3b8"
            status_pill_text.value = "OFFLINE"
            status_pill_text.color = "#94a3b8"
            status_pill.bgcolor = "#141c2e"
            status_pill.border = make_border("#233554")

            start_stop_btn.text = "▶   START STREAMING SERVER"
            start_stop_btn.bgcolor = "#2563eb"

            dir_input.disabled = False
            port_input.disabled = False
            browse_btn.disabled = False
            notify("Server stopped.")

        page.update()

    start_stop_btn.on_click = toggle_server

    # Trigger initial scan
    update_folder_stats()

    # ──────────────────────────────────────────────────────────────────────────
    # 7. FIRST-TIME INSTALL PERMISSION POPUP DIALOG
    # ──────────────────────────────────────────────────────────────────────────
    first_launch_perm_dialog = None

    def close_first_perm_dialog(e=None):
        nonlocal first_launch_perm_dialog
        if hasattr(page, "pop_dialog"):
            try: page.pop_dialog()
            except Exception: pass
        if first_launch_perm_dialog:
            first_launch_perm_dialog.open = False
        update_folder_stats()
        page.update()

    def on_grant_permission_clicked(e):
        close_first_perm_dialog()
        trigger_direct_permission_request()

    first_launch_perm_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Row([
            ft.Icon(ft.Icons.SECURITY_ROUNDED, color="#38bdf8", size=26),
            ft.Text("Storage Permission", size=16, weight=ft.FontWeight.BOLD, color="#ffffff"),
        ], spacing=8),
        content=ft.Container(
            content=ft.Column([
                ft.Text("Welcome to 7Stream by Salman Sarod!", size=13, weight=ft.FontWeight.BOLD, color="#38bdf8"),
                ft.Text(
                    "To stream videos to your TV or PC, 7Stream needs access to your device storage.",
                    size=12, color="#cbd5e1",
                ),
                ft.Container(height=6),
                ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.CHECK_CIRCLE, color="#22c55e", size=16),
                        ft.Text("Tap below to allow storage access directly", size=11, color="#22c55e", weight=ft.FontWeight.W_500),
                    ], spacing=6),
                    bgcolor="#052e16",
                    padding=8,
                    border_radius=8,
                    border=make_border("#14532d"),
                ),
            ], spacing=6, tight=True),
            width=310,
        ),
        actions=[
            ft.TextButton("Later", on_click=close_first_perm_dialog),
            ft.FilledButton(
                "Allow Storage Access",
                bgcolor="#2563eb",
                color="#ffffff",
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                on_click=on_grant_permission_clicked,
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 8. ASSEMBLE PAGE
    # ──────────────────────────────────────────────────────────────────────────
    page.add(
        ft.Column([
            top_header,
            mode_card,
            broadcast_deck,
            media_hub_card,
            ft.Container(
                content=start_stop_btn,
                alignment=ft.Alignment(0, 0),
                padding=ft.Padding(top=6, bottom=24, left=0, right=0),
            ),
        ], spacing=14, scroll=ft.ScrollMode.AUTO)
    )

    # Check if this is an Android device with ungranted / restricted storage
    if is_android:
        needs_permission = False
        try:
            cand_p = Path("/storage/emulated/0/Movies")
            if not cand_p.exists() or len(list(os.scandir(cand_p))) == 0:
                # Also check Downloads
                dl_p = Path("/storage/emulated/0/Download")
                if not dl_p.exists() or len(list(os.scandir(dl_p))) == 0:
                    needs_permission = True
        except Exception:
            needs_permission = True

        if needs_permission:
            if hasattr(page, "show_dialog"):
                page.show_dialog(first_launch_perm_dialog)
            else:
                page.dialog = first_launch_perm_dialog
                first_launch_perm_dialog.open = True
                page.update()


def main(page: ft.Page):
    build_app(page)


if __name__ == "__main__":
    if hasattr(ft, "run"):
        ft.run(main)
    elif hasattr(ft, "app"):
        ft.app(target=main)
    else:
        raise RuntimeError("No recognized Flet application runner found.")

