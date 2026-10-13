"""Import the packaged runtime rather than an installed machine-local copy."""
import sys
from pathlib import Path
RUNTIME = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
sys.path.insert(0, str(RUNTIME))
