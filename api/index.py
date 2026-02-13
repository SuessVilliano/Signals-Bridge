"""
Vercel Serverless Entry Point.

Vercel routes ALL requests through this file.
It imports the FastAPI app and exposes it as `app` for Vercel's Python runtime.
"""

import sys
import os
from pathlib import Path

# Add the project root to the Python path so `from app...` imports work.
# On Vercel, __file__ is /var/task/api/index.py, so parent.parent = /var/task
root = str(Path(__file__).resolve().parent.parent)
if root not in sys.path:
    sys.path.insert(0, root)

from app.main_vercel import app
