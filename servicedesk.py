"""IT Service Desk Ticketing and Employee Onboarding/Offboarding Management System (SDLM).

One small web app (Python standard library + SQLite) that replaces manual
tracking of:
  * support tickets   - priority and deadline (SLA) are set automatically
  * onboarding        - one form creates the username, email and full checklist
  * offboarding       - one form creates the leaver checklist

Run:  python servicedesk.py      then open  http://localhost:8080
Note: it tracks the work; it does not connect to Active Directory or Microsoft 365.
"""
import csv, html, io, json, re, secrets, sqlite3, string, sys, threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_FILE = "servicedesk.db"
STATUSES = ["Open", "In Progress", "Resolved", "Closed"]
REVEAL = {}   # one-time password reveal: token -> password (memory only, never saved to disk)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL, requester TEXT NOT NULL, category TEXT NOT NULL,
    priority TEXT NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Open',
    description TEXT, resolved TEXT, employee_id INTEGER);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
    ts TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL, department TEXT NOT NULL,
    job_title TEXT, manager TEXT, username TEXT UNIQUE, email TEXT, date TEXT,
    status TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, owner TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0, done_at TEXT);
"""

def now():
    return datetime.now(timezone.utc).replace(microsecond=0)

def iso(dt):
    return dt.isoformat()

def parse(ts):
    return datetime.fromisoformat(ts)

def get_db(path=DB_FILE):
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db

# ----------------------------------------------------------------- tickets
def create_ticket(db, cfg, requester, category, description, priority=None, employee_id=None):
    if category not in cfg["categories"]:
        category = "Other"
    priority = priority if priority in cfg["sla_hours"] else cfg["categories"][category]
    created = now()
    due = created + timedelta(hours=cfg["sla_hours"][priority])
    cur = db.execute(
        "INSERT INTO tickets(created, requester, category, priority, due, description, employee_id) VALUES (?,?,?,?,?,?,?)",
        (iso(created), requester.strip(), category, priority, iso(due), description.strip(), employee_id))
    db.commit()
    return cur.lastrowid

def set_status(db, ticket_id, status):
    if status not in STATUSES:
        raise ValueError("bad status")
    resolved = iso(now()) if status in ("Resolved", "Closed") else None
    db.execute("UPDATE tickets SET status=?, resolved=? WHERE id=?", (status, resolved, ticket_id))
    db.commit()

def add_note(db, ticket_id, text):
    if text.strip():
        db.execute("INSERT INTO notes(ticket_id, ts, text) VALUES (?,?,?)", (ticket_id, iso(now()), text.strip()))
        db.commit()

def is_breached(row):
    if row["status"] in ("Resolved", "Closed"):
        return bool(row["resolved"]) and parse(row["resolved"]) > parse(row["due"])
    return now() > parse(row["due"])

# --------------------------------------------------------------- employees
def make_username(db, full_name):
    parts = re.sub(r"[^a-zA-Z ]", "", full_name).lower().split()
    if len(parts) < 2:
        raise ValueError("Enter first name and surname")
    base = parts[0][0] + parts[-1]
    name, n = base, 1
    while db.execute("SELECT 1 FROM employees WHERE username=?", (name,)).fetchone():
        n += 1
        name = f"{base}{n}"
    return name

def temp_password(length=12):
    chars = string.ascii_letters + string.digits + "!@#$%"
    while True:
        pw = "".join(secrets.choice(chars) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%" for c in pw)):
            return pw

def onboard(db, cfg, full_name, department, job_title, manager, start_date):
    username = make_username(db, full_name)
    email = f"{username}@{cfg['email_domain']}"
    cur = db.execute(
        "INSERT INTO employees(full_name, department, job_title, manager, username, email, date, status, created) VALUES (?,?,?,?,?,?,?,?,?)",
        (full_name.strip(), department, job_title.strip(), manager.strip(), username, email, start_date, "Onboarding", iso(now())))
    emp_id = cur.lastrowid
    items = list(cfg["onboarding_common"]) + list(cfg["onboarding_by_department"].get(department, []))
    for title, owner in items:
        db.execute("INSERT INTO tasks(employee_id, kind, title, owner) VALUES (?,?,?,?)", (emp_id, "onboarding", title, owner))
    db.commit()
    create_ticket(db, cfg, "HR / " + (manager.strip() or "Manager"), "Access request",
                  f"New starter setup for {full_name.strip()} ({department}, {job_title.strip()}), starting {start_date}. "
                  f"Username {username}. See onboarding checklist.", priority="Medium", employee_id=emp_id)
    return emp_id, username, email, temp_password()   # password shown once, never stored

def offboard(db, cfg, emp_id, last_day):
    emp = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not emp or emp["status"] in ("Offboarding", "Offboarded"):
        return False
    db.execute("UPDATE employees SET status='Offboarding', date=? WHERE id=?", (last_day, emp_id))
    for title, owner in cfg["offboarding"]:
        db.execute("INSERT INTO tasks(employee_id, kind, title, owner) VALUES (?,?,?,?)", (emp_id, "offboarding", title, owner))
    db.commit()
    create_ticket(db, cfg, "HR", "Access request",
                  f"Leaver: {emp['full_name']} ({emp['department']}), last day {last_day}. "
                  f"Disable account '{emp['username']}' and recover equipment. See offboarding checklist.",
                  priority="High", employee_id=emp_id)
    return True

def toggle_task(db, task_id):
    t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        return
    done = 0 if t["done"] else 1
    db.execute("UPDATE tasks SET done=?, done_at=? WHERE id=?", (done, iso(now()) if done else None, task_id))
    refresh_employee_status(db, t["employee_id"])
    db.commit()

def refresh_employee_status(db, emp_id):
    """When every task of the current process is done, move the employee on."""
    emp = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    kind = "offboarding" if emp["status"] in ("Offboarding", "Offboarded") else "onboarding"
    tasks = db.execute("SELECT done FROM tasks WHERE employee_id=? AND kind=?", (emp_id, kind)).fetchall()
    all_done = bool(tasks) and all(t["done"] for t in tasks)
    if kind == "onboarding":
        new = "Active" if all_done else "Onboarding"
    else:
        new = "Offboarded" if all_done else "Offboarding"
    db.execute("UPDATE employees SET status=? WHERE id=?", (new, emp_id))

def progress(db, emp_id, kind):
    rows = db.execute("SELECT done FROM tasks WHERE employee_id=? AND kind=?", (emp_id, kind)).fetchall()
    return (sum(r["done"] for r in rows), len(rows))

# -------------------------------------------------------------------- HTML
CSS = """body{font-family:Arial,sans-serif;margin:0;background:#f5f7fa;color:#222}
header{background:#1f3a5f;color:#fff;padding:12px 24px}header a{color:#fff;margin-right:18px;text-decoration:none;font-weight:bold}
main{padding:20px 24px;max-width:1100px;margin:auto}h1{margin-top:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.card{background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 3px #0002}.card b{font-size:26px;display:block}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #ddd;padding:7px 9px;text-align:left;font-size:14px}
th{background:#1f3a5f;color:#fff}form.box{background:#fff;padding:16px;border-radius:8px;box-shadow:0 1px 3px #0002;max-width:560px}
label{display:block;margin:10px 0 3px;font-weight:bold;font-size:13px}input,select,textarea{width:100%;padding:8px;box-sizing:border-box;font-size:14px}
button{background:#2f6fed;color:#fff;border:0;padding:9px 16px;border-radius:5px;margin-top:12px;cursor:pointer;font-size:14px}
.tag{padding:2px 8px;border-radius:10px;font-size:12px;color:#fff}.Critical{background:#c0392b}.High{background:#e67e22}.Medium{background:#2f6fed}.Low{background:#7f8c8d}
.breach{color:#c0392b;font-weight:bold}.ok{color:#2e9e5b}.bar{background:#ddd;border-radius:6px;height:12px;width:220px}.bar i{display:block;background:#2e9e5b;height:12px;border-radius:6px}
.notice{background:#fff8e1;border-left:5px solid #f1c40f;padding:12px;margin:12px 0}.done{text-decoration:line-through;color:#888}"""

def e(x):
    return html.escape("" if x is None else str(x))

def page(cfg, title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(title)}</title><style>{CSS}</style></head><body>"
            f"<header><a href='/'>Dashboard</a><a href='/tickets'>Tickets</a><a href='/tickets/new'>New ticket</a>"
            f"<a href='/employees'>Employees</a><a href='/onboard'>Onboard</a><a href='/offboard'>Offboard</a>"
            f"<a href='/export/tickets.csv'>Export CSV</a></header><main><h1>{e(title)}</h1>{body}</main></body></html>")

def fmt(ts):
    return parse(ts).astimezone().strftime("%Y-%m-%d %H:%M") if ts else ""

def view_dashboard(db, cfg):
    t = db.execute("SELECT * FROM tickets").fetchall()
    open_t = [r for r in t if r["status"] in ("Open", "In Progress")]
    breached = [r for r in open_t if is_breached(r)]
    emps = db.execute("SELECT status, COUNT(*) c FROM employees GROUP BY status").fetchall()
    ec = {r["status"]: r["c"] for r in emps}
    cards = (f"<div class='cards'><div class='card'><b>{len(open_t)}</b>Open tickets</div>"
             f"<div class='card'><b class='breach'>{len(breached)}</b>Past deadline (SLA)</div>"
             f"<div class='card'><b>{sum(1 for r in t if r['status'] in ('Resolved','Closed'))}</b>Resolved</div>"
             f"<div class='card'><b>{ec.get('Onboarding', 0)}</b>Onboarding now</div>"
             f"<div class='card'><b>{ec.get('Offboarding', 0)}</b>Offboarding now</div></div>")
    rows = "".join(
        f"<tr><td><a href='/tickets/{r['id']}'>#{r['id']}</a></td><td>{e(r['requester'])}</td><td>{e(r['category'])}</td>"
        f"<td><span class='tag {r['priority']}'>{r['priority']}</span></td><td>{fmt(r['due'])}</td>"
        f"<td class='{'breach' if is_breached(r) else 'ok'}'>{'OVERDUE' if is_breached(r) else 'On time'}</td></tr>"
        for r in sorted(open_t, key=lambda r: r["due"])[:10]) or "<tr><td colspan=6>No open tickets.</td></tr>"
    return page(cfg, "IT Service Desk Dashboard", cards + "<h2>Next tickets due</h2><table><tr><th>Ticket</th><th>Requester</th><th>Category</th><th>Priority</th><th>Due</th><th>SLA</th></tr>" + rows + "</table>")

def view_tickets(db, cfg, q):
    status = q.get("status", [""])[0]
    rows = db.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    if status:
        rows = [r for r in rows if r["status"] == status]
    opts = "<option value=''>All</option>" + "".join(f"<option {'selected' if s == status else ''}>{s}</option>" for s in STATUSES)
    body = f"<form method='get'><label>Filter by status</label><select name='status' onchange='this.form.submit()'>{opts}</select></form><br>"
    body += "<table><tr><th>#</th><th>Requester</th><th>Category</th><th>Priority</th><th>Status</th><th>Due</th><th>SLA</th></tr>"
    for r in rows:
        body += (f"<tr><td><a href='/tickets/{r['id']}'>#{r['id']}</a></td><td>{e(r['requester'])}</td><td>{e(r['category'])}</td>"
                 f"<td><span class='tag {r['priority']}'>{r['priority']}</span></td><td>{r['status']}</td><td>{fmt(r['due'])}</td>"
                 f"<td class='{'breach' if is_breached(r) else 'ok'}'>{'Breached' if is_breached(r) else 'OK'}</td></tr>")
    return page(cfg, "Tickets", body + "</table>")

def view_new_ticket(cfg):
    cats = "".join(f"<option>{e(c)}</option>" for c in cfg["categories"])
    return page(cfg, "Log a new ticket",
        f"<form class='box' method='post' action='/tickets/new'><label>Your name</label><input name='requester' required>"
        f"<label>What is the problem about?</label><select name='category'>{cats}</select>"
        f"<label>Describe the problem</label><textarea name='description' rows='4' required></textarea>"
        f"<p>Priority and deadline are set automatically from the category.</p><button>Submit ticket</button></form>")

def view_ticket(db, cfg, tid):
    r = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    notes = "".join(f"<tr><td>{fmt(n['ts'])}</td><td>{e(n['text'])}</td></tr>" for n in db.execute("SELECT * FROM notes WHERE ticket_id=? ORDER BY id", (tid,))) or "<tr><td colspan=2>No notes yet.</td></tr>"
    opts = "".join(f"<option {'selected' if s == r['status'] else ''}>{s}</option>" for s in STATUSES)
    link = f"<p><a href='/employees/{r['employee_id']}'>Linked employee checklist</a></p>" if r["employee_id"] else ""
    body = (f"<p><span class='tag {r['priority']}'>{r['priority']}</span> {e(r['category'])} &middot; requested by <b>{e(r['requester'])}</b> &middot; "
            f"created {fmt(r['created'])} &middot; due <b>{fmt(r['due'])}</b> &middot; "
            f"<span class='{'breach' if is_breached(r) else 'ok'}'>{'SLA breached' if is_breached(r) else 'Within SLA'}</span></p>"
            f"<p>{e(r['description'])}</p>{link}"
            f"<form class='box' method='post' action='/tickets/{tid}/update'><label>Status</label><select name='status'>{opts}</select>"
            f"<label>Add a note</label><textarea name='note' rows='3'></textarea><button>Save</button></form>"
            f"<h2>Notes</h2><table><tr><th>When</th><th>Note</th></tr>{notes}</table>")
    return page(cfg, f"Ticket #{tid}", body)

def view_employees(db, cfg):
    rows = db.execute("SELECT * FROM employees ORDER BY id DESC").fetchall()
    body = "<table><tr><th>Name</th><th>Department</th><th>Username</th><th>Status</th><th>Progress</th><th>Date</th></tr>"
    for r in rows:
        kind = "offboarding" if r["status"] in ("Offboarding", "Offboarded") else "onboarding"
        d, n = progress(db, r["id"], kind)
        body += (f"<tr><td><a href='/employees/{r['id']}'>{e(r['full_name'])}</a></td><td>{e(r['department'])}</td><td>{e(r['username'])}</td>"
                 f"<td>{r['status']}</td><td>{d}/{n} {kind} tasks</td><td>{e(r['date'])}</td></tr>")
    return page(cfg, "Employees", body + "</table>" if rows else "<p>No employees yet. Use Onboard to add one.</p>")

def view_employee(db, cfg, eid, secret=None):
    r = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not r:
        return None
    body = f"<p><b>{e(r['full_name'])}</b> &middot; {e(r['job_title'])} &middot; {e(r['department'])} &middot; Manager: {e(r['manager'])}<br>Username: <b>{e(r['username'])}</b> &middot; Email: <b>{e(r['email'])}</b> &middot; Status: <b>{r['status']}</b></p>"
    if secret:
        body += (f"<div class='notice'><b>Temporary password (shown once, not stored):</b> <code>{e(secret)}</code><br>"
                 f"Give it to the employee securely and force a password change at first sign-in.</div>")
    for kind in ("onboarding", "offboarding"):
        tasks = db.execute("SELECT * FROM tasks WHERE employee_id=? AND kind=? ORDER BY id", (eid, kind)).fetchall()
        if not tasks:
            continue
        d, n = progress(db, eid, kind)
        body += f"<h2>{kind.title()} checklist ({d}/{n})</h2><div class='bar'><i style='width:{d*100//n}%'></i></div><br><table><tr><th>Done</th><th>Task</th><th>Owner</th><th>Completed</th></tr>"
        for t in tasks:
            body += (f"<tr><td><form method='post' action='/tasks/{t['id']}/toggle' style='margin:0'><button style='margin:0;padding:3px 10px;background:{'#2e9e5b' if t['done'] else '#999'}'>{'Done' if t['done'] else 'Mark done'}</button></form></td>"
                     f"<td class='{'done' if t['done'] else ''}'>{e(t['title'])}</td><td>{e(t['owner'])}</td><td>{fmt(t['done_at'])}</td></tr>")
        body += "</table>"
    return page(cfg, r["full_name"], body)

def view_onboard(cfg):
    deps = "".join(f"<option>{e(d)}</option>" for d in cfg["departments"])
    return page(cfg, "Onboard a new employee",
        f"<form class='box' method='post' action='/onboard'><label>Full name</label><input name='full_name' required placeholder='e.g. Thandi Mokoena'>"
        f"<label>Department</label><select name='department'>{deps}</select><label>Job title</label><input name='job_title' required>"
        f"<label>Manager</label><input name='manager' required><label>Start date</label><input type='date' name='start_date' required>"
        f"<p>This creates the username, email address and the full checklist, and opens a setup ticket automatically.</p><button>Start onboarding</button></form>")

def view_offboard(db, cfg):
    emps = db.execute("SELECT * FROM employees WHERE status IN ('Active','Onboarding') ORDER BY full_name").fetchall()
    if not emps:
        return page(cfg, "Offboard an employee", "<p>No active employees to offboard.</p>")
    opts = "".join(f"<option value='{r['id']}'>{e(r['full_name'])} ({e(r['department'])})</option>" for r in emps)
    return page(cfg, "Offboard an employee",
        f"<form class='box' method='post' action='/offboard'><label>Employee leaving</label><select name='emp_id'>{opts}</select>"
        f"<label>Last working day</label><input type='date' name='last_day' required>"
        f"<p>This creates the leaver checklist and opens a high-priority ticket automatically.</p><button>Start offboarding</button></form>")

def tickets_csv(db):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "created", "requester", "category", "priority", "status", "due", "resolved", "sla_breached", "description"])
    for r in db.execute("SELECT * FROM tickets ORDER BY id"):
        w.writerow([r["id"], r["created"], r["requester"], r["category"], r["priority"], r["status"], r["due"], r["resolved"] or "", is_breached(r), r["description"]])
    return out.getvalue()

# ------------------------------------------------------------------ server
def make_handler(db, cfg, lock):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, status=200, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, where):
            self.send_response(303)
            self.send_header("Location", where)
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path.rstrip("/") or "/", parse_qs(u.query)
            with lock:
                m = re.fullmatch(r"/tickets/(\d+)", p)
                m2 = re.fullmatch(r"/employees/(\d+)", p)
                if p == "/": return self.send(view_dashboard(db, cfg))
                if p == "/tickets": return self.send(view_tickets(db, cfg, q))
                if p == "/tickets/new": return self.send(view_new_ticket(cfg))
                if m:
                    pg = view_ticket(db, cfg, int(m.group(1)))
                    return self.send(pg or page(cfg, "Not found", "No such ticket."), 200 if pg else 404)
                if p == "/employees": return self.send(view_employees(db, cfg))
                if m2:
                    pg = view_employee(db, cfg, int(m2.group(1)), REVEAL.pop(q.get("reveal", [""])[0], None))
                    return self.send(pg or page(cfg, "Not found", "No such employee."), 200 if pg else 404)
                if p == "/onboard": return self.send(view_onboard(cfg))
                if p == "/offboard": return self.send(view_offboard(db, cfg))
                if p == "/export/tickets.csv":
                    return self.send(tickets_csv(db), ctype="text/csv", headers={"Content-Disposition": "attachment; filename=tickets.csv"})
            self.send(page(cfg, "Not found", "Page not found."), 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8")).items()}
            p = urlparse(self.path).path
            with lock:
                try:
                    if p == "/tickets/new":
                        tid = create_ticket(db, cfg, f["requester"], f["category"], f["description"])
                        return self.redirect(f"/tickets/{tid}")
                    m = re.fullmatch(r"/tickets/(\d+)/update", p)
                    if m:
                        tid = int(m.group(1))
                        set_status(db, tid, f["status"])
                        add_note(db, tid, f.get("note", ""))
                        return self.redirect(f"/tickets/{tid}")
                    if p == "/onboard":
                        eid, user, mail, pw = onboard(db, cfg, f["full_name"], f["department"], f["job_title"], f["manager"], f["start_date"])
                        token = secrets.token_urlsafe(16)
                        REVEAL[token] = pw
                        return self.redirect(f"/employees/{eid}?reveal={token}")
                    if p == "/offboard":
                        offboard(db, cfg, int(f["emp_id"]), f["last_day"])
                        return self.redirect(f"/employees/{f['emp_id']}")
                    m = re.fullmatch(r"/tasks/(\d+)/toggle", p)
                    if m:
                        t = db.execute("SELECT employee_id FROM tasks WHERE id=?", (int(m.group(1)),)).fetchone()
                        toggle_task(db, int(m.group(1)))
                        return self.redirect(f"/employees/{t['employee_id']}" if t else "/employees")
                except (KeyError, ValueError) as ex:
                    return self.send(page(cfg, "Please check the form", f"<p>{e(ex)}</p><p><a href='javascript:history.back()'>Go back</a></p>"), 400)
            self.send(page(cfg, "Not found", "Page not found."), 404)
    return H

def serve(db, cfg, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db, cfg, threading.Lock()))
    return srv

if __name__ == "__main__":
    cfg = json.load(open("config.json"))
    db = get_db()
    srv = serve(db, cfg, cfg["port"])
    print(f"Service desk running at http://localhost:{cfg['port']}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
