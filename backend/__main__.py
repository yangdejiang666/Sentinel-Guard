"""Run the local Sentinel API with ``python -m backend``."""

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "backend.app.main:app",
        host=os.getenv("SENTINEL_HOST", "127.0.0.1"),
        port=int(os.getenv("SENTINEL_PORT", "8000")),
    )
