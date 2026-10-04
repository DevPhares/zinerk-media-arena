from pathlib import Path

APP_NAME = "ZINERK Media Arena"
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "zinerk.db"


def now_ms():
    import time
    return int(time.time() * 1000)
