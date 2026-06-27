import sys

from metadata import process_image

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python image_metadata.py <path-or-smb-url> [...]")
        print("  Local:  python image_metadata.py photo.jpg")
        print("  SMB:    python image_metadata.py smb://user:pass@server/share/photo.jpg")
        sys.exit(1)

    for arg in sys.argv[1:]:
        process_image(arg)


if __name__ == "__main__":
    main()