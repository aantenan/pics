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
import sys
from urllib.parse import urlparse
from typing import Any
from pydantic import validate_call, ConfigDict

from smb_client import SMBClient, VIDEO_EXTENSIONS

# Decorator to validate call arguments and return values with arbitrary type support
v_call = validate_call(config=ConfigDict(arbitrary_types_allowed=True), validate_return=True)


@v_call
def build_media_tree(smb_url: str) -> dict[str, Any]:
    """
    Entry point: connect to the SMB share described by `smb_url` and
    return the full media tree as a nested dict.
    """
    parsed = urlparse(smb_url)
    host = parsed.hostname
    username = parsed.username or ""
    password = parsed.password or ""

    path_parts = parsed.path.lstrip("/").split("/", 1)
    share = path_parts[0]
    start_path = "/" + path_parts[1] if len(path_parts) > 1 else "/"

    # Normalise the base URL for building child URLs later
    base_url = f"smb://{host}/{share}{start_path.rstrip('/')}"

    print(f"Connecting to smb://{host}/{share} …", file=sys.stderr)
    client = SMBClient(host=host, username=username, password=password)

    print(f"Traversing {start_path} …", file=sys.stderr)
    with client:
        tree = client.traverse(share, start_path, base_url)

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