"""Make backend/ importable as a flat module root so schedule_proposal's
`from models import TaskCreate` resolves during tests."""
import sys, pathlib
_backend = pathlib.Path(__file__).resolve().parents[1]
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
