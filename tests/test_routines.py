# -*- coding: utf-8 -*-
"""^_tick — Job-Ordner, Werkzeuge und Rolle."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, routines, tool_registry
from core import code_tools, code_loop, recurring, tasks


class RoutinesFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = config.ROUTINES_ROOT
        config.ROUTINES_ROOT = os.path.join(self.tmp.name, "Routinen")
        routines.ensure_layout()

    def tearDown(self):
        config.ROUTINES_ROOT = self.old_root
        self.tmp.cleanup()

    def test_layout_has_all_boxes(self):
        for box in ("Daily", "Inbox", "Blocked", "Done", "Log"):
            self.assertTrue(os.path.isdir(os.path.join(routines.root(), box)), box)

    def test_write_read_list_job(self):
        out = routines.write_job("Inbox", "Vault-Eingang", "JOB: Vault-Eingang\nZiel: kopieren\n")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["path"].endswith("Inbox/Vault-Eingang.md"))
        jobs = routines.list_jobs("Inbox")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["head"], "JOB: Vault-Eingang")
        read = routines.read_job("Vault-Eingang")
        self.assertTrue(read["ok"])
        self.assertIn("Ziel: kopieren", read["body"])

    def test_no_overwrite_and_source_kept(self):
        first = routines.write_job("Inbox", "Job", "JOB: eins\n")
        second = routines.write_job("Inbox", "Job", "JOB: zwei\n")
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        with open(first["path"], encoding="utf-8") as f:
            self.assertIn("JOB: eins", f.read())

    def test_password_and_missing_name_are_refused(self):
        self.assertFalse(routines.write_job("Inbox", "Passwörter", "x")["ok"])
        self.assertFalse(routines.write_job("Inbox", "", "x")["ok"])
        self.assertFalse(routines.write_job("Done", "Neu", "x")["ok"])

    def test_move_to_done_stamps_date_and_removes_source(self):
        src = routines.write_job("Inbox", "Auftrag", "JOB: Auftrag\n")
        moved = routines.move_job("Auftrag", "Done")
        self.assertTrue(moved["ok"], moved)
        self.assertRegex(moved["name"], r"^\d{4}-\d{2}-\d{2}_Auftrag\.md$")
        self.assertFalse(os.path.exists(src["path"]))
        self.assertTrue(os.path.isfile(moved["path"]))

    def test_move_to_blocked_needs_one_question(self):
        routines.write_job("Inbox", "Frage", "JOB: Frage\n")
        self.assertFalse(routines.move_job("Frage", "Blocked")["ok"])
        moved = routines.move_job("Frage", "Blocked", question="Welcher Unterordner")
        self.assertTrue(moved["ok"], moved)
        with open(moved["path"], encoding="utf-8") as f:
            body = f.read()
        self.assertIn("> **Offene Frage:** Welcher Unterordner?", body)
        self.assertFalse(os.path.exists(os.path.join(routines.root(), "Inbox", "Frage.md")))

    def test_move_collision_keeps_source(self):
        routines.write_job("Inbox", "Doppelt", "JOB: neu\n")
        done = os.path.join(routines.root(), "Done")
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d")
        existing = os.path.join(done, f"{today}_Doppelt.md")
        with open(existing, "w", encoding="utf-8") as f:
            f.write("alt\n")
        moved = routines.move_job("Doppelt", "Done")
        self.assertFalse(moved["ok"])
        with open(existing, encoding="utf-8") as f:
            self.assertEqual(f.read(), "alt\n")
        self.assertTrue(os.path.isfile(os.path.join(routines.root(), "Inbox", "Doppelt.md")))

    def test_append_log_creates_then_appends(self):
        first = routines.append_log(["## Job A", "Status: erledigt"])
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["created"])
        second = routines.append_log("## Job B\nStatus: blockiert")
        self.assertTrue(second["ok"])
        self.assertFalse(second["created"])
        self.assertEqual(first["path"], second["path"])
        self.assertRegex(os.path.basename(first["path"]), r"^\d{4}-\d{2}-\d{2}_\d{4}\.md$")
        with open(first["path"], encoding="utf-8") as f:
            body = f.read()
        self.assertIn("Job A", body)
        self.assertIn("Job B", body)

    def test_plan_overview_reads_recurring_store(self):
        old_store = recurring.STORE_PATH
        old_events = recurring.EVENTS_PATH
        old_tasks = tasks.STORE_PATH
        recurring.STORE_PATH = os.path.join(self.tmp.name, "recurring.json")
        recurring.EVENTS_PATH = os.path.join(self.tmp.name, "events.jsonl")
        tasks.STORE_PATH = os.path.join(self.tmp.name, "tasks.json")
        try:
            recurring.create_item({
                "title": "Vault-Eingang",
                "prompt": "Scans einsammeln",
                "schedule": {"kind": "daily", "time": "07:30"},
                "pass": "Alle neuen Dateien kopiert oder als Duplikat vermerkt",
            })
            view = routines.plan_overview()
            self.assertTrue(view["ok"])
            titles = [i["title"] for i in view["recurring"]]
            self.assertIn("Vault-Eingang", titles)
            fresh = [i for i in view["recurring"] if i["title"] == "Vault-Eingang"][0]
            self.assertEqual(fresh["schedule"]["time"], "07:30")
            self.assertFalse(fresh["paused"])
            self.assertEqual(view["tasks"], [])
        finally:
            recurring.STORE_PATH = old_store
            recurring.EVENTS_PATH = old_events
            tasks.STORE_PATH = old_tasks

    def test_create_routine_needs_pass(self):
        old_store = recurring.STORE_PATH
        old_events = recurring.EVENTS_PATH
        recurring.STORE_PATH = os.path.join(self.tmp.name, "recurring.json")
        recurring.EVENTS_PATH = os.path.join(self.tmp.name, "events.jsonl")
        try:
            bad = routines.create_routine({
                "title": "Ohne Kriterium",
                "prompt": "irgendwas",
                "schedule": {"kind": "daily", "time": "08:00"},
            })
            self.assertFalse(bad["ok"])
            good = routines.create_routine({
                "title": "Mit Kriterium",
                "prompt": "irgendwas",
                "schedule": {"kind": "daily", "time": "08:00"},
                "pass": "Zeile im Log",
            })
            self.assertTrue(good["ok"], good)
        finally:
            recurring.STORE_PATH = old_store
            recurring.EVENTS_PATH = old_events


class RoutineOverviewTests(unittest.TestCase):
    """Der Überblick, den ^_tick (und die UI) als Zahlen liest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = config.ROUTINES_ROOT
        self.old_store = recurring.STORE_PATH
        self.old_events = recurring.EVENTS_PATH
        self.old_tasks = tasks.STORE_PATH
        config.ROUTINES_ROOT = os.path.join(self.tmp.name, "Routinen")
        recurring.STORE_PATH = os.path.join(self.tmp.name, "recurring.json")
        recurring.EVENTS_PATH = os.path.join(self.tmp.name, "events.jsonl")
        tasks.STORE_PATH = os.path.join(self.tmp.name, "tasks.json")
        routines.ensure_layout()

    def tearDown(self):
        config.ROUTINES_ROOT = self.old_root
        recurring.STORE_PATH = self.old_store
        recurring.EVENTS_PATH = self.old_events
        tasks.STORE_PATH = self.old_tasks
        self.tmp.cleanup()

    def test_counts_boxes_and_runs(self):
        routines.write_job("Daily", "Vault-Eingang", "JOB: Vault-Eingang\n")
        routines.write_job("Inbox", "Auftrag", "JOB: Auftrag\n")
        routines.append_log(["## Lauf"])
        recurring.create_item({
            "id": "td-a",
            "title": "Aktiv",
            "prompt": "tun",
            "schedule": {"kind": "daily", "time": "07:00"},
            "pass": "Zeile",
        })
        recurring.create_item({
            "id": "td-b",
            "title": "Pausiert",
            "prompt": "tun",
            "schedule": {"kind": "daily", "time": "08:00"},
            "paused": True,
        })
        overview = routines.overview()
        self.assertTrue(overview["ok"])
        counts = overview["counts"]
        titles = [t["title"] for t in overview["todos"]]
        self.assertIn("Aktiv", titles)
        self.assertIn("Pausiert", titles)
        self.assertEqual(counts["todos_total"], counts["todos_active"] + counts["todos_paused"])
        self.assertGreaterEqual(counts["todos_paused"], 1)
        self.assertEqual(
            [t["paused"] for t in overview["todos"] if t["title"] == "Aktiv"], [False]
        )
        self.assertEqual(counts["jobs_daily"], 1)
        self.assertEqual(counts["jobs_inbox"], 1)
        self.assertEqual(counts["jobs_blocked"], 0)
        self.assertEqual(counts["tasks_open"], 0)
        self.assertEqual(
            overview["job_boxes"]["Daily"]["files"][0]["head"], "JOB: Vault-Eingang"
        )
        self.assertIn("jobs_root", overview)

    def test_tool_returns_overview(self):
        out = tool_registry.execute("RoutineOverview", {}, mode="tick")
        self.assertTrue(out["ok"], out)
        self.assertIn("counts", out["result"])


class TickToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = config.ROUTINES_ROOT
        config.ROUTINES_ROOT = os.path.join(self.tmp.name, "Routinen")
        routines.ensure_layout()

    def tearDown(self):
        config.ROUTINES_ROOT = self.old_root
        self.tmp.cleanup()

    def test_tick_mode_exposes_routine_tools(self):
        names = {t["name"] for t in tool_registry.tools_for_mode("tick")}
        for expected in (
            "RoutineOverview", "RoutineJobs", "RoutineRead", "RoutineJobWrite",
            "RoutineMove", "RoutineLog", "RoutinePlan", "RoutineSet", "RoutineRun",
            "ReadFile", "RunCommand",
        ):
            self.assertIn(expected, names)
        self.assertEqual(tool_registry.tool_map("tick"), tool_registry.CODE_TOOL_MAP)

    def test_execute_routine_tools_through_registry(self):
        written = tool_registry.execute(
            "RoutineJobWrite",
            {"box": "Daily", "name": "Vault-Eingang", "body": "JOB: Vault-Eingang\n"},
            confirm=lambda *_a: True,
            mode="tick",
        )
        self.assertTrue(written["ok"], written)
        listed = tool_registry.execute("RoutineJobs", {}, mode="tick")
        self.assertTrue(listed["ok"])
        self.assertEqual([j["name"] for j in listed["result"]["jobs"]], ["Vault-Eingang.md"])
        logged = tool_registry.execute(
            "RoutineLog", {"lines": ["Status: erledigt"]}, mode="tick"
        )
        self.assertTrue(logged["ok"], logged)

    def test_permission_policy(self):
        self.assertEqual(code_tools.permission_decision("RoutineOverview", {})["action"], "allow")
        self.assertEqual(code_tools.permission_decision("RoutineJobs", {})["action"], "allow")
        self.assertEqual(code_tools.permission_decision("RoutineLog", {"lines": ["x"]})["action"], "allow")
        self.assertEqual(code_tools.permission_decision("RoutineMove", {"name": "a", "to": "Done"})["action"], "allow")
        self.assertEqual(code_tools.permission_decision("RoutineRun", {"id": "x"})["action"], "requires_grant")
        self.assertEqual(code_tools.permission_decision("RoutineSet", {"title": "x"})["action"], "requires_grant")

    def test_tick_role_carries_folder_and_rules(self):
        role = code_loop.tick_role()
        self.assertIn("^_tick", role)
        self.assertIn("Daily/", role)
        self.assertIn("RoutineLog", role)
        self.assertIn("Passwörter", role)
        self.assertIn("keine Aufträge", role)
        self.assertNotIn("Du bist ^_Code: ein Code-Agent", role)
        self.assertIn("Profil ^_tick", role)

    def test_tick_is_a_known_task_head(self):
        self.assertIn("_tick", tasks.HEADS)

    def test_shared_contract_snippet_is_head_aware(self):
        agents = os.path.expanduser("~/.glyph/AGENTS.md")
        if not os.path.isfile(agents):
            self.skipTest("~/.glyph/AGENTS.md fehlt")
        snippet = code_loop._shared_contract_snippet(head="tick")
        self.assertIn("§Tick", snippet)
        self.assertIn("## Tick (^_tick)", snippet)
        code_snippet = code_loop._shared_contract_snippet(head="code")
        self.assertIn("§Code", code_snippet)


if __name__ == "__main__":
    unittest.main()
