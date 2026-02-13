"""
Vercel Serverless Entry Point.

Vercel routes ALL requests through this file via the rewrite rule.
Exports `app` (FastAPI ASGI instance) for Vercel's Python runtime.
"""

import sys
import os
from pathlib import Path

# Add the project root to sys.path so `from app...` imports resolve.
root = str(Path(__file__).resolve().parent.parent)
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from app.main_vercel import app
except Exception as exc:
    # If the real app fails to import, serve a diagnostic page
    # so we can see the actual error instead of a blank Vercel crash.
    import traceback
    _err = traceback.format_exc()

    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    app = FastAPI()

    @app.get("/{full_path:path}")
    async def _startup_error(full_path: str = ""):
        return PlainTextResponse(
            f"Signal Bridge failed to start.\n\n{_err}",
            status_code=500,
        )
