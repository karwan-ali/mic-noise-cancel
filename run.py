#!/usr/bin/env python
"""Console entry point: python run.py

Double-clicking is handled by MicNoiseCancel.pyw, which starts without a
console window and installs the dependencies if they are missing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from micnoise.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
