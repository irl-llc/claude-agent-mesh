"""Structural checks on the uv/Homebrew install routes (Q5/D9)."""

import os
import re
import unittest

from tests import REPO_ROOT


class PyprojectTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, "pyproject.toml")) as f:
            self.text = f.read()

    def test_console_scripts_point_at_real_entry_points(self):
        for script, module, func in (
            ("claude-agent-mesh", "claude_agent_mesh", "cli_main"),
            ("claude-agent-mesh-wrapper", "mesh_wrapper", "cli_main"),
        ):
            self.assertIn('%s = "%s:%s"' % (script, module, func), self.text)
            path = os.path.join(REPO_ROOT, module + ".py")
            with open(path) as f:
                self.assertIn("def %s(" % func, f.read(), path)

    def test_every_shipped_module_is_listed(self):
        listed = re.search(r"py-modules\s*=\s*\[([^\]]*)\]", self.text).group(1)
        for module in ("claude_agent_mesh", "mesh_runtime", "mesh_wrapper", "wire"):
            self.assertIn('"%s"' % module, listed)
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, module + ".py")))

    def test_no_dependencies_stdlib_only(self):
        self.assertNotIn("dependencies", self.text.replace("# Deliberately no dependencies", ""))


class FormulaTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, "Formula", "claude-agent-mesh.rb")) as f:
            self.text = f.read()

    def test_installs_both_commands(self):
        self.assertIn('"claude-agent-mesh"', self.text)
        self.assertIn('"claude-agent-mesh-wrapper"', self.text)

    def test_ships_every_module_the_wrapper_imports(self):
        for module in ("claude_agent_mesh", "mesh_runtime", "mesh_wrapper", "wire"):
            name = module + ".py"
            self.assertIn('"%s"' % name, self.text, name)
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, name)))

    def test_license_matches_repo(self):
        self.assertIn('license "GPL-3.0-only"', self.text)


if __name__ == "__main__":
    unittest.main()
