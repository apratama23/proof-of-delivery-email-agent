import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SimulationModuleNamesTest(unittest.TestCase):
    def test_no_sim_module_hides_a_project_package(self):
        # Streamlit puts sim/ first on the import path for `sim.run --dashboard`,
        # so a sim/dashboard.py would be imported instead of the dashboard package.
        packages = {path.name for path in ROOT.iterdir() if (path / "__init__.py").exists() or path.name in {"app", "dashboard"}}
        modules = {path.stem for path in (ROOT / "sim").glob("*.py")}
        self.assertFalse(modules & packages, f"sim/ modules named like a package: {sorted(modules & packages)}")


if __name__ == "__main__":
    unittest.main()
