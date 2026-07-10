import os
import sys

# Modules under test live at the repo root (they are spawned standalone by
# VS Code, not installed as a package), so put the root on the path once.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
