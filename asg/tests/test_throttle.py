"""
Test suite for the scrub controller's throttling logic.

Run with: python3 -m unittest asg.tests.test_throttle -v
"""

import unittest
from unittest.mock import patch, mock_open, MagicMock

from asg.scrub_controller import (
    get_load_average,
    is_system_busy,
)
from asg import config


def _init_test_config():
    """Initialise config with defaults for testing."""
    config._active_config = config._deep_merge(config._DEFAULTS, {})
    config._active_config["state_dir"] = "/tmp/asg-test"


class TestLoadAverage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _init_test_config()

    @patch("builtins.open", mock_open(read_data="0.50 0.60 0.70 1/234 5678\n"))
    def test_normal_load(self):
        self.assertAlmostEqual(get_load_average(), 0.50)

    @patch("builtins.open", mock_open(read_data="4.25 3.10 2.80 5/456 7890\n"))
    def test_high_load(self):
        self.assertAlmostEqual(get_load_average(), 4.25)

    @patch("builtins.open", mock_open(read_data="0.00 0.01 0.05 1/100 1234\n"))
    def test_idle_system(self):
        self.assertAlmostEqual(get_load_average(), 0.00)

    @patch("builtins.open", side_effect=OSError("File not found"))
    def test_proc_unavailable(self, _mock):
        self.assertEqual(get_load_average(), 0.0)

    @patch("builtins.open", mock_open(read_data="garbage data\n"))
    def test_malformed_loadavg(self):
        self.assertEqual(get_load_average(), 0.0)


class TestSystemBusy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _init_test_config()

    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_idle_system_not_busy(self, mock_load, mock_io):
        mock_load.return_value = 0.5
        mock_io.return_value = {"sdc": 2.0, "sdd": 2.0}
        busy, reason = is_system_busy()
        self.assertFalse(busy)
        self.assertEqual(reason, "")

    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_high_load_triggers_busy(self, mock_load, mock_io):
        mock_load.return_value = 3.5
        mock_io.return_value = {"sdc": 5.0}
        busy, reason = is_system_busy()
        self.assertTrue(busy)
        self.assertIn("load average", reason)

    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_high_io_triggers_busy(self, mock_load, mock_io):
        mock_load.return_value = 1.0
        mock_io.return_value = {"sdc": 65.0, "sdd": 5.0}
        busy, reason = is_system_busy()
        self.assertTrue(busy)
        self.assertIn("I/O utilisation", reason)

    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_boundary_load_not_busy(self, mock_load, mock_io):
        """Load exactly at threshold should NOT trigger (strict >)."""
        mock_load.return_value = 3.0
        mock_io.return_value = {"sdc": 0.0}
        busy, _reason = is_system_busy()
        self.assertFalse(busy)

    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_boundary_io_not_busy(self, mock_load, mock_io):
        """I/O exactly at threshold should NOT trigger (strict >)."""
        mock_load.return_value = 0.5
        mock_io.return_value = {"sdc": 40.0, "sdd": 40.0}
        busy, _reason = is_system_busy()
        self.assertFalse(busy)

    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_just_above_threshold_triggers(self, mock_load, mock_io):
        mock_load.return_value = 0.5
        mock_io.return_value = {"sdc": 10.0, "sdd": 40.1}
        busy, reason = is_system_busy()
        self.assertTrue(busy)


class TestSystemBusyRunningMode(unittest.TestCase):
    """
    is_system_busy(running=True), used while a scrub is already in progress.
    The scrub's own load and device utilisation must not pause it — only the
    elevated load threshold or genuine pool write load may.
    """

    @classmethod
    def setUpClass(cls):
        _init_test_config()

    @patch("asg.scrub_controller.get_pool_write_iops")
    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_running_tolerates_scrub_own_footprint(self, mock_load, mock_io, mock_wiops):
        """Scrub-baseline load + saturated read I/O + no writes => not busy."""
        mock_load.return_value = 5.3
        mock_io.return_value = {"sdc": 98.0, "sdd": 97.0}
        mock_wiops.return_value = 0.0
        busy, reason = is_system_busy(running=True)
        self.assertFalse(busy, reason)

    @patch("asg.scrub_controller.get_pool_write_iops")
    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_running_still_pauses_on_real_load(self, mock_load, mock_io, mock_wiops):
        """Load above the elevated running threshold still pauses the scrub."""
        mock_load.return_value = config.get()["scrub"]["load_threshold_running"] + 0.5
        mock_io.return_value = {"sdc": 10.0}
        mock_wiops.return_value = 0.0
        busy, reason = is_system_busy(running=True)
        self.assertTrue(busy)
        self.assertIn("load average", reason)

    @patch("asg.scrub_controller.get_pool_write_iops")
    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_running_pauses_on_write_load(self, mock_load, mock_io, mock_wiops):
        """Sustained pool writes (genuine external load) pause the scrub."""
        mock_load.return_value = 2.0
        mock_io.return_value = {"sdc": 40.0}
        mock_wiops.return_value = config.get()["scrub"]["write_iops_threshold"] + 100
        busy, reason = is_system_busy(running=True)
        self.assertTrue(busy)
        self.assertIn("write load", reason)

    @patch("asg.scrub_controller.get_pool_write_iops")
    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_running_does_not_consult_io_utilisation(self, mock_load, mock_io, mock_wiops):
        """The per-device io_util check is skipped entirely while running."""
        mock_load.return_value = 1.0
        mock_io.side_effect = AssertionError(
            "get_io_utilisation must not be called while a scrub is running"
        )
        mock_wiops.return_value = 5.0
        busy, _reason = is_system_busy(running=True)
        self.assertFalse(busy)

    @patch("asg.scrub_controller.get_io_utilisation")
    @patch("asg.scrub_controller.get_load_average")
    def test_preflight_mode_unchanged(self, mock_load, mock_io):
        """Regression: default (pre-flight) mode keeps the sensitive threshold."""
        mock_load.return_value = config.get()["scrub"]["load_threshold"] + 0.1
        mock_io.return_value = {"sdc": 0.0}
        busy, reason = is_system_busy()
        self.assertTrue(busy)
        self.assertIn("load average", reason)


class TestThresholdDefaults(unittest.TestCase):
    """Verify default thresholds are sensible."""

    @classmethod
    def setUpClass(cls):
        _init_test_config()

    def test_load_threshold_reasonable(self):
        cfg = config.get()
        self.assertGreaterEqual(cfg["scrub"]["load_threshold"], 1.0)
        self.assertLessEqual(cfg["scrub"]["load_threshold"], 4.0)

    def test_io_threshold_reasonable(self):
        cfg = config.get()
        self.assertGreaterEqual(cfg["scrub"]["io_threshold_percent"], 10.0)
        self.assertLessEqual(cfg["scrub"]["io_threshold_percent"], 80.0)

    def test_running_load_threshold_clears_preflight(self):
        cfg = config.get()
        self.assertGreater(
            cfg["scrub"]["load_threshold_running"], cfg["scrub"]["load_threshold"]
        )

    def test_running_load_threshold_sane_for_four_cores(self):
        cfg = config.get()
        self.assertGreaterEqual(cfg["scrub"]["load_threshold_running"], 5.0)
        self.assertLessEqual(cfg["scrub"]["load_threshold_running"], 12.0)

    def test_write_iops_threshold_positive(self):
        cfg = config.get()
        self.assertGreater(cfg["scrub"]["write_iops_threshold"], 0.0)

    def test_poll_interval_not_too_aggressive(self):
        cfg = config.get()
        self.assertGreaterEqual(cfg["scrub"]["poll_interval_seconds"], 10)

    def test_grace_period_exists(self):
        cfg = config.get()
        self.assertGreater(cfg["scrub"]["grace_period_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
