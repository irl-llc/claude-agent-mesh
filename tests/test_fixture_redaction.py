"""The fixture redaction contract, enforced (T1).

"Sanitized" is a test, not a claim: every file under testdata/ is scanned for
secret-shaped patterns. Extend PATTERNS when new fixture material lands.
"""

import os
import re
import unittest

from tests import REPO_ROOT

TESTDATA = os.path.join(REPO_ROOT, "testdata")

PATTERNS = [
    ("anthropic api key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai-style key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{16,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("email address", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("absolute macOS home path", re.compile(r"/Users/(?!PLACEHOLDER\b)[A-Za-z0-9._\-]+")),
    ("absolute linux home path", re.compile(r"/home/(?!PLACEHOLDER\b)[A-Za-z0-9._\-]+")),
    ("oauth-ish value", re.compile(r"(?i)(oauth|access[_-]?token)['\"]?\s*[:=]\s*['\"](?!REDACTED|PLACEHOLDER)[^'\"]{8,}")),
]


class FixtureRedactionTest(unittest.TestCase):
    def test_testdata_exists_and_is_nonempty(self):
        names = os.listdir(TESTDATA)
        self.assertTrue(names, "testdata/ must carry the fixture")

    def test_no_secret_shaped_content(self):
        violations = []
        for dirpath, _dirnames, filenames in os.walk(TESTDATA):
            for name in filenames:
                path = os.path.join(dirpath, name)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        for label, pattern in PATTERNS:
                            m = pattern.search(line)
                            if m:
                                violations.append(
                                    "%s:%d %s: %r"
                                    % (os.path.relpath(path, TESTDATA), lineno, label, m.group(0)[:40])
                                )
        self.assertEqual(violations, [], "\n".join(violations))
