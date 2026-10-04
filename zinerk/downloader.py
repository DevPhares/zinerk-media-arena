import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from . import folders, jobs, ui
from .config import DATA_DIR, now_ms

# yt-dlp nests deeply while loading its extractors. Allow more depth, and give
# each download thread more memory to do it in (must be set before threads start).
sys.setrecursionlimit(5000)
threading.stack_size(16 * 1024 * 1024)

VIDEO_SITES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "tiktok.com": "TikTok",
    "instagram.com": "Instagram", "facebook.com": "Facebook", "fb.watch": "Facebook",
    "twitter.com": "X", "x.com": "X", "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion",
}
IMAGE_SITES = {
    "pinterest.com": "Pinterest", "pin.it": "Pinterest",
    "flickr.com": "Flickr", "imgur.com": "Imgur",
}
DOC_EXTS = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}

RESOURCE_TYPES = [
    ("audio", "Audio"),
    ("video", "Video"),
    ("image_slides", "Image slides (multi-image post)"),
    ("photo", "Photo (single image)"),
    ("webpage", "Webpage"),
    ("document", "Document"),
]
FORMATS = {
    "audio": ["m4a", "mp3"],
    "video": ["mp4", "mkv", "webm"],
    "document": ["pdf", "docx", "xlsx"],
}
TOOL_FOR = {
    "audio": "yt-dlp", "video": "yt-dlp",
    "image_slides": "gallery-dl", "photo": "gallery-dl",
    "webpage": "other", "document": "other",
}


# ---------- link checking and detection ----------

def normalize_url(raw):
    url = (raw or "").strip()
    if not url or re.search(r"\s", url):
        return None
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc:
        return None
    return url


def _host(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _match(host, table):
    for domain, label in table.items():
        if host == domain or host.endswith("." + domain):
            return label
    return None


def detect(url):
    """Best guess about the link. Returns a dict, or None if we can't tell."""
    host = _host(url)
    path = urlparse(url).path.lower()
    label = _match(host, VIDEO_SITES)
    if label:
        return {"platform": label, "label": f"{label} video"}
    label = _match(host, IMAGE_SITES)
    if label:
        return {"platform": label, "label": f"{label} image"}
    for ext, fmt in DOC_EXTS.items():
        if path.endswith(ext):
            return {"platform": host, "label": f"{fmt.upper()} document"}
    if host:
        return {"platform": host, "label": f"webpage on {host}"}
    return None


# ---------- small helpers ----------

def _clean(text):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    return text.replace("ERROR: ", "").strip()[:300]


def _unique(path):
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _bucket_for(file):
    """Sort an image by size. Needs Pillow (pkg install python-pillow); else full_screen."""
    try:
        from PIL import Image
        with Image.open(file) as img:
            width, height = img.size
        return folders.image_bucket(width, height)
    except Exception:
        return "full_screen"


def _target_dir(base, resource_type, file):
    is_image = resource_type in ("image_slides", "photo")
    if is_image and base.parent.name == "Images" and base.name in ("image_slides", "photos"):
        return base / _bucket_for(file)
    return base


# ---------- the runner ----------

class Runner:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancels = {}
        self._procs = {}

    def is_running(self, job_id):
        with self._lock:
            return job_id in self._cancels

    def start(self, user, job_id):
        with self._lock:
            if job_id in self._cancels:
                return
            cancel = threading.Event()
            self._cancels[job_id] = cancel
        threading.Thread(target=self._run, args=(user, job_id, cancel), daemon=True).start()

    def stop_all(self):
        with self._lock:
            cancels = list(self._cancels.values())
            procs = list(self._procs.values())
        for c in cancels:
            c.set()
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass

    def wait_idle(self, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if not self._cancels:
                    return
            time.sleep(0.2)

    def _run(self, user, job_id, cancel):
        uid = user["id"]
        started = time.time()
        try:
            job = jobs.get_job(uid, job_id)
            if job is None or job["status"] == "done":
                return
            jobs.update_job(uid, job_id, status="running", started_at=now_ms(), error=None)

            # Each job gets its own temp folder, so a resume continues where it stopped.
            tmp = Path(user["root_folder"]) / ".incomplete" / f"job_{job_id}"
            tmp.mkdir(parents=True, exist_ok=True)

            engines = {"yt-dlp": self._ytdlp, "gallery-dl": self._gallerydl}
            title = engines.get(job["tool"], self._direct)(job, tmp, cancel)

            if cancel.is_set():
                jobs.update_job(uid, job_id, status="pending")
                return

            files = [p for p in tmp.rglob("*")
                     if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
            if not files:
                raise RuntimeError("Nothing was downloaded")
            total = sum(p.stat().st_size for p in files)

            self._deliver(uid, job, files)
            shutil.rmtree(tmp, ignore_errors=True)  # only this job's own temp folder

            jobs.update_job(
                uid, job_id, status="done", finished_at=now_ms(),
                elapsed_ms=int((time.time() - started) * 1000),
                bytes_downloaded=total, title=title,
            )
            jobs.add_bandwidth(uid, job["tool"], total)
            ui.toast(f"Done: {title or job['url']}")
        except BaseException as e:
            # Save the full technical error so we can read it later.
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                (DATA_DIR / "error.log").write_text(
                    f"job {job_id}\n{traceback.format_exc()}"
                )
            except OSError:
                pass
            if cancel.is_set():
                jobs.update_job(uid, job_id, status="pending")
            else:
                jobs.update_job(uid, job_id, status="failed", error=str(e)[:500])
                ui.toast(f"Failed: {str(e)[:100]}")
        finally:
            with self._lock:
                self._cancels.pop(job_id, None)
                self._procs.pop(job_id, None)

    def _deliver(self, uid, job, files):
        """Download once, copy to every destination."""
        for dest in jobs.get_destinations(uid, job["id"]):
            if dest["copied"]:
                continue
            base = Path(dest["path"])
            for f in files:
                folder = _target_dir(base, job["resource_type"], f)
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, _unique(folder / f.name))
            jobs.mark_copied(uid, dest["id"])

    # ----- engine 1: yt-dlp (audio, video) -----

    def _ytdlp(self, job, tmp, cancel):
        try:
            import yt_dlp
            from yt_dlp.utils import DownloadCancelled, DownloadError
        except ImportError:
            raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")

        state = {"title": None, "started": False, "half": False}

        def hook(d):
            if cancel.is_set():
                raise DownloadCancelled("stopped by user")
            info = d.get("info_dict") or {}
            if info.get("title"):
                state["title"] = info["title"]
            name = state["title"] or job["url"]
            if not state["started"]:
                state["started"] = True
                ui.toast(f"Started: {name}")
            if d.get("status") == "downloading" and not state["half"]:
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                if total and done / total >= 0.5:
                    state["half"] = True
                    ui.toast(f"Halfway: {name}")

        fmt = job["format"]
        opts = {
            "outtmpl": str(tmp / "%(title)s [%(id)s].%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True,
            "noprogress": True, "trim_file_name": 150, "retries": 5,
            "progress_hooks": [hook],
        }
        if job["resource_type"] == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt, "preferredquality": "192",
            }]
        else:
            if fmt == "mp4":
                opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
            elif fmt == "webm":
                opts["format"] = "bv*[ext=webm]+ba[ext=webm]/bv*+ba/b"
            else:
                opts["format"] = "bv*+ba/b"
            opts["merge_output_format"] = fmt

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                code = ydl.download([job["url"]])
        except DownloadError as e:
            raise RuntimeError(_clean(str(e)))
        if cancel.is_set():
            return state["title"]
        if code:
            raise RuntimeError("yt-dlp reported an error")
        return state["title"]

    # ----- engine 2: gallery-dl (images) -----

    def _gallerydl(self, job, tmp, cancel):
        cmd = [sys.executable, "-m", "gallery_dl", "-D", str(tmp), job["url"]]
        ui.toast(f"Started: {job['url']}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        with self._lock:
            self._procs[job["id"]] = proc

        count, tail = 0, []
        for line in proc.stdout:
            line = line.strip()
            if line.startswith(str(tmp)):
                count += 1
            elif line:
                tail = (tail + [line])[-3:]
            if cancel.is_set():
                proc.terminate()
                break
        proc.wait()

        if cancel.is_set():
            return None
        if proc.returncode != 0 and count == 0:
            raise RuntimeError(_clean(" ".join(tail)) or "gallery-dl failed")
        return f"{count} file(s) from {_host(job['url'])}"

    # ----- engine 3: plain download (documents, webpages) -----

    def _direct(self, job, tmp, cancel):
        url = job["url"]
        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) ZINERK"}
        try:
            r = requests.get(url, stream=True, timeout=30, headers=headers)
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Could not fetch the link: {e}")

        if job["resource_type"] == "webpage":
            name = f"{_host(url)}_{time.strftime('%Y%m%d_%H%M%S')}.html"
        else:
            name = Path(unquote(urlparse(url).path)).name or "document"
            if Path(name).suffix.lower() not in DOC_EXTS:
                name += "." + (job["format"] or "pdf")
        name = re.sub(r'[\\/:*?"<>|]', "_", name)[:150]

        total = int(r.headers.get("Content-Length") or 0)
        ui.toast(f"Started: {name}")
        done, half = 0, False
        with r, open(tmp / name, "wb") as f:
            for chunk in r.iter_content(65536):
                if cancel.is_set():
                    return None
                f.write(chunk)
                done += len(chunk)
                if total and not half and done / total >= 0.5:
                    half = True
                    ui.toast(f"Halfway: {name}")
        return name


runner = Runner()
