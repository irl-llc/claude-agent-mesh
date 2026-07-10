"""Structural checks on the plugin packaging (D9)."""

import json
import os
import unittest

from tests import REPO_ROOT


class PluginPackagingTest(unittest.TestCase):
    def _read_json(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts)) as f:
            return json.load(f)

    def test_marketplace_lists_the_plugin_at_repo_root(self):
        marketplace = self._read_json(".claude-plugin", "marketplace.json")
        self.assertEqual(marketplace["name"], "claude-agent-mesh")
        (plugin,) = marketplace["plugins"]
        self.assertEqual(plugin["name"], "agent-mesh")
        self.assertEqual(plugin["source"], "./")

    def test_plugin_manifest_matches_marketplace(self):
        manifest = self._read_json(".claude-plugin", "plugin.json")
        marketplace = self._read_json(".claude-plugin", "marketplace.json")
        self.assertEqual(manifest["name"], marketplace["plugins"][0]["name"])
        self.assertEqual(manifest["license"], "GPL-3.0")

    def test_skill_has_frontmatter_and_teaches_the_cli(self):
        path = os.path.join(REPO_ROOT, "skills", "agent-messaging", "SKILL.md")
        with open(path) as f:
            text = f.read()
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1]
        self.assertIn("name: agent-messaging", frontmatter)
        self.assertIn("description:", frontmatter)
        # The skill must teach the CLI, never direct inbox writes (D10).
        self.assertIn("claude_agent_mesh.py", text)
        self.assertIn("Never write inbox files directly", text)
        # Regression: a `MESH="python3 …"` command variable breaks under zsh
        # (no implicit word-splitting) — the skill must teach direct invocation.
        self.assertNotIn('MESH="', text)
        self.assertIn("message-id", text)
        self.assertIn("coalesce", text.lower())

    def test_shipped_entrypoints_exist_and_are_executable(self):
        for name in ("mesh_wrapper.py", "claude_agent_mesh.py"):
            path = os.path.join(REPO_ROOT, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertTrue(os.access(path, os.X_OK), name + " must be executable")


if __name__ == "__main__":
    unittest.main()
