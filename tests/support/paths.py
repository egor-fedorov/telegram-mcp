"""Repository paths independent of the location of an individual test module."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
