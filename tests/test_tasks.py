# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tasks

PAIR = {"prompt": "Enter sendet nicht", "answer": "Reproduktion offen"}


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = tasks.STORE_PATH
        tasks.STORE_PATH = os.path.join(self.tmp.name, "tasks.json")

    def tearDown(self):
        tasks.STORE_PATH = self.old_path
        self.tmp.cleanup()

    def test_handoff_keeps_selected_evidence_and_build_prompt(self):
        item = tasks.create_item({
            "title": "Composer sendet nicht",
            "source": "glyph-agent",
            "target": "grok",
            "summary": "Bitte Ursache analysieren.",
            "pass": "Ursache steht in einer Datei oder einem reproduzierbaren Schritt",
            "evidence": PAIR,
        })
        self.assertEqual(tasks.list_items()[0]["title"], "Composer sendet nicht")
        prompt = tasks.handoff_prompt(item["id"])
        self.assertIn("Enter sendet nicht", prompt)
        self.assertIn("Bitte Ursache analysieren", prompt)
        self.assertIn("Zielkopf: grok", prompt)
        self.assertIn("Fertig wenn:", prompt)

    def test_create_requires_pass(self):
        with self.assertRaisesRegex(ValueError, "Fertig-Kriterium"):
            tasks.create_item({"title": "Composer"})

    def test_create_requires_prompt_and_answer(self):
        with self.assertRaisesRegex(ValueError, "Meldung und Antwort"):
            tasks.create_item({"title": "Composer", "pass": "Composer sendet"})
        with self.assertRaisesRegex(ValueError, "Meldung und Antwort"):
            tasks.create_item({
                "title": "Composer",
                "pass": "Composer sendet",
                "evidence": {"prompt": "Enter sendet nicht", "answer": ""},
            })

    def test_target_is_optional_and_unknown_heads_are_dropped(self):
        item = tasks.create_item({"title": "Composer", "target": "", "pass": "Composer sendet", "evidence": PAIR})
        self.assertEqual(item["target"], "")
        unknown = tasks.create_item({"title": "Alt", "target": "analysis", "pass": "Alt geklärt", "evidence": PAIR})
        self.assertEqual(unknown["target"], "")
        self.assertNotIn("analysis", tasks.HEADS)
        prompt = tasks.handoff_prompt(item["id"])
        self.assertIn("noch nicht zugewiesen", prompt)

    def test_analysis_remains_a_status_not_a_head(self):
        item = tasks.create_item({"title": "Bug", "status": "analysis", "pass": "Ursache benannt", "evidence": PAIR})
        self.assertEqual(item["status"], "analysis")
        self.assertEqual(item["target"], "")

    def test_evidence_keeps_paths_not_blobs(self):
        item = tasks.create_item({
            "title": "Mit Anhang",
            "pass": "Anhang-Pfad bleibt",
            "evidence": {
                **PAIR,
                "trace": {"model": "deepseek-v4", "blob": "x" * 50, "steps": ["a", "b"]},
                "attachments": [{
                    "name": "a.png",
                    "path": "/tmp/a.png",
                    "content": "AAAA",
                    "previewUrl": "data:image/png;base64,xxxx",
                }],
            },
        })
        att = item["evidence"]["attachments"][0]
        self.assertEqual(att["name"], "a.png")
        self.assertEqual(att["path"], "/tmp/a.png")
        self.assertNotIn("content", att)
        self.assertNotIn("previewUrl", att)
        self.assertEqual(item["evidence"]["trace"]["model"], "deepseek-v4")
        self.assertNotIn("blob", item["evidence"]["trace"])

    def test_status_update_adds_event(self):
        item = tasks.create_item({"title": "Bug", "pass": "Repro steht", "evidence": PAIR})
        updated = tasks.update_item(item["id"], {"status": "ready_to_build", "by": "glyph-agent"})
        self.assertEqual(updated["status"], "ready_to_build")
        self.assertEqual(updated["events"][-1]["type"], "status")

    def test_done_requires_artifact(self):
        item = tasks.create_item({"title": "Bug", "pass": "Fix in Datei", "evidence": PAIR})
        with self.assertRaisesRegex(ValueError, "Artefakt"):
            tasks.update_item(item["id"], {"status": "done"})
        done = tasks.update_item(item["id"], {"status": "done", "artifact": "client/src/App.jsx"})
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["artifact"], "client/src/App.jsx")

    def test_delete_removes_item(self):
        item = tasks.create_item({"title": "Bug", "pass": "Weg aus dem Plan", "evidence": PAIR})
        self.assertTrue(tasks.delete_item(item["id"]))
        self.assertIsNone(tasks.get_item(item["id"]))
        self.assertEqual(tasks.list_items(), [])
        self.assertFalse(tasks.delete_item(item["id"]))

    def test_old_items_without_pass_still_load(self):
        data = {
            "version": 1,
            "items": [{"id": "old1", "title": "Alt", "status": "new", "target": "", "source": "grok"}],
        }
        with open(tasks.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
        loaded = tasks.get_item("old1")
        self.assertEqual(loaded["title"], "Alt")
        self.assertEqual(loaded["pass"], "")

    def test_handoff_includes_id_and_finish_tool(self):
        item = tasks.create_item({
            "title": "Composer sendet nicht",
            "pass": "Ursache steht",
            "evidence": PAIR,
        })
        prompt = tasks.handoff_prompt(item["id"])
        self.assertIn(f"Id: {item['id']}", prompt)
        self.assertIn("TaskFinish", prompt)
        self.assertIn("nicht neu", prompt.lower())

    def test_finish_item_writes_artifact_and_marks_done(self):
        item = tasks.create_item({
            "title": "Loop-Walk",
            "pass": "Ursache in Datei",
            "evidence": PAIR,
        })
        done = tasks.finish_item(
            item["id"],
            content="# Befund\nReadFile-Walk war die Bremse.",
            summary="Walk gestoppt, Artefakt da.",
            by="_code",
        )
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["artifact"].endswith(f"{item['id']}.md"))
        self.assertIn("Walk gestoppt", done["summary"])
        path = os.path.join(tasks.artifact_dir(), f"{item['id']}.md")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("ReadFile-Walk war die Bremse", body)

    def test_finish_item_requires_content(self):
        item = tasks.create_item({
            "title": "Leer",
            "pass": "Inhalt steht",
            "evidence": PAIR,
        })
        with self.assertRaisesRegex(ValueError, "Artefakt"):
            tasks.finish_item(item["id"], content="   ")

    def test_code_tool_taskfinish_via_registry(self):
        from core import tool_registry
        item = tasks.create_item({
            "title": "Via Tool",
            "pass": "Artefakt da",
            "evidence": PAIR,
        })
        out = tool_registry.execute(
            "TaskFinish",
            {"id": item["id"], "content": "# ok\nUrsache steht.", "summary": "Ursache steht."},
            confirm=lambda *_: True,
            mode="code",
        )
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out["result"]["status"], "done")
        self.assertEqual(tasks.get_item(item["id"])["status"], "done")

    def test_old_items_without_pair_still_load(self):
        data = {
            "version": 1,
            "items": [{"id": "old2", "title": "Alt", "status": "new", "pass": "Pfad steht"}],
        }
        with open(tasks.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
        loaded = tasks.get_item("old2")
        self.assertEqual(loaded["title"], "Alt")
        self.assertEqual(loaded["evidence"]["prompt"], "")
        self.assertEqual(loaded["evidence"]["answer"], "")


if __name__ == "__main__":
    unittest.main()
