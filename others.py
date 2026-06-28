import argparse
import sys
from urllib.parse import urlparse
from typing import Any
from pydantic import validate_call, ConfigDict

from smb_client import SMBClient, MEDIA_EXTENSIONS
from transfer import ensure_dest_dir, get_unique_dest_path

# Decorator to validate call arguments and return values with arbitrary type support
v_call = validate_call(config=ConfigDict(arbitrary_types_allowed=True), validate_return=True)

# ---------------------------------------------------------------------------
# Extension → folder-name mapping for known non-media file types
# ---------------------------------------------------------------------------
EXTENSION_CATEGORY: dict[str, str] = {
    # Audio
    ".mp3": "music",
    ".flac": "music",
    ".aac": "music",
    ".ogg": "music",
    ".wav": "music",
    ".wma": "music",
    ".aiff": "music",
    ".m4a": "music",
    ".opus": "music",
    # Documents
    ".pdf": "documents",
    ".doc": "documents",
    ".docx": "documents",
    ".xls": "documents",
    ".xlsx": "documents",
    ".ppt": "documents",
    ".pptx": "documents",
    ".odt": "documents",
    ".ods": "documents",
    ".odp": "documents",
    ".txt": "documents",
    ".rtf": "documents",
    ".csv": "documents",
    ".md": "documents",
    # Archives
    ".zip": "archives",
    ".tar": "archives",
    ".gz": "archives",
    ".bz2": "archives",
    ".xz": "archives",
    ".7z": "archives",
    ".rar": "archives",
    ".zst": "archives",
    # Executables / installers
    ".exe": "executables",
    ".msi": "executables",
    ".dmg": "executables",
    ".pkg": "executables",
    ".deb": "executables",
    ".rpm": "executables",
    ".appimage": "executables",
    ".sh": "executables",
    ".bat": "executables",
    # Code / data
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".json": "code",
    ".xml": "code",
    ".yaml": "code",
    ".yml": "code",
    ".toml": "code",
    ".ini": "code",
    ".cfg": "code",
    ".html": "code",
    ".css": "code",
    ".sql": "code",
}


@v_call
def category_for(filename: str) -> str:
    """Return the destination folder name for *filename* based on its extension."""
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    return EXTENSION_CATEGORY.get(ext, "unknown")


@v_call
def is_not_media(filename: str) -> bool:
    """Return True if *filename* is NOT a picture or video."""
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    return ext not in MEDIA_EXTENSIONS


@v_call
def collect_others(
    client: SMBClient,
    share: str,
    smb_path: str,
) -> list[str]:
    """
    Recursively walk *smb_path* on *share* and return the list of SMB paths
    for every file that is NOT a picture or video.
    """
    if not client.conn:
        raise RuntimeError("SMBClient is not connected. Call connect() first.")

    results: list[str] = []

    try:
        entries = client.conn.listPath(share, smb_path)
    except Exception as e:
        print(f"  [warning] Could not list {smb_path}: {e}", file=sys.stderr)
        return results

    for entry in entries:
        name = entry.filename
        if name in (".", ".."):
            continue

        child_path = f"{smb_path.rstrip('/')}/{name}"

        if entry.isDirectory:
            results.extend(collect_others(client, share, child_path))
        elif is_not_media(name):
            results.append(child_path)

    return results


@v_call
def transfer_others(source_url: str, dest_url: str) -> None:
    """
    Walk *source_url* (SMB), collect all non-media files, and copy each one
    to *dest_url* under a sub-folder named after its file-type category
    (e.g. ``music/``, ``documents/``, ``unknown/``).

    Music files are a special case: instead of being placed flat inside
    ``music/``, the directory structure *relative to the source scan root*
    is preserved.  For example, if the source root is ``/source`` and a
    file lives at ``/source/albums/Pink Floyd/track.mp3``, it will be
    written to ``{dest}/music/albums/Pink Floyd/track.mp3``.
    """
    # ── Parse source URL ────────────────────────────────────────────────────
    src_parsed = urlparse(source_url)
    src_host = src_parsed.hostname
    src_username = src_parsed.username or ""
    src_password = src_parsed.password or ""
    src_path_parts = src_parsed.path.lstrip("/").split("/", 1)
    src_share = src_path_parts[0]
    src_start_path = "/" + src_path_parts[1] if len(src_path_parts) > 1 else "/"

    # ── Parse destination URL ────────────────────────────────────────────────
    dest_parsed = urlparse(dest_url)
    dest_host = dest_parsed.hostname
    dest_username = dest_parsed.username or ""
    dest_password = dest_parsed.password or ""
    dest_path_parts = dest_parsed.path.lstrip("/").split("/", 1)
    dest_share = dest_path_parts[0]
    dest_base_path = "/" + dest_path_parts[1] if len(dest_path_parts) > 1 else "/"

    print("Connecting to source SMB server …", file=sys.stderr)
    src_client = SMBClient(
        host=src_host, username=src_username, password=src_password, use_ntlm_v2=False
    )

    print("Connecting to destination SMB server …", file=sys.stderr)
    dest_client = SMBClient(
        host=dest_host, username=dest_username, password=dest_password, use_ntlm_v2=True
    )

    with src_client, dest_client:
        print("Scanning source path for non-media files …", file=sys.stderr)
        file_paths = collect_others(src_client, src_share, src_start_path)

        if not file_paths:
            print("No non-media files found on source.", file=sys.stderr)
            return

        print(f"Found {len(file_paths)} file(s) to transfer.", file=sys.stderr)

        for i, src_file_path in enumerate(file_paths, 1):
            filename = src_file_path.rsplit("/", 1)[-1]
            category = category_for(filename)

            print(
                f"[{i}/{len(file_paths)}] {filename}  →  {category}/  …",
                file=sys.stderr,
            )

            # ── Read file from source ────────────────────────────────────────
            try:
                file_bytes = src_client.read_file(src_share, src_file_path)
            except Exception as e:
                print(f"  [Error] Failed to read {src_file_path}: {e}", file=sys.stderr)
                continue

            # ── Build & ensure destination directory ─────────────────────────
            dest_dir_parts = [p for p in dest_base_path.strip("/").split("/") if p]
            dest_dir_parts.append(category)

            if category == "music":
                # Preserve the source directory structure relative to the scan
                # root so that artist/album folders are kept intact.
                # src_start_path  = "/source"
                # src_file_path   = "/source/albums/Pink Floyd/track.mp3"
                # → relative_dir  = "albums/Pink Floyd"
                src_parent = src_file_path.rsplit("/", 1)[0]  # dir containing the file
                relative_dir = src_parent.lstrip("/")
                src_root_stripped = src_start_path.strip("/")
                if src_root_stripped and relative_dir.startswith(src_root_stripped):
                    relative_dir = relative_dir[len(src_root_stripped):].lstrip("/")
                if relative_dir:
                    dest_dir_parts.extend(relative_dir.split("/"))

            dest_dir_path = "/" + "/".join(dest_dir_parts)

            ensure_dest_dir(dest_client, dest_share, dest_dir_parts)

            # ── Resolve filename collision ────────────────────────────────────
            unique_dest_path = get_unique_dest_path(
                dest_client, dest_share, dest_dir_path, filename
            )

            # ── Write to destination ─────────────────────────────────────────
            try:
                dest_client.write_file(dest_share, unique_dest_path, file_bytes)
                print(f"  Saved → {dest_share}{unique_dest_path}", file=sys.stderr)
            except Exception as e:
                print(
                    f"  [Error] Failed to write {unique_dest_path}: {e}",
                    file=sys.stderr,
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy non-media (non-picture/video) files from source SMB to destination SMB, "
        "organising them by file-type category (music, documents, archives, …)."
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Source SMB URL, e.g. smb://user:pass@host/share/path",
    )
    parser.add_argument(
        "--destination",
        required=True,
        help="Destination SMB URL, e.g. smb://user:pass@host/share/others",
    )
    args = parser.parse_args()

    try:
        transfer_others(args.source, args.destination)
        print("Transfer completed successfully.", file=sys.stderr)
    except Exception as e:
        print(f"Error during transfer: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
