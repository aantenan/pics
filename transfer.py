import argparse
import sys
import io
from datetime import datetime
from urllib.parse import urlparse
from typing import Any
from pydantic import validate_call, ConfigDict
from PIL import Image

from smb_client import SMBClient
from metadata import get_exif_data, get_timestamp

# Decorator to validate call arguments and return values with arbitrary type support
v_call = validate_call(config=ConfigDict(arbitrary_types_allowed=True), validate_return=True)


@v_call
def flatten_tree(tree: dict[str, Any]) -> list[str]:
    """Flatten nested media tree dictionary into a list of URLs."""
    urls = []
    for key, val in sorted(tree.items()):
        if isinstance(val, dict):
            urls.extend(flatten_tree(val))
        else:
            urls.append(val)
    return urls


@v_call
def get_file_path_from_url(url: str) -> str:
    """Extract share path from SMB URL."""
    parsed = urlparse(url)
    path_parts = parsed.path.lstrip("/").split("/", 1)
    if len(path_parts) < 2:
        raise ValueError(f"Invalid SMB URL: {url}")
    return "/" + path_parts[1]


@v_call
def ensure_dest_dir(dest_client: SMBClient, dest_share: str, path_parts: list[str]) -> None:
    """Create directory structure level by level, ignoring errors if directories exist."""
    current = ""
    for part in path_parts:
        current = f"{current}/{part}"
        try:
            dest_client.create_directory(dest_share, current)
        except Exception:
            pass


@v_call
def get_unique_dest_path(dest_client: SMBClient, dest_share: str, dest_dir: str, filename: str) -> str:
    """Resolve filename collisions by appending _1, _2, etc."""
    if "." in filename:
        base, ext = filename.rsplit(".", 1)
        ext = f".{ext}"
    else:
        base, ext = filename, ""

    counter = 0
    while True:
        candidate_name = f"{base}_{counter}{ext}" if counter > 0 else filename
        candidate_path = f"{dest_dir.rstrip('/')}/{candidate_name}"
        try:
            dest_client.get_attributes(dest_share, candidate_path)
            counter += 1
        except Exception:
            return candidate_path


@v_call
def transfer_media(source_url: str, dest_url: str) -> None:
    """Traverse source SMB path, pull media, and write to destination year/month/day/ path."""
    # Parse source URL
    src_parsed = urlparse(source_url)
    src_host = src_parsed.hostname
    src_username = src_parsed.username or ""
    src_password = src_parsed.password or ""
    src_path_parts = src_parsed.path.lstrip("/").split("/", 1)
    src_share = src_path_parts[0]
    src_start_path = "/" + src_path_parts[1] if len(src_path_parts) > 1 else "/"
    src_base_url = f"smb://{src_host}/{src_share}{src_start_path.rstrip('/')}"

    # Parse destination URL
    dest_parsed = urlparse(dest_url)
    dest_host = dest_parsed.hostname
    dest_username = dest_parsed.username or ""
    dest_password = dest_parsed.password or ""
    dest_path_parts = dest_parsed.path.lstrip("/").split("/", 1)
    dest_share = dest_path_parts[0]
    dest_dest_path = "/" + dest_path_parts[1] if len(dest_path_parts) > 1 else "/"

    print("Connecting to source SMB server ...", file=sys.stderr)
    src_client = SMBClient(host=src_host, username=src_username, password=src_password, use_ntlm_v2=False)
    
    print("Connecting to destination SMB server ...", file=sys.stderr)
    dest_client = SMBClient(host=dest_host, username=dest_username, password=dest_password, use_ntlm_v2=True)

    with src_client, dest_client:
        print("Traversing source path for media files ...", file=sys.stderr)
        tree = src_client.traverse(src_share, src_start_path, src_base_url)
        if not tree:
            print("No media files found on source.", file=sys.stderr)
            return

        urls = flatten_tree(tree)
        print(f"Found {len(urls)} media file(s) to transfer.", file=sys.stderr)

        for i, url in enumerate(urls, 1):
            src_file_path = get_file_path_from_url(url)
            filename = src_file_path.rsplit("/", 1)[-1]
            print(f"[{i}/{len(urls)}] Processing {filename} ...", file=sys.stderr)

            # Pull file bytes
            try:
                file_bytes = src_client.read_file(src_share, src_file_path)
            except Exception as e:
                print(f"  [Error] Failed to read {src_file_path}: {e}", file=sys.stderr)
                continue

            # Extract capture time from EXIF
            ts = None
            try:
                img = Image.open(io.BytesIO(file_bytes))
                exif = get_exif_data(img)
                ts = get_timestamp(exif)
            except Exception as e:
                print(e)
                pass

            # Fallback to last modified time
            if ts is None:
                try:
                    attrs = src_client.get_attributes(src_share, src_file_path)
                    ts = datetime.fromtimestamp(attrs.last_write_time)
                except Exception as e:
                    print(e)
                    pass

            # Fallback to current time
            if ts is None:
                ts = datetime.now()

            # Format destination path: year/month/day
            year = f"{ts.year:04d}"
            month = f"{ts.month:02d}"
            day = f"{ts.day:02d}"

            dest_dir_parts = [dest_dest_path.strip("/")] + [year, month, day]
            dest_dir_parts = [p for p in dest_dir_parts if p]
            dest_dir_path = "/" + "/".join(dest_dir_parts)

            # Ensure destination directories exist
            ensure_dest_dir(dest_client, dest_share, dest_dir_parts)

            # Resolve filename collision
            unique_dest_file_path = get_unique_dest_path(dest_client, dest_share, dest_dir_path, filename)

            # Write file
            try:
                dest_client.write_file(dest_share, unique_dest_file_path, file_bytes)
                print(f"  Saved to: {dest_share}{unique_dest_file_path}", file=sys.stderr)
            except Exception as e:
                print(f"  [Error] Failed to write {unique_dest_file_path} to destination: {e}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer and organize SMB media files by date.")
    parser.add_argument("--source", required=True, help="Source SMB URL (e.g. smb://user:pass@host/share/src)")
    parser.add_argument("--destination", required=True, help="Destination SMB URL (e.g. smb://user:pass@host/share/dest)")
    args = parser.parse_args()

    try:
        transfer_media(args.source, args.destination)
        print("Transfer completed successfully.", file=sys.stderr)
    except Exception as e:
        print(f"Error during transfer: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
