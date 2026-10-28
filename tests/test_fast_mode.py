import json
import os
import tempfile
import unittest
from unittest.mock import patch

from panel import panel


NODES = [("N1", "one"), ("N2", "two"), ("N3", "three")]


class FastModeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump({"password_hash": "keep-me", "nodes": NODES,
                       "fast_mode": True, "fast_nodes": []}, f)
        self.patches = [
            patch.object(panel, "CONFIG", self.cfg_path),
            patch.object(panel, "SERIALS", NODES),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def saved(self):
        with open(self.cfg_path) as f:
            return json.load(f)

    def test_disable_single_node_materializes_the_rest(self):
        panel.set_node_fast("N2", False)
        cfg = self.saved()
        self.assertTrue(cfg["fast_mode"])
        self.assertEqual(cfg["fast_nodes"], ["N1", "N3"])
        self.assertEqual(cfg["password_hash"], "keep-me")

    def test_disabling_every_node_turns_the_master_flag_off(self):
        for serial, _ in NODES:
            panel.set_node_fast(serial, False)
        cfg = self.saved()
        self.assertFalse(cfg["fast_mode"])
        self.assertEqual(cfg["fast_nodes"], [])
        self.assertFalse(panel.fast_enabled("N1"))

    def test_enabling_after_master_off_uses_explicit_list(self):
        for serial, _ in NODES:
            panel.set_node_fast(serial, False)
        panel.set_node_fast("N2", True)
        cfg = self.saved()
        self.assertTrue(cfg["fast_mode"])
        self.assertEqual(cfg["fast_nodes"], ["N2"])
        self.assertTrue(panel.fast_enabled("N2"))
        self.assertFalse(panel.fast_enabled("N1"))

    def test_enabling_every_node_collapses_to_all(self):
        for serial, _ in NODES:
            panel.set_node_fast(serial, True)
        cfg = self.saved()
        self.assertTrue(cfg["fast_mode"])
        self.assertEqual(cfg["fast_nodes"], [])

    def test_unknown_node_is_rejected_without_touching_config(self):
        before = self.saved()
        self.assertIsNone(panel.set_node_fast("nope", True))
        self.assertEqual(self.saved(), before)


class SessionTests(unittest.TestCase):
    def test_logout_revokes_the_token(self):
        panel.SESSIONS.clear()
        tok = panel.new_session()
        self.assertTrue(panel.check_session(tok))
        self.assertTrue(panel.drop_session(tok))
        self.assertFalse(panel.check_session(tok))
        self.assertFalse(panel.drop_session(tok))

    def test_drop_session_tolerates_empty_values(self):
        self.assertFalse(panel.drop_session(None))
        self.assertFalse(panel.drop_session(""))


if __name__ == "__main__":
    unittest.main()
