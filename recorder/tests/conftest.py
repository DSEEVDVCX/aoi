"""Makes the recorder package importable in tests without installation.

Adds the recorder/ directory (the parent) to sys.path so that `import extract`
and `import db` work the same way the recorder code itself imports them
(flat imports, not a package).
"""
import os
import sys

RECORDER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RECORDER_DIR not in sys.path:
    sys.path.insert(0, RECORDER_DIR)
