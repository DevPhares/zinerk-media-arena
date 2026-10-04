import os
from pathlib import Path

from .config import BASE_DIR

INTERNAL_STORAGE = Path("/storage/emulated/0")
FALLBACK_BASE = BASE_DIR / "userfiles"

# The folder tree from your design. A dict is a folder, and
# an empty dict {} is a folder with nothing inside it.
TREE = {
    "Audio": {"m4a": {}, "mp3": {}},
    "Video": {"mp4": {}, "mkv": {}, "webm": {}},
    "Images": {
        "image_slides": {"desktop": {}, "full_screen": {}, "small": {}},
        "photos": {"desktop": {}, "full_screen": {}, "small": {}},
    },
    "Playlists": {},
    "Webpages": {},
    "Documents": {"pdf": {}, "docx": {}, "xlsx": {}},
    "torrent_balls": {},
    ".incomplete": {},
    ".thumbnails": {},
}


def default_root(username):
    """Internal storage if Termux can write there, otherwise Termux's private folder."""
    name = f"ZINERK_{username}"
    if INTERNAL_STORAGE.is_dir() and os.access(INTERNAL_STORAGE, os.W_OK):
        return INTERNAL_STORAGE / name
    return FALLBACK_BASE / name


def _build(base, tree):
    for name, sub in tree.items():
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        _build(folder, sub)


def create_tree(root):
    """Create the user's root folder and every subfolder. Safe to run twice."""
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise ValueError("Folder path must be absolute (start with /)")
    root.mkdir(parents=True, exist_ok=True)
    _build(root, TREE)
    return root


def image_bucket(width, height):
    longer = max(width, height)
    if longer > 1080:
        return "desktop"
    if longer > 200:
        return "full_screen"
    return "small"


def destination_for(root, resource_type, fmt=None, bucket="full_screen"):
    """Pick the right subfolder for a download. Unknown things go to torrent_balls."""
    root = Path(root)
    fmt = (fmt or "").lower()

    if resource_type == "audio":
        rel = Path("Audio") / fmt
    elif resource_type == "video":
        rel = Path("Video") / fmt
    elif resource_type == "image_slides":
        rel = Path("Images/image_slides") / bucket
    elif resource_type == "photo":
        rel = Path("Images/photos") / bucket
    elif resource_type == "webpage":
        rel = Path("Webpages")
    elif resource_type == "document":
        rel = Path("Documents") / fmt
    elif resource_type == "playlist":
        rel = Path("Playlists")
    else:
        rel = Path("torrent_balls")

    target = root / rel
    return target if target.is_dir() else root / "torrent_balls"


if __name__ == "__main__":
    import tempfile

    print("default root:", default_root("demo"))
    with tempfile.TemporaryDirectory() as tmp:
        root = create_tree(Path(tmp) / "ZINERK_demo")
        print("top folders:", sorted(p.name for p in root.iterdir()))
        print("mp3  ->", destination_for(root, "audio", "mp3").relative_to(root))
        print("mkv  ->", destination_for(root, "video", "MKV").relative_to(root))
        print("pdf  ->", destination_for(root, "document", "pdf").relative_to(root))
        print("slide 1920x1080 ->",
              destination_for(root, "image_slides",
                              bucket=image_bucket(1920, 1080)).relative_to(root))
        print(".apk ->", destination_for(root, "other", "apk").relative_to(root))
        print("flac ->", destination_for(root, "audio", "flac").relative_to(root))
