from collections import defaultdict
from datetime import datetime

from fpdf import FPDF
from fpdf.fonts import FontFace

from .config import DATA_DIR
from .db import get_connection

NAVY = (25, 55, 105)
GREY = (110, 110, 110)
WHITE = (255, 255, 255)
HEAD = FontFace(emphasis="BOLD", color=WHITE, fill_color=NAVY)


# ---------- reading the data ----------

def _fetch(conn, user_id):
    """user_id=None means every account. Only collect_all() may pass None."""
    if user_id is None:
        scope, params = "", ()
    else:
        scope, params = "WHERE j.user_id = ?", (user_id,)

    jobs = [dict(r) for r in conn.execute(
        "SELECT j.*, u.username FROM jobs j JOIN users u ON u.id = j.user_id "
        f"{scope} ORDER BY j.created_at DESC", params)]

    dests = defaultdict(list)
    for r in conn.execute(
            "SELECT d.job_id, d.path FROM job_destinations d "
            f"JOIN jobs j ON j.id = d.job_id {scope}", params):
        dests[r["job_id"]].append(r["path"])

    bw_scope = "" if user_id is None else "WHERE b.user_id = ?"
    bandwidth = [dict(r) for r in conn.execute(
        "SELECT b.day, b.tool, b.bytes, u.username FROM bandwidth_daily b "
        "JOIN users u ON u.id = b.user_id "
        f"{bw_scope} ORDER BY b.day DESC, b.tool", params)]

    u_scope = "" if user_id is None else "WHERE id = ?"
    users = [dict(r) for r in conn.execute(
        "SELECT id, username, role, created_at, last_login, root_folder "
        f"FROM users {u_scope} ORDER BY id", params)]

    return {"jobs": jobs, "dests": dests, "bandwidth": bandwidth, "users": users}


def collect(user_id):
    """One account's data. Everything is filtered to this user_id."""
    conn = get_connection()
    try:
        return _fetch(conn, user_id)
    finally:
        conn.close()


def collect_all():
    """Every account's data. Call this only after the admin password was checked."""
    conn = get_connection()
    try:
        return _fetch(conn, None)
    finally:
        conn.close()


# ---------- formatting helpers ----------

def _t(value):
    """The built-in PDF fonts only know Latin-1. Other characters become '?'."""
    text = "" if value is None else str(value)
    return text.encode("latin-1", "replace").decode("latin-1")


def wrap_text(text, width):
    text = str(text or "")
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_dur(ms):
    if not ms:
        return "-"
    if ms < 1000:
        return f"{int(ms)} ms"
    seconds = int(ms / 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def fmt_full(ms):
    """Day, date, time to the millisecond, AM/PM, year."""
    if not ms:
        return "-"
    dt = datetime.fromtimestamp(ms / 1000)
    return dt.strftime("%a %d %b %Y %I:%M:%S") + f".{ms % 1000:03d} " + dt.strftime("%p")


def fmt_day(ms):
    if not ms:
        return "never"
    return datetime.fromtimestamp(ms / 1000).strftime("%a %d %b %Y %I:%M %p")


# ---------- the numbers ----------

def _summarise(jobs):
    s = {
        "total": len(jobs), "status": defaultdict(int), "bytes": 0, "ms": 0,
        "tool": defaultdict(lambda: [0, 0, 0]),
        "type": defaultdict(lambda: [0, 0, 0]),
        "platform": defaultdict(lambda: [0, 0, 0]),
        "hours": [[0, 0] for _ in range(24)],
    }
    for j in jobs:
        size = j["bytes_downloaded"] or 0
        took = j["elapsed_ms"] or 0
        s["status"][j["status"]] += 1
        s["bytes"] += size
        s["ms"] += took
        groups = (("tool", j["tool"] or "unknown"),
                  ("type", j["resource_type"]),
                  ("platform", j["platform"] or "unknown"))
        for key, name in groups:
            row = s[key][name]
            row[0] += 1
            row[1] += size
            row[2] += took
        if j["started_at"]:
            hour = datetime.fromtimestamp(j["started_at"] / 1000).hour
            s["hours"][hour][0] += 1
            s["hours"][hour][1] += took
    return s


def summary_line(data):
    s = _summarise(data["jobs"])
    return (f"{s['total']} downloads, {fmt_bytes(s['bytes'])}, "
            f"{s['status'].get('done', 0)} done, {s['status'].get('failed', 0)} failed")


def _url_activity(jobs):
    urls = {}
    for j in jobs:
        u = urls.setdefault(j["url"], {
            "users": set(), "count": 0, "tools": set(),
            "first": j["created_at"], "last": j["created_at"]})
        u["users"].add(j["username"])
        u["count"] += 1
        u["tools"].add(j["tool"] or "unknown")
        u["first"] = min(u["first"], j["created_at"])
        u["last"] = max(u["last"], j["created_at"])
    return sorted(urls.items(), key=lambda kv: (-len(kv[1]["users"]), -kv[1]["count"]))


# ---------- the PDF ----------

class Report(FPDF):
    def __init__(self, heading):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.heading = heading
        self.set_margins(10, 16, 10)
        self.set_auto_page_break(True, 15)
        self.alias_nb_pages()

    def header(self):
        self.set_y(6)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 5, _t(self.heading), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(200, 200, 200)
        self.line(10, 12, 287, 12)
        self.set_y(16)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 6, f"Page {self.page_no()} of {{nb}}", align="C")


def section(pdf, text, need=45):
    if pdf.will_page_break(need):
        pdf.add_page()
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 8, _t(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)


def kv(pdf, label, value):
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(62, 6, _t(label + ":"))
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, _t(value), new_x="LMARGIN", new_y="NEXT")


def table(pdf, headers, rows, widths, aligns, size=8):
    if not rows:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*GREY)
        pdf.cell(0, 7, "Nothing recorded yet.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        return
    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(0, 0, 0)
    pdf.set_fill_color(*WHITE)  # body cells must never inherit a colour
    with pdf.table(col_widths=widths, text_align=aligns, headings_style=HEAD,
                   line_height=size * 0.55, width=sum(widths), align="LEFT") as t:
        head = t.row()
        for h in headers:
            head.cell(_t(h))
        for row in rows:
            r = t.row()
            for cell in row:
                r.cell(_t(cell))
    pdf.set_fill_color(*WHITE)
    pdf.ln(3)


def group_rows(group):
    ordered = sorted(group.items(), key=lambda kv: -kv[1][0])
    return [[name.replace("_", " "), c, fmt_bytes(b), fmt_dur(ms)]
            for name, (c, b, ms) in ordered]


def hourly_chart(pdf, hours):
    peak = max((ms for _, ms in hours), default=0)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*NAVY)
    pdf.cell(32, 6, "Hour", border="B")
    pdf.cell(22, 6, "Downloads", border="B", align="R")
    pdf.cell(30, 6, "Time used", border="B", align="R")
    pdf.cell(100, 6, "  Relative usage", border="B", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 8)
    for hour, (count, ms) in enumerate(hours):
        pdf.cell(32, 6, f"{hour:02d}:00 - {hour:02d}:59", border="B")
        pdf.cell(22, 6, str(count), border="B", align="R")
        pdf.cell(30, 6, fmt_dur(ms), border="B", align="R")
        x, y = pdf.get_x(), pdf.get_y()
        pdf.cell(100, 6, "", border="B", new_x="LMARGIN", new_y="NEXT")
        if peak and ms:
            pdf.set_fill_color(*NAVY)
            pdf.rect(x + 2, y + 1.3, 96 * ms / peak, 3.4, style="F")
            pdf.set_fill_color(*WHITE)  # put the colour back right after each bar
    pdf.set_fill_color(*WHITE)
    pdf.ln(3)


def log_rows(data, all_users):
    wrap = 46 if all_users else 58
    rows = []
    for j in data["jobs"]:
        when = "Req " + fmt_full(j["created_at"])
        if j["finished_at"]:
            when += "\nEnd " + fmt_full(j["finished_at"])

        kind = j["resource_type"].replace("_", " ")
        if j["format"]:
            kind += " / " + j["format"]

        lines = []
        if j["title"]:
            lines.append(wrap_text(j["title"][:100], wrap))
        lines.append(wrap_text(j["url"], wrap))
        for path in data["dests"].get(j["id"], []):
            lines.append(wrap_text("-> " + path, wrap))
        if j["error"]:
            lines.append(wrap_text("Error: " + j["error"][:120], wrap))

        row = [f"#{j['id']}", when]
        if all_users:
            row.append(j["username"])
        row += [j["platform"] or "-", kind, j["tool"] or "-", j["status"],
                fmt_bytes(j["bytes_downloaded"]), fmt_dur(j["elapsed_ms"]),
                "\n".join(lines)]
        rows.append(row)
    return rows


def build_pdf(data, all_users, viewer):
    """Returns the finished PDF as bytes."""
    jobs = data["jobs"]
    s = _summarise(jobs)
    heading = ("Administrator report - all users" if all_users
               else f"Personal download report - {viewer}")

    pdf = Report(f"ZINERK Media Arena  |  {heading}")
    pdf.set_fill_color(*WHITE)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 12, "ZINERK Media Arena", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 13)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 8, _t(heading), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    done = s["status"].get("done", 0)
    failed = s["status"].get("failed", 0)
    unfinished = s["total"] - done - failed
    created = [j["created_at"] for j in jobs]

    kv(pdf, "Generated", datetime.now().strftime("%A %d %B %Y, %I:%M:%S %p"))
    kv(pdf, "Prepared for", viewer + (" (administrator)" if all_users else ""))
    kv(pdf, "Scope", f"All accounts ({len(data['users'])})" if all_users
       else "This account only")
    if not all_users and data["users"]:
        kv(pdf, "Account created", fmt_day(data["users"][0]["created_at"]))
        kv(pdf, "Last login", fmt_day(data["users"][0]["last_login"]))
    kv(pdf, "Total downloads", str(s["total"]))
    kv(pdf, "Completed / failed / unfinished", f"{done} / {failed} / {unfinished}")
    kv(pdf, "Data downloaded", fmt_bytes(s["bytes"]))
    kv(pdf, "Total download time", fmt_dur(s["ms"]))
    kv(pdf, "First request", fmt_full(min(created)) if created else "-")
    kv(pdf, "Latest request", fmt_full(max(created)) if created else "-")

    if all_users:
        section(pdf, "Accounts overview")
        per_user = defaultdict(lambda: [0, 0])
        for j in jobs:
            per_user[j["user_id"]][0] += 1
            per_user[j["user_id"]][1] += j["bytes_downloaded"] or 0
        rows = []
        for u in data["users"]:
            count, size = per_user[u["id"]]
            rows.append([u["username"], u["role"], count, fmt_bytes(size),
                         fmt_day(u["created_at"]), fmt_day(u["last_login"]),
                         wrap_text(u["root_folder"], 42)])
        table(pdf, ["User", "Role", "Downloads", "Data", "Account created",
                    "Last login", "Storage folder"], rows,
              (35, 18, 25, 28, 50, 50, 71),
              ("LEFT", "LEFT", "RIGHT", "RIGHT", "LEFT", "LEFT", "LEFT"))

    headers = ["Downloads", "Data", "Time spent"]
    widths = (70, 30, 35, 40)
    aligns = ("LEFT", "RIGHT", "RIGHT", "RIGHT")
    section(pdf, "Usage by tool")
    table(pdf, ["Tool"] + headers, group_rows(s["tool"]), widths, aligns)
    section(pdf, "Usage by resource type")
    table(pdf, ["Type"] + headers, group_rows(s["type"]), widths, aligns)
    section(pdf, "Usage by platform")
    table(pdf, ["Platform"] + headers, group_rows(s["platform"]), widths, aligns)

    section(pdf, "Daily bandwidth per tool")
    if all_users:
        rows = [[b["username"], b["day"], b["tool"], fmt_bytes(b["bytes"])]
                for b in data["bandwidth"]]
        table(pdf, ["User", "Date", "Tool", "Data"], rows, (50, 40, 40, 40),
              ("LEFT", "LEFT", "LEFT", "RIGHT"))
    else:
        rows = [[b["day"], b["tool"], fmt_bytes(b["bytes"])] for b in data["bandwidth"]]
        table(pdf, ["Date", "Tool", "Data"], rows, (45, 45, 45),
              ("LEFT", "LEFT", "RIGHT"))

    section(pdf, "Usage across a 24-hour cycle", need=24 * 6 + 14)
    hourly_chart(pdf, s["hours"])

    if all_users:
        section(pdf, "URLs requested (how many accounts, when, which tool)")
        rows = []
        for url, info in _url_activity(jobs)[:40]:
            rows.append([wrap_text(url, 70), len(info["users"]), info["count"],
                         ", ".join(sorted(info["tools"])),
                         fmt_day(info["first"]), fmt_day(info["last"])])
        table(pdf, ["URL", "Accounts", "Requests", "Tools", "First", "Latest"], rows,
              (120, 20, 22, 35, 40, 40),
              ("LEFT", "RIGHT", "RIGHT", "LEFT", "LEFT", "LEFT"))

    section(pdf, "Download log (newest first)", need=30)
    if all_users:
        headers = ["ID", "Requested / finished", "User", "Platform", "Type", "Tool",
                   "Status", "Size", "Took", "Title, URL, saved to"]
        widths = (9, 54, 20, 22, 26, 19, 16, 18, 16, 77)
        aligns = ("LEFT", "LEFT", "LEFT", "LEFT", "LEFT", "LEFT", "LEFT",
                  "RIGHT", "RIGHT", "LEFT")
    else:
        headers = ["ID", "Requested / finished", "Platform", "Type", "Tool",
                   "Status", "Size", "Took", "Title, URL, saved to"]
        widths = (9, 54, 22, 26, 20, 17, 19, 17, 93)
        aligns = ("LEFT", "LEFT", "LEFT", "LEFT", "LEFT", "LEFT",
                  "RIGHT", "RIGHT", "LEFT")
    table(pdf, headers, log_rows(data, all_users), widths, aligns, size=7)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 6, "All times are in this phone's local time.",
             new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


if __name__ == "__main__":
    conn = get_connection()
    first = conn.execute("SELECT username FROM users ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if first is None:
        print("No accounts yet. Run the tool and create an account first.")
    else:
        out = DATA_DIR / "test_report.pdf"
        out.write_bytes(build_pdf(collect_all(), True, first["username"]))
        print("Wrote", out, f"({out.stat().st_size} bytes)")
