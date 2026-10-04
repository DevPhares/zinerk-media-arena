import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from . import auth, downloader, folders, jobs, stats, ui
from .config import APP_NAME, DATA_DIR
from .db import get_connection, init_db

MAX_LOGIN_TRIES = 3


def fmt_time(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%a %d %b %Y, %I:%M %p")


def count_users():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def create_account(username):
    """Walk a new user through password + storage folder. Returns the user dict or None."""
    ui.toast(f"Setting up a new account for {username}")

    while True:
        pw = ui.ask_text("Create password", f"At least {auth.MIN_PASSWORD_LEN} characters", password=True)
        if pw is None:
            return None
        if len(pw) < auth.MIN_PASSWORD_LEN:
            ui.toast("Password too short, try again")
            continue
        again = ui.ask_text("Confirm password", "Type it again", password=True)
        if again is None:
            return None
        if again != pw:
            ui.toast("Passwords didn't match, try again")
            continue
        break

    default = str(folders.default_root(username))
    while True:
        answer = ui.ask_text("Storage folder", f"Blank = {default}")
        chosen = answer.strip() if answer else default
        try:
            root = folders.create_tree(chosen)
            break
        except (ValueError, OSError) as e:
            ui.toast(f"Can't use that folder: {e}")
            if chosen == default:
                return None

    role = "admin" if count_users() == 0 else "user"
    try:
        auth.create_user(username, pw, str(root), role)
    except ValueError as e:
        ui.toast(str(e))
        return None
    return auth.login(username, pw)


def login_flow():
    """Ask for a username, then log in or create an account. Returns the user or None."""
    username = ui.ask_text("ZINERK login", "Username")
    username = (username or "").strip()
    if not username:
        return None

    if auth.username_exists(username):
        for attempt in range(MAX_LOGIN_TRIES):
            pw = ui.ask_text(f"Password for {username}", "Password", password=True)
            if pw is None:
                return None
            user = auth.login(username, pw)
            if user:
                return user
            ui.toast(f"Wrong password ({MAX_LOGIN_TRIES - attempt - 1} tries left)")
        return None

    if not auth.USERNAME_RE.match(username):
        ui.toast("Username must be 3-32 letters, numbers or _")
        return None
    if not ui.confirm("New user", f"No account named '{username}'. Create it?"):
        return None
    return create_account(username)


def welcome(user):
    if user["previous_login"]:
        ui.toast(f"Welcome back, {user['username']}! Last visit: {fmt_time(user['previous_login'])}")
    else:
        ui.toast(f"Welcome, {user['username']}!")


def resume_pending(user):
    """Restart this user's unfinished downloads (only theirs, never anyone else's)."""
    ids = jobs.resumable_jobs(user["id"])
    for job_id in ids:
        downloader.runner.start(user, job_id)
    if ids:
        ui.toast(f"Resuming {len(ids)} unfinished download(s)")


# ---------- saved folder paths (per user) ----------

def get_saved_paths(user_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = 'saved_paths'",
            (user_id,),
        ).fetchone()
        if row and row["value"]:
            return json.loads(row["value"])
        return []
    except json.JSONDecodeError:
        return []
    finally:
        conn.close()


def remember_path(user_id, path):
    paths = get_saved_paths(user_id)
    if path in paths:
        return
    paths.append(path)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?, 'saved_paths', ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, json.dumps(paths)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- folder picking (used by downloads AND report export) ----------

def tree_paths(tree, prefix=""):
    """List the end folders of the folder tree, e.g. 'Audio/mp3', 'Video/mkv'."""
    found = []
    for name, sub in tree.items():
        if name.startswith("."):  # hide .incomplete and .thumbnails
            continue
        rel = f"{prefix}{name}"
        if sub:
            found += tree_paths(sub, rel + "/")
        else:
            found.append(rel)
    return found


def ask_new_path(user):
    """Ask for a new folder path. Returns a Path (and remembers it), or None when done."""
    while True:
        answer = ui.ask_text("New folder", "Full path, e.g. /storage/emulated/0/Music (blank = done)")
        path = (answer or "").strip()
        if not path:
            return None
        p = Path(path).expanduser()
        if not p.is_absolute():
            ui.toast("Path must start with /")
            continue
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            ui.toast(f"Can't use that folder: {e}")
            continue
        remember_path(user["id"], str(p))
        return p


def ensure_root(user):
    """Make sure the user's folder tree exists. Returns the root Path, or None."""
    try:
        return folders.create_tree(user["root_folder"])
    except (ValueError, OSError) as e:
        ui.toast(f"Your storage folder has a problem: {e}")
        return None


def pick_folders(user, root, default, title, default_label):
    """Default folder, or a checklist of folders. Returns a list of Paths, or None."""
    mode = ui.choose(title, [default_label, "Choose folders (checklist)"])
    if mode is None:
        return None
    if mode == 0:
        return [default]

    entries = []  # (label, path) for every folder you can tick
    for rel in sorted(tree_paths(folders.TREE)):
        entries.append((rel, root / rel))
    for saved in get_saved_paths(user["id"]):
        entries.append((f"Saved: {saved}", Path(saved)))

    labels = [label.replace(",", " ") for label, _ in entries]  # commas would split the list
    labels.append("+ Add a new path")

    picked = ui.checklist("Tick the folders", labels)
    if picked is None:
        return None

    dests = []
    add_new = False
    for i in picked:
        if i == len(entries):
            add_new = True
        elif entries[i][1] not in dests:
            dests.append(entries[i][1])

    if add_new:
        while True:
            p = ask_new_path(user)
            if p is None:
                break
            if p not in dests:
                dests.append(p)

    if not dests:
        ui.toast("No folder chosen")
        return None
    return dests


# ---------- download flow ----------

def default_destination(root, rtype, fmt):
    if rtype == "image_slides":
        return root / "Images" / "image_slides"
    if rtype == "photo":
        return root / "Images" / "photos"
    return folders.destination_for(root, rtype, fmt)


def pick_destinations(user, rtype, fmt):
    root = ensure_root(user)
    if root is None:
        return None
    default = default_destination(root, rtype, fmt)
    return pick_folders(user, root, default, "Save to", "Default folder only")


def download_flow(user):
    raw = ui.ask_text("Download", "Paste the link")
    if raw is None:
        return
    url = downloader.normalize_url(raw)
    if url is None:
        ui.toast("That doesn't look like a valid http/https link")
        return

    guess = downloader.detect(url)
    if guess:
        ui.toast(f"Detected: {guess['label']}")
    else:
        ui.toast("Error: Resource URL can't be determined at the moment")

    labels = [label for _, label in downloader.RESOURCE_TYPES]
    i = ui.choose("What are you downloading?", labels)
    if i is None:
        return
    rtype = downloader.RESOURCE_TYPES[i][0]

    fmt = None
    if rtype in downloader.FORMATS:
        options = downloader.FORMATS[rtype]
        j = ui.choose("Format", options)
        if j is None:
            return
        fmt = options[j]

    dests = pick_destinations(user, rtype, fmt)
    if not dests:
        return

    # Saved to the database BEFORE anything downloads, so a kill can't lose it.
    job_id = jobs.create_job(
        user["id"], url, guess["platform"] if guess else None,
        rtype, fmt, downloader.TOOL_FOR[rtype], dests,
    )
    downloader.runner.start(user, job_id)


# ---------- stats flow ----------

def verify_identity(title, accept):
    """Ask for username + password (3 tries). accept(user) decides if they're allowed."""
    for attempt in range(MAX_LOGIN_TRIES):
        username = ui.ask_text(title, "Username")
        if username is None:
            return None
        pw = ui.ask_text(title, "Password", password=True)
        if pw is None:
            return None
        found = auth.check_credentials(username.strip(), pw)
        if found and accept(found):
            return found
        ui.toast(f"Wrong details ({MAX_LOGIN_TRIES - attempt - 1} tries left)")
    return None


def export_report(user, data, all_users, viewer):
    root = ensure_root(user)
    if root is None:
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    kind = "admin_report" if all_users else "report"
    filename = f"ZINERK_{kind}_{user['username']}_{stamp}.pdf"

    default = root / "Documents" / "pdf"
    dests = pick_folders(user, root, default, "Save report to", "Default (Documents/pdf)")
    if not dests:
        return

    ui.toast("Building your PDF report...")
    try:
        pdf_bytes = stats.build_pdf(data, all_users, viewer)
    except Exception as e:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "error.log").write_text(f"report\n{traceback.format_exc()}")
        ui.toast(f"Could not build the report: {str(e)[:80]}")
        return

    saved = []
    for folder in dests:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / filename
            target.write_bytes(pdf_bytes)
            saved.append(target)
        except OSError as e:
            ui.toast(f"Could not save to {folder}: {e}")
    if saved:
        ui.toast(f"Report saved: {saved[0]}")


def stats_flow(user):
    who = ui.choose("Stats", ["Normal user (my stats)", "Admin (all users)"])
    if who is None:
        return

    if who == 0:
        found = verify_identity("Verify your account", lambda u: u["id"] == user["id"])
        if found is None:
            return
        data = stats.collect(user["id"])
        all_users = False
    else:
        found = verify_identity("Admin login", lambda u: u["role"] == "admin")
        if found is None:
            return
        data = stats.collect_all()
        all_users = True

    ui.toast(stats.summary_line(data))
    export_report(user, data, all_users, found["username"])


# ---------- menus ----------

def menu(user):
    """The Download / Stats / Exit loop. Returns when the user confirms Exit."""
    while True:
        ui.show_notification(
            APP_NAME, f"Logged in as {user['username']}",
            [("Download", "download"), ("Stats", "stats"), ("Exit", "exit")],
        )
        action = ui.wait_for_action()

        if action == "download":
            download_flow(user)
        elif action == "stats":
            stats_flow(user)
        elif action == "exit":
            if ui.confirm("Exit", "Quit ZINERK Media Arena?"):
                downloader.runner.stop_all()
                downloader.runner.wait_idle(8)
                ui.toast("Progress saved. It will resume next time you launch.")
                return
            ui.toast("phew, almost exited ZINERK Media Arena")


def main():
    init_db()
    if not ui.api_available():
        print("Termux:API isn't responding. Install the Termux:API app and allow notifications.")
        sys.exit(1)

    ui.clear_pending_action()
    try:
        while True:
            ui.show_notification(APP_NAME, "Tap Run to start", [("Run", "run"), ("Exit", "exit")])
            action = ui.wait_for_action()

            if action == "exit":
                return
            if action == "run":
                user = login_flow()
                if user is None:
                    continue
                welcome(user)
                resume_pending(user)
                menu(user)
                return
    finally:
        ui.clear_notification()


if __name__ == "__main__":
    main()
