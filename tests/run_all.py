"""Run every test suite. Usage: python tests/run_all.py"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = (
    "test_dsp.py",
    "test_engine.py",
    "test_ui.py",
    "test_app.py",
    "test_launcher.py",
    "test_ladspa.py",
    "test_i18n.py",
)


def main():
    failed = []
    for suite in SUITES:
        print("\n" + "#" * 62)
        print("# %s" % suite)
        print("#" * 62)
        result = subprocess.call([sys.executable, os.path.join(HERE, suite)])
        if result != 0:
            failed.append(suite)

    print("\n" + "#" * 62)
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("All suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
