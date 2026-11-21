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
        .modal-footer {{
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.84rem;
            color: var(--text-muted);
            background: #111827;
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
    page.title = "7Stream"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#0d1117"
    page.padding = 14
    page.scroll = ft.ScrollMode.AUTO

    server = VideoServerManager()
    active_mode = "hotspot"  # 'hotspot' or 'wifi'
    current_detected_ip = detect_ip(active_mode)

    # Safe cross-version border helper
    def make_border(color, width: float = 1.0):
        try:
            if hasattr(ft, "Border"):
                # Flet 1.x style
                return ft.Border(
                    top=ft.BorderSide(width, color),
                    right=ft.BorderSide(width, color),
                    bottom=ft.BorderSide(width, color),
                    left=ft.BorderSide(width, color),
                )
        except Exception:
            pass
        return None

    # Safe cross-version padding helper
    def make_padding(top=0, bottom=0, left=0, right=0):
        try:
            return ft.Padding(top=top, bottom=bottom, left=left, right=right)
        except Exception:
            return None

    # Safe alignment center
    def get_alignment_center():
        try:
            return ft.alignment.center
        except Exception:
            pass
        try:
            return ft.Alignment(0, 0)
        except Exception:
            return None

    # Snackbar notification
    def notify(msg: str):
        try:
            sb = ft.SnackBar(content=ft.Text(msg, color="#ffffff"), bgcolor="#1e293b")
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

    # Clipboard copy
    def copy_to_clipboard(text: str):
        try:
            if hasattr(page, "set_clipboard"):
                page.set_clipboard(text)
        except Exception:
            pass
        notify(f"Copied: {text}")

    # â”€â”€ Status Badge â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    status_dot = ft.Container(width=12, height=12, border_radius=6, bgcolor="#ef4444")
    status_text = ft.Text("Server Stopped", color="#ef4444", weight=ft.FontWeight.BOLD, size=15)
    status_card = ft.Container(
        content=ft.Row(
            [status_dot, status_text],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=10,
        ),
        bgcolor="#1a0a0a",
        padding=ft.Padding(top=12, bottom=12, left=16, right=16),
        border_radius=12,
        border=make_border("#7f1d1d"),
    )

    # â”€â”€ Mode Selector (custom tabs - no SegmentedButton) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Using simple ElevatedButton pair so there's no serialization risk
    mode_hotspot_btn = ft.FilledButton(
        "ðŸ“¡  Hotspot Mode",
        bgcolor="#1d4ed8",
        color="#ffffff",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        expand=True,
    )
    mode_wifi_btn = ft.FilledButton(
        "ðŸŒ  Wi-Fi Mode",
        bgcolor="#1e293b",
        color="#94a3b8",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        expand=True,
    )

    def select_mode(mode: str):
        nonlocal active_mode, current_detected_ip
        active_mode = mode
        current_detected_ip = detect_ip(active_mode)
        if mode == "hotspot":
            mode_hotspot_btn.bgcolor = "#1d4ed8"
            mode_hotspot_btn.color = "#ffffff"
            mode_wifi_btn.bgcolor = "#1e293b"
            mode_wifi_btn.color = "#94a3b8"
        else:
            mode_wifi_btn.bgcolor = "#1d4ed8"
            mode_wifi_btn.color = "#ffffff"
            mode_hotspot_btn.bgcolor = "#1e293b"
            mode_hotspot_btn.color = "#94a3b8"
        update_guide_card()
        update_url_and_qr()
        page.update()

    mode_hotspot_btn.on_click = lambda e: select_mode("hotspot")
    mode_wifi_btn.on_click = lambda e: select_mode("wifi")

    mode_row = ft.Container(
        content=ft.Row([mode_hotspot_btn, mode_wifi_btn], spacing=8),
        bgcolor="#111827",
        padding=ft.Padding(top=8, bottom=8, left=8, right=8),
        border_radius=12,
        border=make_border("#1f2937"),
    )

    # â”€â”€ Guide Card â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    guide_title = ft.Text("", weight=ft.FontWeight.BOLD, size=13, color="#38bdf8")
    guide_body = ft.Column(spacing=5)
    hotspot_settings_btn = ft.OutlinedButton(
        "âš™  Open Hotspot Settings",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        on_click=lambda e: open_hotspot_settings(),
    )
    guide_container = ft.Container(
        content=ft.Column([guide_title, guide_body, hotspot_settings_btn], spacing=10),
        bgcolor="#0f172a",
        padding=14,
        border_radius=12,
        border=make_border("#1e3a5f"),
    )

    def open_hotspot_settings():
        try:
            subprocess.run(
                ["am", "start", "-a", "android.settings.TETHER_SETTINGS"],
                check=False, timeout=1.5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        notify("Open Settings â€º Network â€º Hotspot & tethering")

    def update_guide_card():
        guide_body.controls.clear()
        if active_mode == "hotspot":
            guide_title.value = "📶  Hotspot Guide (No Router / No Internet)"
            steps = [
                "1. Turn ON phone's Personal Hotspot (mobile data can stay OFF).",
                "2. Connect Laptop / TV / Tablet to this phone's hotspot.",
                "3. Start server then open the URL or scan QR on the client.",
            ]
            hotspot_settings_btn.visible = True
        else:
            guide_title.value = "ðŸŒ  Wi-Fi Router Guide"
            steps = [
                "1. Connect your phone AND the client device to the same Wi-Fi.",
                "2. Start server then open the URL or scan QR on the client.",
                "3. No internet needed â€” works over local LAN.",
            ]
            hotspot_settings_btn.visible = False
        for s in steps:
            guide_body.controls.append(ft.Text(s, size=12, color="#cbd5e1"))

    update_guide_card()

    # â”€â”€ URL + QR Card â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    port_input = ft.TextField(
        label="Port",
        value="8080",
        width=100,
        keyboard_type=ft.KeyboardType.NUMBER,
        text_size=14,
        on_change=lambda e: (_lazy_qr_update()),
    )

    url_text = ft.Text(
        f"http://{current_detected_ip}:8080",
        size=15,
        weight=ft.FontWeight.BOLD,
        color="#38bdf8",
        selectable=True,
    )

    qr_image = ft.Image(
        src=f"data:image/png;base64,{generate_qr_base64(f'http://{current_detected_ip}:8080')}",
        width=160,
        height=160,
        fit=ft.BoxFit.CONTAIN,
    )
    qr_box = ft.Container(
        content=qr_image,
        bgcolor="#ffffff",
        padding=8,
        border_radius=10,
        alignment=get_alignment_center(),
        width=176,
        height=176,
    )

    def _lazy_qr_update():
        update_url_and_qr()
        page.update()

    def update_url_and_qr():
        port_val = (port_input.value or "8080").strip()
        full_url = f"http://{current_detected_ip}:{port_val}"
        url_text.value = full_url
        try:
            new_b64 = generate_qr_base64(full_url)
            qr_image.src = f"data:image/png;base64,{new_b64}"
        except Exception:
            pass

    def on_refresh_ip(e):
        nonlocal current_detected_ip
        current_detected_ip = detect_ip(active_mode)
        update_url_and_qr()
        page.update()
        notify(f"IP refreshed â†’ {current_detected_ip}")

    def on_copy_url(e):
        port_val = (port_input.value or "8080").strip()
        copy_to_clipboard(f"http://{current_detected_ip}:{port_val}")

    url_card = ft.Container(
        content=ft.Column([
            ft.Text("📡  Streaming Address", size=12, color="#64748b"),
            ft.Row([
                url_text,
                ft.IconButton(ft.Icons.COPY, tooltip="Copy URL", on_click=on_copy_url, icon_color="#38bdf8"),
                ft.IconButton(ft.Icons.REFRESH, tooltip="Re-detect IP", on_click=on_refresh_ip, icon_color="#38bdf8"),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Row([qr_box], alignment=ft.MainAxisAlignment.CENTER),
            ft.Text(
                "Scan QR code from TV / Laptop / VLC to connect",
                size=11, color="#64748b", text_align=ft.TextAlign.CENTER,
            ),
            ft.Row([
                ft.OutlinedButton(
                    "📋  Copy VLC Playlist (.m3u)",
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                    on_click=lambda e: copy_to_clipboard(f"{url_text.value}/playlist.m3u"),
                )
            ], alignment=ft.MainAxisAlignment.CENTER),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
        bgcolor="#0f172a",
        padding=14,
        border_radius=12,
        border=make_border("#1e3a5f"),
    )

    # â”€â”€ Config Card (Directory + Port) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    dir_input = ft.TextField(
        label="Video Folder Path",
        value=get_default_media_dir(),
        expand=True,
        prefix_icon=ft.Icons.FOLDER_OPEN,
        text_size=13,
        hint_text="/storage/emulated/0/Movies",
    )

    # Live folder video stats & storage permission helper
    folder_stats_text = ft.Text("Scanning folder...", size=12, color="#94a3b8")

    def update_folder_stats():
        p_str = (dir_input.value or "").strip()
        if not p_str:
            folder_stats_text.value = "⚠️ Please select or type a folder path"
            folder_stats_text.color = "#ef4444"
            return

        p = Path(p_str)
        if not p.exists():
            folder_stats_text.value = f"⚠️ Folder does not exist: {p_str}"
            folder_stats_text.color = "#ef4444"
            return
        if not p.is_dir():
            folder_stats_text.value = f"⚠️ Path is a file, not a directory: {p_str}"
            folder_stats_text.color = "#ef4444"
            return

        try:
            direct_vids = 0
            sub_dirs = 0
            for e in os.scandir(p):
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    sub_dirs += 1
                elif e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTENSIONS:
                    direct_vids += 1

            total_vids = count_videos_in_dir(p)
            if direct_vids > 0:
                folder_stats_text.value = f"🎬 {direct_vids} direct video(s) • {total_vids} total across subfolders"
                folder_stats_text.color = "#22c55e"
            elif total_vids > 0:
                folder_stats_text.value = f"📁 {total_vids} video(s) found inside {sub_dirs} subfolder(s)"
                folder_stats_text.color = "#38bdf8"
            else:
                folder_stats_text.value = "⚠️ 0 video files found. Tap 'Storage Permission' if you have videos on phone."
                folder_stats_text.color = "#f59e0b"
        except Exception as ex:
            folder_stats_text.value = f"⚠️ Permission/Access error: {ex}"
            folder_stats_text.color = "#ef4444"

    # Android All Files Access Permission launcher
    def open_storage_settings():
        commands = [
            ["am", "start", "-a", "android.settings.MANAGE_APP_ALL_FILES_ACCESS_PERMISSION", "-d", "package:com.sevenstream.app"],
            ["am", "start", "-a", "android.settings.MANAGE_ALL_FILES_ACCESS_PERMISSION"],
            ["am", "start", "-a", "android.settings.APPLICATION_DETAILS_SETTINGS", "-d", "package:com.sevenstream.app"],
        ]
        opened = False
        for cmd in commands:
            try:
                res = subprocess.run(cmd, check=False, timeout=1.5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0:
                    opened = True
                    break
            except Exception:
                pass
        notify("Go to Settings > Apps > 7Stream > Permissions > All files access.")

    # Quick folder presets for Android and Desktop
    def set_folder_preset(preset_path: str):
        dir_input.value = preset_path
        update_folder_stats()
        page.update()
        notify(f"Selected: {preset_path}")

    is_android = Path("/storage/emulated/0").exists()
    movies_preset = "/storage/emulated/0/Movies" if is_android else str(Path.home() / "Videos")
    download_preset = "/storage/emulated/0/Download" if is_android else str(Path.home() / "Downloads")
    dcim_preset = "/storage/emulated/0/DCIM" if is_android else str(Path.home() / "Pictures")
    storage_preset = "/storage/emulated/0" if is_android else str(Path.home())

    preset_chips = ft.Row([
        ft.TextButton("🎬 Movies", on_click=lambda e: set_folder_preset(movies_preset)),
        ft.TextButton("📥 Download", on_click=lambda e: set_folder_preset(download_preset)),
        ft.TextButton("📷 DCIM", on_click=lambda e: set_folder_preset(dcim_preset)),
        ft.TextButton("📱 Storage", on_click=lambda e: set_folder_preset(storage_preset)),
    ], wrap=True, spacing=2)

    storage_perm_btn = ft.OutlinedButton(
        "🔐 Grant Storage Permission",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        on_click=lambda e: open_storage_settings(),
    )

    # In-App Pure-Python Directory Browser (Shows BOTH folders and video files!)
    folder_dialog = None
    browse_state = {"path": dir_input.value or get_default_media_dir()}
    folder_list_view = ft.ListView(expand=True, spacing=4, height=260)
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
            found_others = 0

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
                            page.update()
                            notify(f"Selected folder: {parent_d}")

                        folder_list_view.controls.append(
                            ft.ListTile(
                                leading=ft.Icon(ft.Icons.VIDEO_FILE, color="#38bdf8", size=22),
                                title=ft.Text(entry.name, size=13, color="#f1f5f9"),
                                subtitle=ft.Text(f"Video file • {f_size} • Tap to select folder", size=10, color="#38bdf8"),
                                on_click=on_video_click,
                            )
                        )
                    else:
                        found_others += 1

            summary_info = f"{c_path} | 📁 {found_dirs} folders • 🎬 {found_vids} video files"
            if found_others > 0:
                summary_info += f" • 📄 {found_others} other files"
            current_path_text.value = summary_info

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
        page.update()
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
                    ft.Divider(color="#334155", height=1),
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

    # Initial scan of folder stats
    update_folder_stats()

    config_card = ft.Container(
        content=ft.Column([
            ft.Text("Server Settings", weight=ft.FontWeight.BOLD, size=13, color="#94a3b8"),
            ft.Row([dir_input, browse_btn], spacing=4),
            folder_stats_text,
            preset_chips,
            storage_perm_btn,
            ft.Row([
                port_input,
                ft.Text(
                    "Port to serve on (default 8080).",
                    size=11, color="#64748b", expand=True,
                ),
            ], spacing=8, alignment=ft.MainAxisAlignment.START),
        ], spacing=10),
        bgcolor="#0f172a",
        padding=14,
        border_radius=12,
        border=make_border("#1e293b"),
    )

    # â”€â”€ Start / Stop Button â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    start_stop_btn = ft.FilledButton(
        "â–¶   Start Server",
        bgcolor="#1d4ed8",
        color="#ffffff",
        width=260,
        height=48,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
    )

    def toggle_server(e):
        if not server.is_running:
            try:
                port_num = int((port_input.value or "8080").strip())
            except ValueError:
                notify("Port must be a number (e.g. 8080)")
                return
            media_path = (dir_input.value or "").strip()
            if not media_path:
                notify("Please set a video folder path first")
                return

            server.set_config(media_path, port_num)
            success, msg = server.start()
            if success:
                status_dot.bgcolor = "#22c55e"
                status_text.value = f"Server Running  â€¢  {url_text.value}"
                status_text.color = "#22c55e"
                status_card.bgcolor = "#052e16"
                status_card.border = make_border("#14532d")

                start_stop_btn.text = "â¹   Stop Server"
                start_stop_btn.bgcolor = "#b91c1c"

                dir_input.disabled = True
                port_input.disabled = True
                browse_btn.disabled = True
                notify("ðŸŸ¢ Server is LIVE â€” connect your client devices now!")
            else:
                notify(f"Failed to start: {msg}")
        else:
            server.stop()
            status_dot.bgcolor = "#ef4444"
            status_text.value = "Server Stopped"
            status_text.color = "#ef4444"
            status_card.bgcolor = "#1a0a0a"
            status_card.border = make_border("#7f1d1d")

            start_stop_btn.text = "â–¶   Start Server"
            start_stop_btn.bgcolor = "#1d4ed8"

            dir_input.disabled = False
            port_input.disabled = False
            browse_btn.disabled = False
            notify("Server stopped.")

        page.update()

    start_stop_btn.on_click = toggle_server

    # â”€â”€ Header â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logo_path = Path(__file__).resolve().parent / "assets" / "logo.png"
    if logo_path.exists():
        header_icon = ft.Image(
            src=str(logo_path), width=40, height=40,
            border_radius=10, fit=ft.BoxFit.CONTAIN,
        )
    else:
        header_icon = ft.Text("ðŸŽ¬", size=32)

    header = ft.Row([
        header_icon,
        ft.Column([
            ft.Text("7Stream", size=22, weight=ft.FontWeight.BOLD, color="#f1f5f9"),
            ft.Text("Hotspot & Wi-Fi Video Server", size=11, color="#475569"),
        ], spacing=2),
    ], spacing=12)

    # â”€â”€ Assemble Page â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    page.add(
        ft.Column([
            header,
            status_card,
            mode_row,
            guide_container,
            url_card,
            config_card,
            ft.Container(
                content=start_stop_btn,
                alignment=get_alignment_center(),
                padding=make_padding(top=8, bottom=24),
            ),
        ], spacing=14, scroll=ft.ScrollMode.AUTO)
    )


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

