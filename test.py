import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app


def good(**changes):
    plan = {"id": "plan-0001", "title": "Bridge day", "activity_id": "A01", "slot_start": "2026-11-10T09:00",
            "duration_min": 40, "learners": ["L01", "L02"]}
    plan.update(changes)
    return plan


class Rules(unittest.TestCase):
    def test_T01_valid_plan_accepted(self):
        self.assertEqual(app.validate_plan(good()), {})

    def test_T02_title_rules(self):
        self.assertIn("title", app.validate_plan(good(title="   ")))
        self.assertIn("title", app.validate_plan(good(title="x" * 61)))
        self.assertIn("title", app.validate_plan(good(title=None)))

    def test_T03_learner_names_rejected(self):
        for bad in (["Maria"], ["L09"], ["L01", "L01"], [], "L01", ["l01"]):
            self.assertIn("learners", app.validate_plan(good(learners=bad)), bad)
        self.assertIn("Do not type learner names", app.validate_plan(good(learners=["Maria"]))["learners"])

    def test_T04_duration_slot_activity_rules(self):
        for d in (9, 181, "40", None, True, 40.5):
            self.assertIn("duration_min", app.validate_plan(good(duration_min=d)), d)
        for s in ("", "2026-13-40T09:00", "2026-11-10 09:00", None, "2026-02-30T09:00"):
            self.assertIn("slot_start", app.validate_plan(good(slot_start=s)), s)
        self.assertIn("activity_id", app.validate_plan(good(activity_id="ZZ")))

    def test_T05_overlap_is_found_with_free_time(self):
        clash = app.find_clash([good()], good(id="plan-0002", slot_start="2026-11-10T09:30"))
        self.assertEqual(clash["free_from"], "2026-11-10T09:40")

    def test_T06_back_to_back_is_not_a_clash(self):
        self.assertIsNone(app.find_clash([good()], good(id="plan-0002", slot_start="2026-11-10T09:40")))

    def test_T24_filters_work_together(self):
        ids = lambda **f: [a["id"] for a in app.filter_activities(app.ACTIVITIES, **f)]
        self.assertEqual(len(ids()), 7)
        self.assertEqual(ids(offline_only=True), ["A01", "A02", "A03", "A04", "A06", "A07"])
        self.assertEqual(ids(level="Grade 3-5", topic="Science", max_minutes=30), ["A06"])
        self.assertEqual(ids(topic="Maths", materials="None", max_minutes="20"), ["A07"])
        self.assertEqual(ids(topic="Coding", offline_only=True), [])

    def test_T25_attendance_rules(self):
        ok = {"L01": {"present": True, "level": "High"}, "L02": {"present": False, "level": "Low"}}
        self.assertEqual(app.validate_plan(good(attendance=ok)), {})
        for bad in ({"L05": {"present": True, "level": "High"}},
                    {"L01": {"present": "yes", "level": "High"}},
                    {"L01": {"present": True, "level": "Great"}},
                    {"L01": "present"}, ["L01"]):
            self.assertIn("attendance", app.validate_plan(good(attendance=bad)), bad)


class Service(unittest.TestCase):
    def setUp(self):
        self.db = app.Database(":memory:")
        app.ensure_demo_users(self.db)
        self.uid = self.db.get_user("demo01")["id"]

    def test_T07_last_saved_wins_and_resend_is_idempotent(self):
        app.save_plan(self.db, self.uid, good(saved_at="2026-11-01T10:00:00"))
        self.assertEqual(app.save_plan(self.db, self.uid, good(saved_at="2026-11-01T10:00:00"))[1]["status"], "synced")
        self.assertEqual(len(self.db.plans_for_user(self.uid)), 1)
        self.assertEqual(app.save_plan(self.db, self.uid, good(title="Newer", saved_at="2026-11-01T11:00:00"))[0], 200)
        self.assertEqual(app.save_plan(self.db, self.uid, good(title="Older", saved_at="2026-11-01T09:00:00"))[1]["status"], "ignored_older")
        self.assertEqual(self.db.plans_for_user(self.uid)[0]["title"], "Newer")

    def test_T07b_editing_own_plan_is_not_a_self_clash(self):
        app.save_plan(self.db, self.uid, good(saved_at="2026-11-01T10:00:00"))
        self.assertEqual(app.save_plan(self.db, self.uid, good(duration_min=60, saved_at="2026-11-01T10:05:00"))[0], 200)

    def test_T07c_clash_is_refused_and_not_stored(self):
        app.save_plan(self.db, self.uid, good())
        code, body = app.save_plan(self.db, self.uid, good(id="plan-0002", slot_start="2026-11-10T09:30"))
        self.assertEqual((code, body["clash"]["free_from"]), (409, "2026-11-10T09:40"))
        self.assertEqual(len(self.db.plans_for_user(self.uid)), 1)

    def test_T26_only_offline_capable_activities_can_be_saved(self):
        self.assertEqual(app.save_activity(self.db, self.uid, "A01")[0], 200)
        self.assertEqual(app.save_activity(self.db, self.uid, "A01")[0], 200)
        self.assertEqual(app.save_activity(self.db, self.uid, "A05")[0], 422)
        self.assertEqual(app.save_activity(self.db, self.uid, "ZZ")[0], 404)
        self.assertEqual([a["id"] for a in app.saved_activities(self.db, self.uid)[1]], ["A01"])


def call(base, path, method="GET", body=None, token=None, raw=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def J(response):
    return response[0], json.loads(response[1])


def plan(**changes):
    p = {"id": "plan-0001", "title": "Bridge day", "activity_id": "A01", "slot_start": "2026-11-10T09:00",
         "duration_min": 40, "learners": ["L01", "L02"]}
    p.update(changes)
    return p


class ServerCase(unittest.TestCase):
    db_path = ":memory:"

    def start(self, path=None):
        self.server = app.create_server(path or self.db_path, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.server.db.close()

    def setUp(self): self.start()
    def tearDown(self): self.stop()

    def login(self, fid="demo01", pw="demo-pass-1"):
        code, body = J(call(self.base, "/api/login", "POST", {"facilitator_id": fid, "password": pw}))
        return code, body.get("token")


class Api(ServerCase):
    def test_T08_login_accepts_valid_rejects_wrong(self):
        code, token = self.login(); self.assertEqual(code, 200); self.assertTrue(token)
        code, body = J(call(self.base, "/api/login", "POST", {"facilitator_id": "demo01", "password": "nope"}))
        self.assertEqual(code, 401); self.assertIn("wrong", body["error"])
        self.assertEqual(self.login("ghost", "x")[0], 401)

    def test_T09_data_endpoints_need_login(self):
        for path in ("/api/plans", "/api/activities", "/api/saved", "/api/export.csv"):
            self.assertEqual(call(self.base, path)[0], 401, path)
        self.assertEqual(call(self.base, "/api/plans", "POST", plan())[0], 401)
        self.assertEqual(call(self.base, "/api/saved", "POST", {"activity_id": "A01"})[0], 401)
        self.assertEqual(call(self.base, "/api/plans", token="forged")[0], 401)

    def test_T10_end_to_end_slice(self):
        _, t = self.login()
        self.assertEqual(J(call(self.base, "/api/plans", "POST", plan(), t))[1]["status"], "synced")
        code, plans = J(call(self.base, "/api/plans", token=t))
        self.assertEqual((code, len(plans), plans[0]["title"], plans[0]["learners"]), (200, 1, "Bridge day", ["L01", "L02"]))
        self.assertEqual(call(self.base, "/")[0], 200)

    def test_T12_validation_errors_store_nothing(self):
        _, t = self.login()
        code, body = J(call(self.base, "/api/plans", "POST", plan(title="", learners=["Maria"], duration_min=5), t))
        self.assertEqual(code, 422); self.assertEqual(set(body["errors"]), {"title", "learners", "duration_min"})
        self.assertEqual(J(call(self.base, "/api/plans", token=t))[1], [])

    def test_T13_clash_over_http(self):
        _, t = self.login(); call(self.base, "/api/plans", "POST", plan(), t)
        code, body = J(call(self.base, "/api/plans", "POST", plan(id="plan-0002", slot_start="2026-11-10T09:10"), t))
        self.assertEqual(code, 409); self.assertEqual(body["clash"]["free_from"], "2026-11-10T09:40")
        self.assertEqual(len(J(call(self.base, "/api/plans", token=t))[1]), 1)

    def test_T14_facilitators_cannot_see_or_overwrite_each_others_plans(self):
        _, t1 = self.login(); _, t2 = self.login("demo02", "demo-pass-2")
        call(self.base, "/api/plans", "POST", plan(), t1)
        self.assertEqual(J(call(self.base, "/api/plans", token=t2))[1], [])
        self.assertEqual(call(self.base, "/api/plans", "POST", plan(title="Hijack"), t2)[0], 403)

    def test_T15_small_messages_and_size_limit(self):
        self.assertLess(len(json.dumps(plan()).encode()), 2048)
        _, t = self.login()
        for a in J(call(self.base, "/api/activities", token=t))[1]:
            self.assertLess(len(json.dumps(a).encode()), 5120)
        self.assertEqual(call(self.base, "/api/plans", "POST", raw=b'{"title":"' + b"x" * 5000 + b'"}', token=t)[0], 413)

    def test_T16_malformed_input_does_not_crash_server(self):
        _, t = self.login()
        for raw in (b"{not json", b"\xff\xfe", b"[]", b'"str"', b"123"):
            self.assertIn(call(self.base, "/api/plans", "POST", raw=raw, token=t)[0], (400, 413), raw)
        self.assertEqual(call(self.base, "/api/login", "POST", raw=b"[]")[0], 400)
        self.assertEqual(call(self.base, "/api/plans", token=t)[0], 200)

    def test_T17_csv_export_neutralises_formulas(self):
        _, t = self.login(); call(self.base, "/api/plans", "POST", plan(title="=HYPERLINK(1)"), t)
        code, body = call(self.base, "/api/export.csv", token=t)
        text = body.decode()
        self.assertEqual(code, 200); self.assertTrue(text.startswith("id,title")); self.assertIn("\"'=HYPERLINK(1)\"", text); self.assertIn("L01 L02", text)

    def test_T18_logout_invalidates_token(self):
        _, t = self.login(); self.assertEqual(call(self.base, "/api/logout", "POST", {}, t)[0], 200)
        self.assertEqual(call(self.base, "/api/plans", token=t)[0], 401)

    def test_T27_activity_search_over_http(self):
        _, t = self.login()
        search = lambda q: [a["id"] for a in J(call(self.base, "/api/activities?" + q, token=t))[1]]
        self.assertEqual(len(search("")), 7)
        self.assertEqual(search("topic=Science&max_minutes=30"), ["A06"])
        self.assertEqual(search("offline_only=1&level=Grade%207-9"), ["A04"])
        self.assertEqual(call(self.base, "/api/activities?max_minutes=abc", token=t)[0], 422)

    def test_T28_saved_activities_are_per_user_and_offline_only(self):
        _, t1 = self.login(); _, t2 = self.login("demo02", "demo-pass-2")
        self.assertEqual(J(call(self.base, "/api/saved", "POST", {"activity_id": "A02"}, t1))[0], 200)
        self.assertEqual(J(call(self.base, "/api/saved", "POST", {"activity_id": "A05"}, t1))[0], 422)
        self.assertEqual([a["id"] for a in J(call(self.base, "/api/saved", token=t1))[1]], ["A02"])
        self.assertEqual(J(call(self.base, "/api/saved", token=t2))[1], [])

    def test_T29_attendance_round_trip_and_rejection(self):
        _, t = self.login()
        att = {"L01": {"present": True, "level": "High"}, "L02": {"present": False, "level": "Low"}}
        self.assertEqual(call(self.base, "/api/plans", "POST", plan(attendance=att), t)[0], 200)
        self.assertEqual(J(call(self.base, "/api/plans", token=t))[1][0]["attendance"], att)
        code, body = J(call(self.base, "/api/plans", "POST", plan(attendance={"L08": {"present": True, "level": "High"}}), t))
        self.assertEqual((code, list(body["errors"])), (422, ["attendance"]))

    def test_T30_static_files_are_safe(self):
        for path in ("/app.js", "/style.css", "/sw.js"):
            self.assertEqual(call(self.base, path)[0], 200, path)
        for path in ("/../app.py", "/js/../../app.py", "/%2e%2e/app.py", "/nothing.js"):
            self.assertEqual(call(self.base, path)[0], 404, path)


class Persistence(unittest.TestCase):
    def test_T11_data_survives_restart_and_retry_is_safe(self):
        path = os.path.join(tempfile.mkdtemp(), "test.db")
        first = ServerCase(); first.start(path); _, t = first.login()
        call(first.base, "/api/plans", "POST", plan(), t); call(first.base, "/api/saved", "POST", {"activity_id": "A03"}, t); first.stop()
        second = ServerCase(); second.start(path); _, t2 = second.login()
        self.assertEqual([p["title"] for p in J(call(second.base, "/api/plans", token=t2))[1]], ["Bridge day"])
        self.assertEqual([a["id"] for a in J(call(second.base, "/api/saved", token=t2))[1]], ["A03"])
        self.assertEqual(J(call(second.base, "/api/plans", "POST", plan(), t2))[0], 200)
        self.assertEqual(len(J(call(second.base, "/api/plans", token=t2))[1]), 1)
        second.stop()


ROOT = Path(__file__).resolve().parent; WEB = ROOT / "web"

class P(HTMLParser):
    def __init__(s):
        super().__init__(); s.tags = []; s.fors = set(); s.ids = {}; s.inlabel = 0; s.title = ""; s.intitle = False
    def handle_starttag(s, tag, a):
        a = dict(a); s.tags.append((tag, a))
        if a.get("id"): s.ids[a["id"]] = (tag, a)
        if tag == "label" and a.get("for"): s.fors.add(a["for"])
        if tag == "title": s.intitle = True
    def handle_endtag(s, t):
        if t == "title": s.intitle = False
    def handle_data(s, d):
        if s.intitle: s.title += d

def lum(h):
    h = h.lstrip("#"); h = "".join(c * 2 for c in h) if len(h) == 3 else h
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True); return (la + 0.05) / (lb + 0.05)

class Quality(unittest.TestCase):
    def setUp(self):
        self.p = P(); self.p.feed((WEB / "index.html").read_text())

    def test_T19_html_basics_for_wcag(self):
        html = (WEB / "index.html").read_text()
        self.assertIn('<html lang="en">', html)
        self.assertTrue(self.p.title.strip())
        self.assertIn("width=device-width", html)
        self.assertTrue(any(a.get("class") == "skip" and a.get("href") == "#main" for t, a in self.p.tags))
        for tag, a in self.p.tags:
            if tag in ("input", "select") and a.get("type") != "hidden":
                self.assertIn(a.get("id"), self.p.fors, f"{tag} {a} has no <label for>")
        self.assertTrue(any(a.get("aria-live") == "polite" for t, a in self.p.tags))
        self.assertTrue(any(a.get("role") == "alert" for t, a in self.p.tags))
        self.assertIn("<caption>", html); self.assertIn('scope="col"', html); self.assertIn("<legend>", html)
        self.assertEqual(len(self.p.ids), len([1 for _ in self.p.ids]))
        for t, a in self.p.tags:
            if t == "img": self.assertIn("alt", a)

    def test_T20_text_contrast_at_least_4_5(self):
        css = (WEB / "style.css").read_text()
        v = dict(re.findall(r"--(\w+):(#[0-9a-fA-F]{3,6})", css))
        pairs = {"body text": (v["fg"], v["bg"]), "muted text": (v["muted"], v["bg"]), "error text": (v["err"], v["bg"]),
                 "button text": ("#ffffff", v["accent"]), "accent text on white": (v["accent"], "#ffffff")}
        out = {k: round(ratio(*x), 2) for k, x in pairs.items()}
        print("\ncontrast ratios:", out)
        for k, r in out.items(): self.assertGreaterEqual(r, 4.5, k)
        self.assertGreaterEqual(ratio(v["line"], v["bg"]), 3.0)

    def test_T21_touch_targets_and_focus_style_present(self):
        css = (WEB / "style.css").read_text()
        self.assertIn("min-height:44px", css); self.assertIn(":focus-visible", css)
        self.assertNotRegex(css, r"outline:\s*(none|0)")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_T22_client_js_parses(self):
        for f in [WEB / "app.js", WEB / "sw.js"]:
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr}")

    def test_T23_no_secrets_or_real_learner_data_in_source(self):
        bad = re.compile(r"(api[_-]?key\s*[=:]|secret\w*\s*[=:]\s*['\"]|BEGIN (RSA|PRIVATE)|AKIA[0-9A-Z]{12})", re.I)
        for f in [ROOT / "app.py"] + list(WEB.rglob("*")):
            if f.is_file() and f.suffix in (".py", ".js", ".html", ".json", ".css"):
                self.assertIsNone(bad.search(f.read_text()), f)
        self.assertNotIn("innerHTML", (WEB / "app.js").read_text())


if __name__ == "__main__":
    unittest.main()
