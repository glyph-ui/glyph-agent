# -*- coding: utf-8 -*-
"""Workspaces Kabelsalat (Phase 2): attach / mode / detach / empty-registry."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from core import bind_store, code_tools, config


class WorkspacesRegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.store = os.path.join(self.root, "workspaces.json")
        self.ws_a = os.path.realpath(os.path.join(self.root, "proj-a"))
        self.ws_b = os.path.realpath(os.path.join(self.root, "proj-b"))
        os.makedirs(self.ws_a)
        os.makedirs(self.ws_b)

        import core.workspaces_registry as wr

        self.wr = wr
        self._orig_dir = wr.GLYPH_DIR
        self._orig_store = wr.USER_STORE
        self._old_reg = getattr(config, "CODE_WORKSPACES_USE_REGISTRY", True)
        self._old_roots = list(config.CODE_WORKSPACE_ROOTS)
        wr.GLYPH_DIR = self.root
        wr.USER_STORE = self.store
        # Vault-Registry isolieren (accessible_roots() liest auch vr)
        import core.vaults_registry as _vr

        self._vr = _vr
        self._orig_vr_dir = _vr.GLYPH_DIR
        self._orig_vr_store = _vr.USER_STORE
        self._orig_vr_defaults = _vr.DEFAULTS_STORE
        _vr.GLYPH_DIR = self.root
        _vr.USER_STORE = os.path.join(self.root, "vaults.json")
        _vr.DEFAULTS_STORE = os.path.join(self.root, "vaults.defaults.json")
        with open(_vr.USER_STORE, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "vaults": []}, f)
        bind_store._mtime_cache.clear()
        with open(self.store, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "workspaces": []}, f)

    def tearDown(self):
        self.wr.GLYPH_DIR = self._orig_dir
        self.wr.USER_STORE = self._orig_store
        self._vr.GLYPH_DIR = self._orig_vr_dir
        self._vr.USER_STORE = self._orig_vr_store
        self._vr.DEFAULTS_STORE = self._orig_vr_defaults
        config.CODE_WORKSPACES_USE_REGISTRY = self._old_reg
        config.CODE_WORKSPACE_ROOTS = self._old_roots
        bind_store._mtime_cache.clear()
        self._tmp.cleanup()

    def test_attach_mode_primary_detach(self):
        wr = self.wr
        res = wr.attach(self.ws_a, mode="rw")
        self.assertTrue(res["workspace"]["exists"])
        self.assertEqual(res["workspace"]["mode"], "rw")
        self.assertTrue(res["workspace"]["primary"])

        res2 = wr.attach(self.ws_b, mode="r")
        self.assertEqual(res2["workspace"]["mode"], "r")
        self.assertFalse(res2["workspace"]["primary"])

        snap = wr.public_snapshot()
        self.assertEqual(len(snap["workspaces"]), 2)
        self.assertIn(self.ws_a, snap["accessible_roots"])

        wid_b = res2["workspace"]["id"]
        updated = wr.update_workspace(wid_b, {"mode": "private", "primary": True})
        self.assertEqual(updated["mode"], "private")
        self.assertTrue(updated["primary"])
        # private not in accessible
        self.assertNotIn(self.ws_b, wr.accessible_roots())

        ok = wr.detach(wid_b)
        self.assertTrue(ok)
        self.assertEqual(len(wr.list_workspaces()), 1)

    def test_attach_duplicate_raises(self):
        wr = self.wr
        wr.attach(self.ws_a, mode="r")
        with self.assertRaises(ValueError):
            wr.attach(self.ws_a, mode="rw")

    def test_attach_missing_raises(self):
        wr = self.wr
        with self.assertRaises(ValueError):
            wr.attach(os.path.join(self.root, "nope"), mode="r")

    def test_mode_cycle_including_r_plus_w(self):
        wr = self.wr
        res = wr.attach(self.ws_a, mode="r+w")
        self.assertEqual(res["workspace"]["mode"], "rw")
        wid = res["workspace"]["id"]
        self.assertEqual(wr.update_workspace(wid, {"mode": "private"})["mode"], "private")
        self.assertEqual(wr.update_workspace(wid, {"mode": "r"})["mode"], "r")
        self.assertEqual(wr.update_workspace(wid, {"mode": "r+w"})["mode"], "rw")

    def test_enabled_and_private_filter(self):
        wr = self.wr
        a = wr.attach(self.ws_a, mode="rw")["workspace"]
        b = wr.attach(self.ws_b, mode="private")["workspace"]
        self.assertIn(self.ws_a, wr.accessible_roots())
        self.assertNotIn(self.ws_b, wr.accessible_roots())

        wr.update_workspace(a["id"], {"enabled": False})
        self.assertEqual(wr.accessible_roots(), [])

        wr.update_workspace(b["id"], {"mode": "r", "enabled": True})
        self.assertIn(self.ws_b, wr.accessible_roots())

    def test_missing_folder_primary_does_not_steal(self):
        wr = self.wr
        a = wr.attach(self.ws_a, mode="rw")["workspace"]
        missing = os.path.join(self.root, "gone")
        store = wr.load_store(force=True)
        store["workspaces"].append(
            {
                "id": "gone",
                "name": "gone",
                "path": missing,
                "mode": "rw",
                "primary": False,
                "enabled": True,
                "order": 1,
            }
        )
        wr.save_store(store)

        # missing as primary must not steal from enabled+exists sibling
        wr.update_workspace("gone", {"primary": True})
        items = {w["id"]: w for w in wr.list_workspaces()}
        self.assertTrue(items[a["id"]]["primary"])
        self.assertFalse(items["gone"]["primary"])
        self.assertEqual(wr.primary_root(), self.ws_a)

    def test_disable_last_rw_workspace_roots_empty(self):
        wr = self.wr
        a = wr.attach(self.ws_a, mode="rw")["workspace"]
        self.assertTrue(a["primary"])
        config.CODE_WORKSPACES_USE_REGISTRY = True
        config.CODE_WORKSPACE_ROOTS = [self.ws_b]
        wr.update_workspace(a["id"], {"enabled": False})
        # Save wirft nicht; primary darf stehen bleiben
        item = wr.get_workspace(a["id"])
        self.assertFalse(item["enabled"])
        self.assertTrue(item["primary"])
        self.assertEqual(wr.accessible_roots(), [])
        self.assertEqual(code_tools.workspace_roots(), [])

    def test_heads_default_and_cross_bind(self):
        wr = self.wr
        a = wr.attach(self.ws_a, mode="rw")["workspace"]
        self.assertEqual(a["heads"]["code"], "rw")
        self.assertEqual(a["heads"]["agent"], "unbound")
        self.assertEqual(a["heads"]["grok"], "rw")
        self.assertIn(self.ws_a, wr.accessible_roots())

        wr.update_workspace(a["id"], {"heads": {"code": "private", "agent": "r"}})
        self.assertNotIn(self.ws_a, wr.accessible_roots())
        from core import vaults_registry as vr

        # agent sees the workspace when bound
        self.assertIn(self.ws_a, vr.paths_for_agent())

        wr.update_workspace(a["id"], {"heads": {"agent": "unbound", "code": "r"}})
        self.assertIn(self.ws_a, wr.accessible_roots())

    def test_save_disk_has_no_exists_load_old_exists(self):
        wr = self.wr
        wr.attach(self.ws_a, mode="r")
        with open(self.store, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertTrue(disk["workspaces"])
        for item in disk["workspaces"]:
            self.assertNotIn("exists", item)

        disk["workspaces"][0]["exists"] = False
        with open(self.store, "w", encoding="utf-8") as f:
            json.dump(disk, f)
        bind_store._mtime_cache.clear()
        loaded = wr.load_store(force=True)
        self.assertTrue(loaded["workspaces"][0]["exists"])

    def test_bad_order_does_not_fall_through_to_default_roots(self):
        wr = self.wr
        wr.attach(self.ws_a, mode="rw")
        with open(self.store, encoding="utf-8") as f:
            disk = json.load(f)
        disk["workspaces"][0]["order"] = "nope"
        with open(self.store, "w", encoding="utf-8") as f:
            json.dump(disk, f)
        bind_store._mtime_cache.clear()
        config.CODE_WORKSPACES_USE_REGISTRY = True
        config.CODE_WORKSPACE_ROOTS = [self.ws_b]
        loaded = wr.load_store(force=True)
        self.assertEqual(loaded["workspaces"][0]["order"], 0)
        self.assertEqual(code_tools.workspace_roots(), [self.ws_a])

    def test_cache_after_save_has_live_exists(self):
        wr = self.wr
        wr.attach(self.ws_a, mode="rw")
        cached = wr.load_store()
        self.assertTrue(cached["workspaces"])
        for item in cached["workspaces"]:
            self.assertIn("exists", item)
            self.assertEqual(item["exists"], os.path.isdir(item["path"]))
        self.assertTrue(wr.accessible_roots())


if __name__ == "__main__":
    unittest.main()
