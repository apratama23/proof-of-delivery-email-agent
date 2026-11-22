"""The real dashboard on the simulated data: started by `python -m sim.run --dashboard`.

Named sim_dashboard, not dashboard: Streamlit puts this folder first on the
import path, and a sim/dashboard.py would hide the dashboard package.
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim import fakes  # noqa: E402

fakes.apply_settings()  # every rerun: the dashboard reads and writes only the simulation folders
runpy.run_path(str(ROOT / "dashboard" / "app.py"), run_name="__main__")
