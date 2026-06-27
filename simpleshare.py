"""
smb_media_tree.py
-----------------
Traverses an SMB share starting from a given path and returns a nested
dictionary reflecting the directory tree, containing only image and video files.

Requirements:
    pip install pysmb

Usage:
    python smb_media_tree.py smb://nas-server/share
    python smb_media_tree.py smb://nas-server/share/photos
    python smb_media_tree.py smb://nas-server/share/photos --json
    python smb_media_tree.py smb://nas-server/share/photos --json --output tree.json

Tree structure returned:
    {
        "Photos": {                         # directory  → nested dict
            "2024": {
                "vacation.jpg": "smb://nas/share/Photos/2024/vacation.jpg",
                "clip.mp4":     "smb://nas/share/Photos/2024/clip.mp4",
            }
        },
        "Videos": {
            "movie.mkv": "smb://nas/share/Videos/movie.mkv"
        }
    }

Directories that contain no media (at any depth) are omitted.
"""

import argparse
import json
import socket
import sys
from urllib.parse import urlparse, unquote
from typing import Any
from pydantic import validate_call, ConfigDict

try:
    from smb.SMBConnection import SMBConnection
    HAS_PYSMB = True
except ImportError:
    class SMBConnection:
        pass
    HAS_PYSMB = False

# Decorator to validate call arguments and return values with arbitrary type support
v_call = validate_call(config=ConfigDict(arbitrary_types_allowed=True), validate_return=True)


# ── Media extensions ──────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".raw", ".cr2", ".cr3", ".nef",
    ".arw", ".dng", ".orf", ".rw2", ".sr2",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".mts", ".m2ts",
    ".vob", ".ogv", ".rm", ".rmvb",
}

MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


@v_call
def is_media(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in MEDIA_EXTENSIONS)


# ── SMB connection ────────────────────────────────────────────────────────────

@v_call
def make_connection(host: str, remote_name: str) -> SMBConnection:
    """Create and return an authenticated (guest) SMBConnection."""
    if not HAS_PYSMB:
        raise ImportError("pysmb is not installed. Run: pip install pysmb")

    conn = SMBConnection(
        username="",
        password="",
        my_name=socket.gethostname(),
        remote_name=remote_name,
        use_ntlm_v2=False,
        is_direct_tcp=False,
    )

    host_ip = socket.gethostbyname(host)

    connected = conn.connect(host_ip, port=139, timeout=15)
    if not connected:
        connected = conn.connect(host_ip, port=445, timeout=15)
    if not connected:
        raise ConnectionError(f"Could not connect to SMB host: {host}")

    return conn


# ── Traversal ─────────────────────────────────────────────────────────────────

@v_call
def _traverse(conn: SMBConnection, share: str, smb_path: str, base_url: str) -> dict[str, Any] | None:
    """
    Recursively list `smb_path` on `share`.

    Returns a dict where:
      - each subdirectory key maps to another dict (or None if empty of media)
      - each media file key maps to its full smb:// URL

    Returns None if the directory contains no media at any depth.
    """
    try:
        entries = conn.listPath(share, smb_path)
    except Exception as e:
        print(f"  [warning] Could not list {smb_path}: {e}", file=sys.stderr)
        return None

    node = {}

    for entry in entries:
        name = entry.filename
        if name in (".", ".."):
            continue

        child_smb_path = f"{smb_path.rstrip('/')}/{name}"
        child_url = f"{base_url.rstrip('/')}/{name}"

        if entry.isDirectory:
            subtree = _traverse(conn, share, child_smb_path, child_url)
            if subtree is not None:          # only include dirs with media
                node[name] = subtree
        else:
            print(name)
            if is_media(name):
                node[name] = child_url

    return node if node else None            # prune empty dirs


@v_call
def build_media_tree(smb_url: str) -> dict[str, Any]:
    """
    Entry point: connect to the SMB share described by `smb_url` and
    return the full media tree as a nested dict.
    """
    if not HAS_PYSMB:
        raise ImportError("pysmb is not installed. Run: pip install pysmb")

    parsed = urlparse(smb_url)
    host = parsed.hostname
    # NetBIOS name = uppercase hostname without domain
    remote_name = host.split(".")[0].upper()

    path_parts = parsed.path.lstrip("/").split("/", 1)
    share = path_parts[0]
    start_path = "/" + path_parts[1] if len(path_parts) > 1 else "/"

    # Normalise the base URL for building child URLs later
    base_url = f"smb://{host}/{share}{start_path.rstrip('/')}"

    print(f"Connecting to smb://{host}/{share} …", file=sys.stderr)
    conn = make_connection(host, remote_name)

    print(f"Traversing {start_path} …", file=sys.stderr)
    try:
        tree = _traverse(conn, share, start_path, base_url)
    finally:
        conn.close()

    return tree or {}


# ── CLI ───────────────────────────────────────────────────────────────────────

@v_call
def print_tree(node: dict[str, Any], indent: int = 0) -> None:
    """Pretty-print the tree to stdout."""
    prefix = "  " * indent
    for key, value in sorted(node.items()):
        if isinstance(value, dict):
            print(f"{prefix}📁  {key}/")
            print_tree(value, indent + 1)
        else:
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            icon = "🎬" if f".{ext}" in VIDEO_EXTENSIONS else "🖼 "
            print(f"{prefix}{icon}  {key}")


@v_call
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Traverse an SMB share and return a media file tree."
    )
    parser.add_argument(
        "url",
        help="SMB URL to start from, e.g. smb://nas/share or smb://nas/share/photos",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the tree as JSON instead of a pretty-printed tree",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write JSON output to FILE instead of stdout",
    )
    args = parser.parse_args()

    tree = build_media_tree(args.url)

    if args.json or args.output:
        payload = json.dumps(tree, indent=2, ensure_ascii=False)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(payload)
            print(f"Tree written to {args.output}", file=sys.stderr)
        else:
            print(payload)
    else:
        if tree:
            print_tree(tree)
        else:
            print("No media files found.")



if __name__ == "__main__":
    main()