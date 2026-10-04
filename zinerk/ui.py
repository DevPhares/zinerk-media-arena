import json
import shlex
import shutil
import subprocess
import time

from .config import APP_NAME, DATA_DIR

NOTIF_ID = "zinerk-main"
ACTION_FILE = DATA_DIR / "ui_action"


def _run(cmd, timeout=10):
    """Run a termux-* command. Returns its output, or None if it failed or hung."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def api_available():
    """True if the Termux:API app is installed and responding."""
    if shutil.which("termux-toast") is None:
        return False
    return _run(["termux-toast", "-s", f"{APP_NAME} ready"], timeout=8) is not None


# ---------- toasts ----------

def toast(message, short=False):
    cmd = ["termux-toast"]
    if short:
        cmd.append("-s")
    cmd.append(message)
    _run(cmd)


# ---------- dialogs ----------

def _dialog(kind, title, extra=None):
    """Show a dialog and wait. Returns the answer dict, or None if dismissed."""
    cmd = ["termux-dialog", kind, "-t", title] + (extra or [])
    out = _run(cmd, timeout=None)  # no timeout: the user takes as long as they like
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if data.get("code") != -1:  # -1 means OK was pressed; anything else is cancel
        return None
    return data


def ask_text(title, hint="", password=False):
    """Text box. Returns the typed string, or None if cancelled."""
    extra = ["-i", hint]
    if password:
        extra.append("-p")
    data = _dialog("text", title, extra)
    return None if data is None else data.get("text", "")


def confirm(title, message):
    """Yes/No box. Returns True only if Yes was tapped."""
    out = _run(["termux-dialog", "confirm", "-t", title, "-i", message], timeout=None)
    if not out:
        return False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False
    return str(data.get("text", "")).strip().lower() == "yes"


def choose(title, options):
    """Pick one option (options must not contain commas). Returns its index, or None."""
    out = _run(["termux-dialog", "radio", "-t", title, "-v", ",".join(options)],
               timeout=None)
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    index = data.get("index")
    if data.get("code") == -2 or not isinstance(index, int) or not (0 <= index < len(options)):
        return None
    return index
# ---------- persistent notification ----------

def show_notification(title, content, buttons=()):
    """buttons: up to 3 of (label, action_name). Tapping one writes its name to ACTION_FILE."""
    cmd = [
        "termux-notification", "--id", NOTIF_ID,
        "--title", title, "--content", content,
        "--ongoing", "--alert-once", "--priority", "low",
    ]
    for i, (label, action) in enumerate(list(buttons)[:3], start=1):
        write = f"echo {shlex.quote(action)} > {shlex.quote(str(ACTION_FILE))}"
        cmd += [f"--button{i}", label, f"--button{i}-action", write]
    _run(cmd)


def clear_notification():
    _run(["termux-notification-remove", NOTIF_ID])


def clear_pending_action():
    ACTION_FILE.unlink(missing_ok=True)


def wait_for_action(timeout=None):
    """Wait until a notification button is tapped. Returns its action name, or None on timeout."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start = time.time()
    while True:
        if ACTION_FILE.exists():
            action = ACTION_FILE.read_text().strip()
            if action:
                ACTION_FILE.unlink(missing_ok=True)
                return action
        if timeout is not None and time.time() - start > timeout:
            return None
        time.sleep(0.4)


if __name__ == "__main__":
    if not api_available():
        print("Termux:API isn't responding. Install the Termux:API app "
              "(same source as Termux) and allow its notifications.")
        raise SystemExit(1)

    clear_pending_action()
    show_notification(APP_NAME, "Tap Run to start", [("Run", "run"), ("Exit", "exit")])
    print("Notification shown. Pull down your notification shade and tap a button (60s)...")

    action = wait_for_action(60)
    print("You tapped:", action)

    if action == "run":
        name = ask_text("Login", "Username")
        print("Typed username:", name)
        pw = ask_text("Login", "Password", password=True)
        print("Password length:", None if pw is None else len(pw))
        ok = confirm("Exit?", "Quit ZINERK Media Arena?")
        print("Confirm:", ok)
        toast("Progress saved" if ok else "phew, almost exited ZINERK Media Arena")

    clear_notification()
