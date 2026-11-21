"""Record a complete installed-package suite; skips or an empty suite fail qualification."""
import json
from pathlib import Path
import sys
import unittest

suite = unittest.defaultTestLoader.discover("tests", pattern="test_" + sys.argv[1] + ".py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
record = {"suite": sys.argv[1], "tests": result.testsRun, "failures": len(result.failures),
          "errors": len(result.errors), "skips": len(result.skipped)}
Path(sys.argv[2]).write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
raise SystemExit(0 if result.wasSuccessful() and result.testsRun > 0 and not result.skipped else 1)
