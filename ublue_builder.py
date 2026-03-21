#!/usr/bin/env python3
"""Compatibility wrapper for the legacy script name.

The project has grown beyond Universal Blue-only images, so the primary module
and command now live in ``atomic_image_builder.py``. Keep this shim so existing
checkouts and old documentation do not break immediately.
"""

from atomic_image_builder import *  # noqa: F401,F403
from atomic_image_builder import main


if __name__ == "__main__":
    main()
