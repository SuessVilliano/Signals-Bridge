"""
Vercel Serverless Entry Point.

Vercel routes ALL requests through this file.
It imports the FastAPI app and exposes it as `app` for Vercel's Python runtime.
"""

import sys
import os

# Add the project root to the Python path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main_vercel import app
