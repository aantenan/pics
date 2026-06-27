"""
image_metadata.py
-----------------
Extracts the capture timestamp and city from a JPEG/TIFF image's EXIF metadata.
Supports local file paths and SMB URLs (smb://host/share/path).

Requirements:
    pip install Pillow geopy pysmb

Usage:
    python image_metadata.py photo.jpg
    python image_metadata.py smb://nas-server/share/photos/img.jpg
"""

import io
import sys
import socket
from datetime import datetime
from urllib.parse import urlparse, unquote

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError


# ── SMB ───────────────────────────────────────────────────────────────────────

def read_smb_file(smb_url: str) -> io.BytesIO:
    """
    Parse an smb:// URL and return the remote file as an in-memory BytesIO buffer.
    Connects as a guest/anonymous user — no credentials required.

    URL format:  smb://hostname/share/path/to/file.jpg
    """
    try:
        from smb.SMBConnection import SMBConnection
    except ImportError:
        raise ImportError("pysmb is not installed. Run: pip install pysmb")

    parsed = urlparse(smb_url)
    host = parsed.hostname

    # Split /share/rest/of/path
    path_parts = parsed.path.lstrip("/").split("/", 1)
    if len(path_parts) < 2:
        raise ValueError(
            f"SMB URL must include both a share and a file path: {smb_url}"
        )
    share, file_path = path_parts
    file_path = "/" + file_path  # pysmb expects a leading slash on the path

    # Resolve hostname to IP (pysmb needs the IP for the connection)
    try:
        host_ip = socket.gethostbyname(host)
    except socket.gaierror:
        host_ip = host  # fall back; let pysmb handle the error

    # Guest / anonymous login: empty username + password, SMB1 enabled
    conn = SMBConnection(
        username="",
        password="",
        my_name=socket.gethostname(),
        remote_name=host,
        use_ntlm_v2=False,   # many old devices only support NTLMv1
        is_direct_tcp=False,  # use NetBIOS over TCP (port 139) for old devices
    )

    connected = conn.connect(host_ip, port=139, timeout=15)
    if not connected:
        # Fall back to direct TCP (port 445) in case the device supports it
        connected = conn.connect(host_ip, port=445, timeout=15)
    if not connected:
        raise ConnectionError(f"Could not connect to SMB host: {host}")

    buf = io.BytesIO()
    try:
        conn.retrieveFile(share, file_path, buf)
    finally:
        conn.close()

    buf.seek(0)
    return buf


# ── Image loading ─────────────────────────────────────────────────────────────

def open_image(path_or_url: str) -> Image.Image:
    """Open an image from a local path or an smb:// URL."""
    if path_or_url.lower().startswith("smb://"):
        print("  Connecting via SMB (guest) …")
        buf = read_smb_file(path_or_url)
        return Image.open(buf)
    return Image.open(path_or_url)


# ── EXIF helpers ──────────────────────────────────────────────────────────────

def get_exif_data(img: Image.Image) -> dict:
    """Return a dict of human-readable EXIF tag → value."""
    raw_exif = img._getexif()
    if raw_exif is None:
        return {}
    return {TAGS.get(tag_id, tag_id): value for tag_id, value in raw_exif.items()}


def get_timestamp(exif: dict) -> datetime | None:
    """
    Pull the capture time from the EXIF block.
    Prefers DateTimeOriginal → DateTimeDigitized → DateTime.
    """
    for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        raw = exif.get(key)
        if raw:
            try:
                return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
            except ValueError:
                pass
    return None


# ── GPS helpers ───────────────────────────────────────────────────────────────

def _dms_to_decimal(dms_tuple, ref: str) -> float:
    """Convert degrees/minutes/seconds (as rationals) + hemisphere ref to decimal degrees."""
    degrees, minutes, seconds = dms_tuple
    decimal = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def get_gps_coords(exif: dict) -> tuple[float, float] | None:
    """Return (latitude, longitude) in decimal degrees, or None if GPS data is absent."""
    gps_raw = exif.get("GPSInfo")
    if not gps_raw:
        return None

    gps = {GPSTAGS.get(tag_id, tag_id): value for tag_id, value in gps_raw.items()}

    lat_dms = gps.get("GPSLatitude")
    lat_ref = gps.get("GPSLatitudeRef")
    lon_dms = gps.get("GPSLongitude")
    lon_ref = gps.get("GPSLongitudeRef")

    if not all([lat_dms, lat_ref, lon_dms, lon_ref]):
        return None

    return (
        _dms_to_decimal(lat_dms, lat_ref),
        _dms_to_decimal(lon_dms, lon_ref),
    )


# ── Reverse geocoding ─────────────────────────────────────────────────────────

def coords_to_city(lat: float, lon: float) -> str | None:
    """Reverse-geocode coordinates to a city name via Nominatim (no API key needed)."""
    geolocator = Nominatim(user_agent="image_metadata_reader/1.0")
    try:
        location = geolocator.reverse((lat, lon), language="en", timeout=10)
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        print(f"  [geocoding error] {e}", file=sys.stderr)
        return None

    if location is None:
        return None

    addr = location.raw.get("address", {})
    for key in ("city", "town", "village", "suburb", "county", "state"):
        if addr.get(key):
            country = addr.get("country", "")
            return f"{addr[key]}, {country}".strip(", ")

    return location.address


# ── Main ──────────────────────────────────────────────────────────────────────

def process_image(path_or_url: str) -> None:
    print(f"\n📷  {path_or_url}")
    print("─" * 60)

    try:
        img = open_image(path_or_url)
        exif = get_exif_data(img)
    except ImportError as e:
        print(f"  Error: {e}")
        return
    except Exception as e:
        print(f"  Error opening image: {e}")
        return

    if not exif:
        print("  No EXIF metadata found in this image.")
        return

    # Timestamp
    ts = get_timestamp(exif)
    print(f"  Timestamp : {ts.strftime('%Y-%m-%d %H:%M:%S') if ts else 'not found in EXIF'}")

    # GPS / City
    coords = get_gps_coords(exif)
    if coords:
        lat, lon = coords
        print(f"  GPS       : {lat:.6f}, {lon:.6f}")
        print("  Resolving city via Nominatim …")
        city = coords_to_city(lat, lon)
        print(f"  City      : {city or 'could not determine'}")
    else:
        print("  GPS       : no GPS data embedded in this image")
        print("  City      : n/a")

