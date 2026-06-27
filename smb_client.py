import io
import socket
import sys
from typing import Any, Callable
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

def resolve_mdns(hostname: str, timeout: float = 5.0) -> str:
    """Resolve a .local mDNS hostname to an IP address."""
    import socket
    if not hostname.endswith(".local"):
        return socket.gethostbyname(hostname)

    from zeroconf import Zeroconf
    from zeroconf._utils.ipaddress import get_ip_address_object_from_record
    import time

    name = hostname.removesuffix(".local")
    zc = Zeroconf()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            info = zc.get_service_info("_smb._tcp.local.", f"{name}._smb._tcp.local.")
            if info and info.addresses:
                return socket.inet_ntoa(info.addresses[0])
            # Fall back to raw A record lookup
            record = zc.cache.get_by_details(f"{hostname}.", "A", "IN")
            if record:
                return get_ip_address_object_from_record(record).compressed
            time.sleep(0.1)
    finally:
        zc.close()

    raise ConnectionError(f"Could not resolve mDNS hostname: {hostname}")


class SMBClient:
    @v_call
    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        remote_name: str | None = None,
        my_name: str | None = None,
        use_ntlm_v2: bool = False
    ) -> None:
        if not HAS_PYSMB:
            raise ImportError("pysmb is not installed. Run: pip install pysmb")
        self.host = host
        self.username = username
        self.password = password
        self.remote_name = remote_name or host.split(".")[0].upper()
        self.my_name = my_name or socket.gethostname()
        self.conn: SMBConnection | None = None
        self.use_ntlm_v2 = use_ntlm_v2

    @v_call
    def connect(self) -> None:
        try:
            host_ip = resolve_mdns(self.host)
            print(f"host_ip = {host_ip}")
        except socket.gaierror:
            host_ip = self.host

        for is_direct, port in [(False, 139), (True, 445)]:
            conn = SMBConnection(
                username=self.username,
                password=self.password,
                my_name=self.my_name,
                remote_name=self.remote_name,
                use_ntlm_v2=self.use_ntlm_v2,
                is_direct_tcp=is_direct,
            )
            try:
                if conn.connect(host_ip, port=port, timeout=15):
                    self.conn = conn
                    return
            except Exception as e:
                print(e)
                continue

        raise ConnectionError(f"Could not connect to SMB host: {self.host}")

    @v_call
    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "SMBClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    @v_call
    def traverse(
        self,
        share: str,
        smb_path: str,
        base_url: str,
        filter_fn: Callable[[str], bool] = is_media,
    ) -> dict[str, Any] | None:
        """
        Recursively list `smb_path` on `share`.

        Returns a dict where:
          - each subdirectory key maps to another dict (or None if empty of media)
          - each media file key maps to its full smb:// URL

        Returns None if the directory contains no media at any depth.
        """
        if not self.conn:
            raise RuntimeError("SMBClient is not connected. Call connect() first.")

        try:
            entries = self.conn.listPath(share, smb_path)
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
                subtree = self.traverse(share, child_smb_path, child_url, filter_fn)
                if subtree is not None:          # only include dirs with media
                    node[name] = subtree
            else:
                print(name)
                if filter_fn(name):
                    node[name] = child_url

        return node if node else None            # prune empty dirs

    @v_call
    def create_directory(self, share: str, path: str, timeout: int = 30) -> None:
        """Create a new directory on the remote SMB server."""
        if not self.conn:
            raise RuntimeError("SMBClient is not connected. Call connect() first.")
        self.conn.createDirectory(share, path, timeout)

    @v_call
    def read_file(self, share: str, path: str, file_obj: Any = None, timeout: int = 30) -> bytes | tuple[Any, int]:
        """
        Retrieve a file from the SMB server.
        If file_obj is None, returns the file contents as bytes.
        Otherwise, writes to file_obj and returns the tuple (file_attributes, filesize).
        """
        if not self.conn:
            raise RuntimeError("SMBClient is not connected. Call connect() first.")
        if file_obj is None:
            buf = io.BytesIO()
            self.conn.retrieveFile(share, path, buf, timeout)
            buf.seek(0)
            return buf.read()
        return self.conn.retrieveFile(share, path, file_obj, timeout)

    @v_call
    def write_file(self, share: str, path: str, data: Any, timeout: int = 30) -> int:
        """
        Write a file to the SMB server.
        data can be bytes, a string, or a file-like object with a read method.
        Returns the number of bytes written.
        """
        if not self.conn:
            raise RuntimeError("SMBClient is not connected. Call connect() first.")
        if isinstance(data, str):
            file_obj = io.BytesIO(data.encode('utf-8'))
        elif isinstance(data, bytes):
            file_obj = io.BytesIO(data)
        elif hasattr(data, 'read'):
            file_obj = data
        else:
            raise TypeError("data must be bytes, str, or a file-like object with read()")
        return self.conn.storeFile(share, path, file_obj, timeout)

    @v_call
    def get_attributes(self, share: str, path: str, timeout: int = 30) -> Any:
        """Get attributes of a file or directory on the SMB server."""
        if not self.conn:
            raise RuntimeError("SMBClient is not connected. Call connect() first.")
        return self.conn.getAttributes(share, path, timeout)
