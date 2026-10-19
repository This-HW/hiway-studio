"""Test environment defaults. Set BEFORE any app.* import (pytest imports this first).

Real environment variables override these via setdefault.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("PROJECTS_ROOT", "/tmp/ai-studio-test-projects")
os.environ.setdefault("SSE_BACKEND", "memory")
