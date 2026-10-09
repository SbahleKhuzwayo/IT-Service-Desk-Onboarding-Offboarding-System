import json, os, sys, tempfile, threading, unittest, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import servicedesk as sd

CFG = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")))

class Logic(unittest.TestCase):
    def setUp(self):
        self.db = sd.get_db(os.path.join(tempfile.mkdtemp(), "t.db"))

    def test_priority_and_deadline_set_automatically(self):
        tid = sd.create_ticket(self.db, CFG, "Anele", "Server / system down", "Mail server is down")
        r = self.db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        self.assertEqual(r["priority"], "Critical")
        hours = (sd.parse(r["due"]) - sd.parse(r["created"])).total_seconds() / 3600
        self.assertEqual(hours, 4)

    def test_password_reset_is_low_priority_72h(self):
        tid = sd.create_ticket(self.db, CFG, "Anele", "Password reset", "Forgot password")
        r = self.db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        self.assertEqual(r["priority"], "Low")

    def test_breach_detection(self):
        tid = sd.create_ticket(self.db, CFG, "A", "Other", "x")
        self.db.execute("UPDATE tickets SET due=? WHERE id=?", (sd.iso(sd.now() - sd.timedelta(hours=1)), tid))
        self.assertTrue(sd.is_breached(self.db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()))
        sd.set_status(self.db, tid, "Resolved")
        self.assertTrue(sd.is_breached(self.db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()))

    def test_onboarding_creates_username_email_tasks_and_ticket(self):
        eid, user, mail, pw = sd.onboard(self.db, CFG, "Thandi Mokoena", "Sales", "Sales Rep", "J. Smith", "2026-11-01")
        self.assertEqual(user, "tmokoena")
        self.assertEqual(mail, "tmokoena@democompany.co.za")
        n = self.db.execute("SELECT COUNT(*) FROM tasks WHERE employee_id=?", (eid,)).fetchone()[0]
        self.assertEqual(n, len(CFG["onboarding_common"]) + len(CFG["onboarding_by_department"]["Sales"]))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tickets WHERE employee_id=?", (eid,)).fetchone()[0], 1)
        self.assertGreaterEqual(len(pw), 12)

    def test_duplicate_usernames_get_a_number(self):
        _, u1, _, _ = sd.onboard(self.db, CFG, "Thandi Mokoena", "IT", "x", "m", "2026-11-01")
        _, u2, _, _ = sd.onboard(self.db, CFG, "Themba Mokoena", "IT", "x", "m", "2026-11-01")
        self.assertEqual((u1, u2), ("tmokoena", "tmokoena2"))

    def test_full_lifecycle(self):
        eid, *_ = sd.onboard(self.db, CFG, "Sipho Dlamini", "IT", "Technician", "m", "2026-11-01")
        for t in self.db.execute("SELECT id FROM tasks WHERE employee_id=?", (eid,)).fetchall():
            sd.toggle_task(self.db, t["id"])
        self.assertEqual(self.db.execute("SELECT status FROM employees WHERE id=?", (eid,)).fetchone()[0], "Active")
        self.assertTrue(sd.offboard(self.db, CFG, eid, "2027-01-31"))
        self.assertEqual(self.db.execute("SELECT status FROM employees WHERE id=?", (eid,)).fetchone()[0], "Offboarding")
        for t in self.db.execute("SELECT id FROM tasks WHERE employee_id=? AND kind='offboarding'", (eid,)).fetchall():
            sd.toggle_task(self.db, t["id"])
        self.assertEqual(self.db.execute("SELECT status FROM employees WHERE id=?", (eid,)).fetchone()[0], "Offboarded")

    def test_bad_name_rejected(self):
        with self.assertRaises(ValueError):
            sd.onboard(self.db, CFG, "Madonna", "IT", "x", "m", "2026-11-01")

class Web(unittest.TestCase):
    def test_pages_and_forms(self):
        db = sd.get_db(os.path.join(tempfile.mkdtemp(), "w.db"))
        srv = sd.serve(db, CFG, 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            for path in ["/", "/tickets", "/tickets/new", "/employees", "/onboard", "/export/tickets.csv"]:
                self.assertEqual(urllib.request.urlopen(base + path).status, 200, path)
            data = urllib.parse.urlencode({"requester": "Anele", "category": "Network / Wi-Fi", "description": "Wi-Fi is down"}).encode()
            r = urllib.request.urlopen(urllib.request.Request(base + "/tickets/new", data=data))
            self.assertIn("Ticket #1", r.read().decode())
            self.assertEqual(db.execute("SELECT priority FROM tickets WHERE id=1").fetchone()[0], "High")
            data = urllib.parse.urlencode({"full_name": "Thandi Mokoena", "department": "IT", "job_title": "Tech", "manager": "M", "start_date": "2026-11-01"}).encode()
            page1 = urllib.request.urlopen(urllib.request.Request(base + "/onboard", data=data))
            body, url = page1.read().decode(), page1.geturl()
            self.assertIn("Temporary password", body)                 # shown once
            self.assertNotIn("pw=", url)                                # never placed in the URL
            self.assertNotIn("Temporary password", urllib.request.urlopen(url).read().decode())  # gone on reload
        finally:
            srv.shutdown(); srv.server_close()

if __name__ == "__main__":
    unittest.main()
