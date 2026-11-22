"""Dashboard job cancellation."""

import time
import unittest

from app import jobs
from dashboard.tab_jobs import _JobController


class DashboardJobsTest(unittest.TestCase):
    def test_running_job_can_be_cancelled_and_controller_reused(self):
        controller = _JobController()
        job = jobs.Job("long job", "", (("-c", "import time; print('started', flush=True); time.sleep(30)"),))

        self.assertTrue(controller.start(job))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = controller.snapshot()
            if "started" in state["lines"]:
                break
            time.sleep(0.01)
        else:
            self.fail("Job did not start")

        controller.cancel()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and controller.snapshot()["status"] != "cancelled":
            time.sleep(0.01)

        self.assertEqual(controller.snapshot()["status"], "cancelled")
        self.assertTrue(controller.start(jobs.Job("quick job", "", (("-c", "print('done')"),))))

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and controller.snapshot()["status"] == "running":
            time.sleep(0.01)
        self.assertEqual(controller.snapshot()["status"], "finished")


if __name__ == "__main__":
    unittest.main()
