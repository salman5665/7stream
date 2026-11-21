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
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".ts",
    ".flv",
    ".m4v",
    ".3gp",
    ".wmv",
    ".m2ts",
    ".vob",
    ".ogv",
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
    ".m3u": "audio/x-mpegurl",
    ".m3u8": "application/vnd.apple.mpegurl",
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


def render_html_page(
    base_dir: Path, current_path: Path, rel_path: str, host_header: str
) -> str:
    """
    Renders a responsive, modern dark-mode directory browser
    with video preview modal, search filtering, and VLC playlist shortcuts.
    Self-contained with zero external CDNs so it operates offline.
    """
    items = []
    try:
        entries = sorted(
            os.scandir(current_path),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
    except Exception as err:
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

    # Build breadcrumb links
    parts = [p for p in rel_path.strip("/").split("/") if p]
    breadcrumbs_html = '<a href="/" class="crumb-link">🏠 Root</a>'
    built_path = ""
    for idx, part in enumerate(parts):
        built_path += "/" + quote(part)
        safe_part = html.escape(part)
        if idx == len(parts) - 1:
            breadcrumbs_html += f' <span class="sep">/</span> <span class="crumb-current">{safe_part}</span>'
        else:
            breadcrumbs_html += f' <span class="sep">/</span> <a href="/browse{built_path}" class="crumb-link">{safe_part}</a>'

    # Parent directory link
    parent_link_html = ""
    if parts:
        parent_rel = "/".join(parts[:-1])
        if parent_rel:
            parent_url = f"/browse/{quote(parent_rel)}"
        else:
            parent_url = "/"
        parent_link_html = f"""
        <a href="{parent_url}" class="card card-parent">
            <div class="card-icon">📁⬆️</div>
            <div class="card-info">
                <div class="card-title">.. (Parent Directory)</div>
                <div class="card-meta">Go up one level</div>
            </div>
        </a>
        """

    # Subdirectories
    folders_html = []
    for d in subdirs:
        sub_rel = (
            f"{rel_path.strip('/')}/{d.name}".strip("/") if rel_path else d.name
        )
        folder_url = f"/browse/{quote(sub_rel)}"
        safe_name = html.escape(d.name)
        folders_html.append(f"""
        <a href="{folder_url}" class="card card-folder" data-name="{safe_name.lower()}">
            <div class="card-icon">📁</div>
            <div class="card-info">
                <div class="card-title">{safe_name}</div>
                <div class="card-meta">Folder</div>
            </div>
            <div class="card-arrow">➔</div>
        </a>
        """)

    # Video files
    videos_html = []
    for f in video_files:
        try:
            stat = f.stat()
            size_str = format_size(stat.st_size)
            mtime_str = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)
            )
        except Exception:
            size_str = "Unknown"
            mtime_str = ""

        f_rel = (
            f"{rel_path.strip('/')}/{f.name}".strip("/") if rel_path else f.name
        )
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
                <button class="btn btn-play" onclick="openPlayer('{stream_url}', '{safe_name}')">
                    ▶ Play
                </button>
                <a href="{stream_url}" download="{safe_name}" class="btn btn-download" title="Download Video">
                    ⬇
                </a>
                <button class="btn btn-copy" onclick="copyText('{full_stream_url}')" title="Copy VLC Stream URL">
                    🔗 Copy
                </button>
            </div>
        </div>
        """)

    folder_content = "\n".join(folders_html) if folders_html else ""
    video_content = (
        "\n".join(videos_html)
        if videos_html
        else '<div class="empty-state">No video files found in this directory.</div>'
    )

    playlist_param = f"?path={quote(rel_path)}" if rel_path else ""
    vlc_playlist_url = f"/playlist.m3u{playlist_param}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>7Stream Media Server</title>
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
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
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
            padding: 16px 20px;
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
            font-size: 28px;
            background: rgba(56, 189, 248, 0.15);
            padding: 8px;
            border-radius: 12px;
            border: 1px solid rgba(56, 189, 248, 0.3);
        }}
        .logo-title {{
            font-size: 1.25rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: #ffffff;
        }}
        .logo-subtitle {{
            font-size: 0.8rem;
            color: var(--primary);
            font-weight: 500;
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
            font-size: 0.88rem;
            transition: all 0.2s ease;
        }}
        .btn-header:hover {{
            background: #34d399;
            transform: translateY(-1px);
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
            margin-bottom: 20px;
            font-size: 0.92rem;
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
        .crumb-link:hover {{
            text-decoration: underline;
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
            transition: border 0.2s;
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
            font-size: 26px;
            flex-shrink: 0;
        }}
        .card-info {{
            flex: 1;
            min-width: 0;
        }}
        .card-title {{
            font-size: 1rem;
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
        .card-arrow {{
            color: var(--text-muted);
            font-size: 1.1rem;
        }}
        .card-actions {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-shrink: 0;
        }}
        .btn {{
            padding: 7px 12px;
            border-radius: 8px;
            font-size: 0.84rem;
            font-weight: 600;
            cursor: pointer;
            border: none;
            text-decoration: none;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .btn-play {{
            background: var(--primary);
            color: #0c4a6e;
        }}
        .btn-play:hover {{
            background: var(--primary-hover);
        }}
        .btn-download {{
            background: var(--surface-hover);
            color: var(--text);
            border: 1px solid var(--border);
        }}
        .btn-download:hover {{
            background: #28374f;
        }}
        .btn-copy {{
            background: rgba(168, 85, 247, 0.15);
            color: #d8b4fe;
            border: 1px solid rgba(168, 85, 247, 0.3);
        }}
        .btn-copy:hover {{
            background: rgba(168, 85, 247, 0.3);
        }}
        .empty-state {{
            text-align: center;
            padding: 40px 20px;
            color: var(--text-muted);
            background: var(--surface);
            border: 1px dashed var(--border);
            border-radius: 12px;
            font-size: 0.95rem;
        }}
        footer {{
            border-top: 1px solid var(--border);
            padding: 16px 20px;
            text-align: center;
            font-size: 0.8rem;
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
            backdrop-filter: blur(8px);
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
            padding-right: 12px;
        }}
        .modal-close {{
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 24px;
            cursor: pointer;
            padding: 4px 8px;
            line-height: 1;
            border-radius: 6px;
        }}
        .modal-close:hover {{
            color: #ffffff;
            background: rgba(255, 255, 255, 0.1);
        }}
        .video-wrapper {{
            background: #000000;
            position: relative;
            padding-top: 56.25%; /* 16:9 Aspect Ratio */
        }}
        .video-wrapper video {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            outline: none;
        }}
        .modal-footer {{
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.84rem;
            color: var(--text-muted);
            background: #111827;
        }}
        /* Toast notification */
        .toast {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--accent-green);
            color: #064e3b;
            font-weight: 600;
            padding: 10px 18px;
            border-radius: 8px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.4);
            transform: translateY(100px);
            opacity: 0;
            transition: all 0.3s ease;
            z-index: 200;
            pointer-events: none;
        }}
        .toast.show {{
            transform: translateY(0);
            opacity: 1;
        }}
        @media (max-width: 600px) {{
            .card {{
                flex-direction: column;
                align-items: flex-start;
                gap: 10px;
            }}
            .card-actions {{
                width: 100%;
                justify-content: flex-end;
            }}
            .logo-subtitle {{
                display: none;
            }}
        }}
    </style>
</head>
<body>
    <header>
        <div class="header-wrap">
            <div class="logo-group">
                <div class="logo-icon">🎬</div>
                <div>
                    <div class="logo-title">7Stream Media Server</div>
                    <div class="logo-subtitle">Hardware-Accelerated Video Streaming</div>
                </div>
            </div>
            <div class="header-actions">
                <a href="{vlc_playlist_url}" class="btn-header" title="Download recursive .m3u playlist for VLC">
                    📋 Open VLC Playlist (.m3u)
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
                {len(video_files)} Video(s), {len(subdirs)} Folder(s)
            </div>
        </div>

        <div class="grid" id="itemsList">
            {parent_link_html}
            {folder_content}
            {video_content}
        </div>
    </main>

    <footer>
        7Stream Server &bull; Native HTTP Byte-Range Seeking Enabled &bull; Powered by aiohttp & Flet
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
            <div class="modal-footer">
                <span>Seeking supported via HTTP 206 Partial Content</span>
                <button class="btn btn-copy" id="copyModalBtn" onclick="copyCurrentVideoUrl()">🔗 Copy Stream Link</button>
            </div>
        </div>
    </div>

    <!-- Notification Toast -->
    <div id="toast" class="toast">Link copied to clipboard!</div>

    <script>
        let currentVideoUrl = "";

        function openPlayer(url, title) {{
            currentVideoUrl = window.location.origin + url;
            document.getElementById("playerTitle").innerText = title;
            const video = document.getElementById("htmlPlayer");
            video.src = url;
            video.load();
            document.getElementById("videoModal").style.display = "flex";
            video.play().catch(e => console.log("Autoplay prevented:", e));
        }}

        function closePlayer() {{
            const modal = document.getElementById("videoModal");
            const video = document.getElementById("htmlPlayer");
            video.pause();
            video.removeAttribute("src");
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

        function copyText(text) {{
            navigator.clipboard.writeText(text).then(() => {{
                showToast("Stream URL copied to clipboard!");
            }}).catch(() => {{
                // Fallback for non-secure contexts
                const el = document.createElement('textarea');
                el.value = text;
                document.body.appendChild(el);
                el.select();
                document.execCommand('copy');
                document.body.removeChild(el);
                showToast("Stream URL copied to clipboard!");
            }});
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
            let visibleCount = 0;
            cards.forEach(card => {{
                const name = card.getAttribute("data-name") || "";
                if (name.includes(query)) {{
                    card.style.display = "flex";
                    visibleCount++;
                }} else {{
                    card.style.display = "none";
                }}
            }});
        }}
    </script>
</body>
</html>
"""


# ============================================================================
# 4. Asynchronous Video Streaming Server Manager
# ============================================================================
class VideoServerManager:
    """
    Manages the background aiohttp web server inside a dedicated daemon thread.
    Natively supports HTTP 206 Partial Content byte ranges via web.FileResponse.
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
        html_content = render_html_page(
            self.media_root, target, rel_path, host_header
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
                            url_rel = str(rel).replace("\\", "/")
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
            app.router.add_get("/browse", self.handle_index)
            app.router.add_get(r"/browse/{tail:.*}", self.handle_browse)
            app.router.add_get(r"/video/{tail:.*}", self.handle_video)
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
        self.is_running = False


# ============================================================================
# 5. Modern Flet Mobile GUI Application
# ============================================================================
def build_app(page: ft.Page):
    page.title = "7Stream - Video Server"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 16
    page.scroll = ft.ScrollMode.AUTO

    server = VideoServerManager()
    active_mode = "hotspot"  # 'hotspot' or 'wifi'
    current_detected_ip = detect_ip(active_mode)

    # Cross-version Button helper
    ButtonWidget = getattr(ft, "FilledButton", getattr(ft, "ElevatedButton", ft.Button))

    # Cross-version Styling Helpers
    def make_border(color, width: float = 1):
        if hasattr(ft, "Border") and hasattr(ft.Border, "all"):
            return ft.Border.all(width, color)
        if hasattr(ft, "border") and hasattr(ft.border, "all"):
            return ft.border.all(width, color)
        return None

    def make_padding(top: float = 0, bottom: float = 0, left: float = 0, right: float = 0):
        if hasattr(ft, "Padding") and hasattr(ft.Padding, "only"):
            return ft.Padding.only(top=top, bottom=bottom, left=left, right=right)
        if hasattr(ft, "padding") and hasattr(ft.padding, "only"):
            return ft.padding.only(top=top, bottom=bottom, left=left, right=right)
        return None

    def get_alignment_center():
        if hasattr(ft, "Alignment") and hasattr(ft.Alignment, "CENTER"):
            return ft.Alignment.CENTER
        if hasattr(ft, "alignment") and hasattr(ft.alignment, "center"):
            return ft.alignment.center
        return ft.Alignment(0, 0)

    # Cross-version Clipboard Service setup
    clipboard_service = None
    if hasattr(ft, "Clipboard") and hasattr(page, "services"):
        clipboard_service = ft.Clipboard()
        try:
            page.services.append(clipboard_service)
        except Exception:
            pass

    def notify(msg: str):
        """Displays a snackbar toast message safely across Flet versions."""
        sb = ft.SnackBar(content=ft.Text(msg))
        if hasattr(page, "show_dialog"):
            try:
                page.show_dialog(sb)
                return
            except Exception:
                pass
        if hasattr(page, "snack_bar"):
            try:
                page.snack_bar = sb
                sb.open = True
                page.update()
                return
            except Exception:
                pass
        if hasattr(page, "overlay"):
            try:
                page.overlay.append(sb)
                sb.open = True
                page.update()
                return
            except Exception:
                pass

    def copy_to_clipboard(text: str):
        """Copies text to clipboard across all Flet versions and desktop platforms."""
        copied = False
        if hasattr(page, "set_clipboard"):
            try:
                page.set_clipboard(text)
                copied = True
            except Exception:
                pass

        if not copied and clipboard_service is not None:
            try:
                if inspect.iscoroutinefunction(clipboard_service.set):
                    if hasattr(page, "run_task"):
                        page.run_task(clipboard_service.set, text)
                        copied = True
                    else:
                        asyncio.create_task(clipboard_service.set(text))
                        copied = True
                else:
                    clipboard_service.set(text)
                    copied = True
            except Exception:
                pass

        notify(f"Copied: {text}")

    # Top Status Badge
    status_icon = ft.Icon(ft.Icons.STOP_CIRCLE_OUTLINED, color=ft.Colors.RED_400, size=20)
    status_text = ft.Text(
        "🔴 Server Stopped",
        color=ft.Colors.RED_400,
        weight=ft.FontWeight.BOLD,
        size=15,
    )
    status_card = ft.Container(
        content=ft.Row([status_icon, status_text], alignment=ft.MainAxisAlignment.CENTER),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        padding=12,
        border_radius=12,
        border=make_border(ft.Colors.RED_900),
    )

    # Mode Selector
    def on_mode_change(e):
        nonlocal active_mode, current_detected_ip
        sel = getattr(mode_selector, "selected", None)
        if sel:
            active_mode = list(sel)[0] if isinstance(sel, (list, set, tuple)) else str(sel)
        current_detected_ip = detect_ip(active_mode)
        update_guide_card()
        update_url_and_qr()
        page.update()

    mode_selector = ft.SegmentedButton(
        segments=[
            ft.Segment(
                value="hotspot",
                label=ft.Text("Mobile Hotspot (Offline)"),
                icon=ft.Icon(ft.Icons.WIFI_TETHERING),
            ),
            ft.Segment(
                value="wifi",
                label=ft.Text("Wi-Fi Router"),
                icon=ft.Icon(ft.Icons.WIFI),
            ),
        ],
        selected=["hotspot"],
        on_change=on_mode_change,
    )

    # Step-by-Step Guide Card
    guide_title = ft.Text("", weight=ft.FontWeight.BOLD, size=14)
    guide_steps = ft.Column(spacing=6)
    hotspot_settings_btn = ButtonWidget(
        "Open Hotspot Settings",
        icon=ft.Icons.SETTINGS,
        on_click=lambda e: open_hotspot_settings(),
    )

    def open_hotspot_settings():
        """Launches Android hotspot settings if on Android or displays a helpful prompt."""
        opened = False
        try:
            subprocess.run(
                ["am", "start", "-a", "android.settings.TETHER_SETTINGS"],
                check=False,
                timeout=1.5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            opened = True
        except Exception:
            pass

        if not opened:
            try:
                from jnius import autoclass
                Intent = autoclass("android.content.Intent")
                Settings = autoclass("android.provider.Settings")
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                intent = Intent(Settings.ACTION_WIRELESS_SETTINGS)
                activity.startActivity(intent)
                opened = True
            except Exception:
                pass

        notify("Open Settings > Network > Hotspot & tethering on your phone.")

    guide_container = ft.Container(
        content=ft.Column([
            ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE, size=18, color=ft.Colors.PRIMARY), guide_title]),
            guide_steps,
            hotspot_settings_btn,
        ], spacing=10),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
        padding=14,
        border_radius=12,
        border=make_border(ft.Colors.OUTLINE_VARIANT),
    )

    def update_guide_card():
        guide_steps.controls.clear()
        if active_mode == "hotspot":
            guide_title.value = "Direct Mobile Hotspot Guide (No Router / No Internet Needed)"
            guide_steps.controls.extend([
                ft.Text("1. Turn ON your phone's Personal Hotspot (Mobile Data can stay OFF).", size=13),
                ft.Text("2. Connect your Laptop, Smart TV, or Tablet to this phone's Hotspot.", size=13),
                ft.Text("3. Start server below and open URL or scan QR code on the client device.", size=13),
            ])
            hotspot_settings_btn.visible = True
        else:
            guide_title.value = "Wi-Fi Router Guide"
            guide_steps.controls.extend([
                ft.Text("1. Ensure your phone and TV/PC are connected to the SAME Wi-Fi router.", size=13),
                ft.Text("2. Start server below and open URL or scan QR code on the client device.", size=13),
                ft.Text("3. Works across local LAN without consuming mobile data.", size=13),
            ])
            hotspot_settings_btn.visible = False

    update_guide_card()

    # URL Display & QR Code Card
    url_text = ft.Text(
        f"http://{current_detected_ip}:8080",
        size=17,
        weight=ft.FontWeight.BOLD,
        color=ft.Colors.PRIMARY,
    )

    qr_image = ft.Image(
        src=f"data:image/png;base64,{generate_qr_base64(f'http://{current_detected_ip}:8080')}",
        width=180,
        height=180,
        fit=ft.BoxFit.CONTAIN,
    )
    qr_box = ft.Container(
        content=qr_image,
        bgcolor=ft.Colors.WHITE,
        padding=10,
        border_radius=12,
        alignment=get_alignment_center(),
        width=200,
        height=200,
    )

    def on_refresh_ip(e):
        nonlocal current_detected_ip
        current_detected_ip = detect_ip(active_mode)
        update_url_and_qr()
        page.update()
        notify(f"Refreshed IP: {current_detected_ip}")

    def on_copy_url(e):
        port_val = port_input.value.strip() or "8080"
        full_url = f"http://{current_detected_ip}:{port_val}"
        copy_to_clipboard(full_url)

    refresh_btn = ft.IconButton(
        icon=ft.Icons.REFRESH,
        tooltip="Refresh Local IP",
        on_click=on_refresh_ip,
    )
    copy_btn = ft.IconButton(
        icon=ft.Icons.COPY,
        tooltip="Copy URL to Clipboard",
        on_click=on_copy_url,
    )

    def update_url_and_qr():
        port_val = port_input.value.strip() or "8080"
        full_url = f"http://{current_detected_ip}:{port_val}"
        url_text.value = full_url
        new_b64 = generate_qr_base64(full_url)
        qr_image.src = f"data:image/png;base64,{new_b64}"

    url_card = ft.Container(
        content=ft.Column([
            ft.Text("Server Streaming Address", size=13, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([url_text, copy_btn, refresh_btn], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Container(height=6),
            ft.Row([qr_box], alignment=ft.MainAxisAlignment.CENTER),
            ft.Text(
                "Scan with phone camera or QR scanner on TV/Laptop to connect",
                size=12,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.CENTER,
            ),
            ft.Row([
                ButtonWidget(
                    "Copy VLC Playlist Link (.m3u)",
                    icon=ft.Icons.PLAYLIST_PLAY,
                    on_click=lambda e: copy_to_clipboard(f"{url_text.value}/playlist.m3u"),
                )
            ], alignment=ft.MainAxisAlignment.CENTER),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
        padding=16,
        border_radius=12,
        border=make_border(ft.Colors.OUTLINE_VARIANT),
    )

    # Configuration Fields: Directory & Port
    dir_input = ft.TextField(
        label="Video Directory Path",
        value=get_default_media_dir(),
        expand=True,
        prefix_icon=ft.Icons.FOLDER_OPEN,
    )

    def on_dir_picker_result(e):
        path = getattr(e, "path", None)
        if path:
            dir_input.value = path
            page.update()

    file_picker = ft.FilePicker(on_result=on_dir_picker_result)
    page.overlay.append(file_picker)

    async def on_browse_dir(e):
        try:
            if inspect.iscoroutinefunction(file_picker.get_directory_path):
                picked = await file_picker.get_directory_path(
                    dialog_title="Select Video Folder",
                    initial_directory=dir_input.value or None,
                )
                if picked:
                    dir_input.value = picked
                    page.update()
            else:
                file_picker.get_directory_path(
                    dialog_title="Select Video Folder",
                    initial_directory=dir_input.value or None,
                )
        except Exception as ex:
            notify(f"Picker error: {ex}")

    browse_btn = ft.IconButton(
        icon=ft.Icons.FOLDER,
        tooltip="Select Folder",
        on_click=on_browse_dir,
    )

    port_input = ft.TextField(
        label="Server Port",
        value="8080",
        width=120,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: (update_url_and_qr(), page.update()),
    )

    config_card = ft.Container(
        content=ft.Column([
            ft.Text("Storage & Network Settings", weight=ft.FontWeight.BOLD, size=14),
            ft.Row([dir_input, browse_btn], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Row([
                port_input,
                ft.Text("Default is 8080. Accessible across all local devices.", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ], alignment=ft.MainAxisAlignment.START),
        ], spacing=10),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
        padding=14,
        border_radius=12,
        border=make_border(ft.Colors.OUTLINE_VARIANT),
    )

    # Main Start / Stop Button
    start_stop_btn = ButtonWidget(
        "Start Server",
        icon=ft.Icons.PLAY_ARROW,
        style=ft.ButtonStyle(
            bgcolor=ft.Colors.PRIMARY,
            color=ft.Colors.ON_PRIMARY,
            padding=16,
            shape=ft.RoundedRectangleBorder(radius=10),
        ),
    )

    def toggle_server(e):
        if not server.is_running:
            # Start server
            try:
                port_num = int(port_input.value.strip())
            except ValueError:
                notify("Error: Port must be a valid number (e.g. 8080)")
                return

            media_path = dir_input.value.strip()
            if not media_path:
                notify("Error: Directory path cannot be empty")
                return

            server.set_config(media_path, port_num)
            success, msg = server.start()
            if success:
                status_icon.name = ft.Icons.CHECK_CIRCLE
                status_icon.color = ft.Colors.GREEN_400
                status_text.value = f"🟢 Server Running at {url_text.value}"
                status_text.color = ft.Colors.GREEN_400
                status_card.border = make_border(ft.Colors.GREEN_800)

                start_stop_btn.text = "Stop Server"
                start_stop_btn.icon = ft.Icons.STOP
                start_stop_btn.style.bgcolor = ft.Colors.RED_700
                start_stop_btn.style.color = ft.Colors.WHITE

                dir_input.disabled = True
                port_input.disabled = True
                browse_btn.disabled = True
                notify("Video Server is LIVE! Connect client devices now.")
            else:
                notify(f"Startup Failed: {msg}")
        else:
            # Stop server
            server.stop()
            status_icon.name = ft.Icons.STOP_CIRCLE_OUTLINED
            status_icon.color = ft.Colors.RED_400
            status_text.value = "🔴 Server Stopped"
            status_text.color = ft.Colors.RED_400
            status_card.border = make_border(ft.Colors.RED_900)

            start_stop_btn.text = "Start Server"
            start_stop_btn.icon = ft.Icons.PLAY_ARROW
            start_stop_btn.style.bgcolor = ft.Colors.PRIMARY
            start_stop_btn.style.color = ft.Colors.ON_PRIMARY

            dir_input.disabled = False
            port_input.disabled = False
            browse_btn.disabled = False
            notify("Server stopped.")

        page.update()

    start_stop_btn.on_click = toggle_server

    # Clean header banner with 7Stream logo
    logo_img_path = Path(__file__).resolve().parent / "assets" / "logo.png"
    if logo_img_path.exists():
        header_icon = ft.Image(
            src=str(logo_img_path),
            width=42,
            height=42,
            border_radius=10,
            fit=ft.BoxFit.CONTAIN,
        )
    else:
        header_icon = ft.Icon(ft.Icons.ONDEMAND_VIDEO, size=32, color=ft.Colors.PRIMARY)

    header_banner = ft.Row([
        header_icon,
        ft.Column([
            ft.Text("7Stream Server", size=20, weight=ft.FontWeight.BOLD),
            ft.Text("Offline Hotspot & Wi-Fi Media Streaming", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=2),
    ], spacing=12)

    # Build Page Layout
    page.add(
        ft.Column([
            header_banner,
            status_card,
            mode_selector,
            guide_container,
            url_card,
            config_card,
            ft.Container(
                content=start_stop_btn,
                alignment=get_alignment_center(),
                padding=make_padding(top=10, bottom=20),
            ),
        ], spacing=16)
    )
    page.update()


# ============================================================================
# 6. Application Entry Point (Cross-Version Compatible)
# ============================================================================
def main(page: ft.Page):
    build_app(page)


if __name__ == "__main__":
    if hasattr(ft, "run"):
        ft.run(main)
    elif hasattr(ft, "app"):
        ft.app(target=main)
    else:
        raise RuntimeError("No recognized Flet application runner found.")
