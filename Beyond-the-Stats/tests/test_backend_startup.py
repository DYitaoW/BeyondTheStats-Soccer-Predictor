"""Tests verifying that backend startup does not trigger the full pipeline by default."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Add main and root to path
_ROOT = Path(__file__).resolve().parent.parent
_MAIN = _ROOT / "main"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_MAIN) not in sys.path:
    sys.path.insert(0, str(_MAIN))

from Backend.server import BackendConfig
import run_backend


class TestBackendStartupConfiguration(unittest.TestCase):
    def test_backend_config_defaults_run_on_start_false(self):
        """BackendConfig must default run_on_start to False."""
        cfg = BackendConfig()
        self.assertFalse(cfg.run_on_start)

    def test_run_backend_cli_default_run_on_start_false(self):
        """CLI arguments without --run-on-start should result in run_on_start=False."""
        with patch.object(sys, "argv", ["run_backend.py"]):
            args = run_backend.parse_args()
            self.assertFalse(args.run_on_start)

    def test_run_backend_cli_flag_run_on_start(self):
        """CLI argument --run-on-start should enable run_on_start=True."""
        with patch.object(sys, "argv", ["run_backend.py", "--run-on-start"]):
            args = run_backend.parse_args()
            self.assertTrue(args.run_on_start)

    def test_run_backend_cli_flag_no_run_on_start(self):
        """CLI argument --no-run-on-start should ensure run_on_start=False."""
        with patch.object(sys, "argv", ["run_backend.py", "--no-run-on-start"]):
            args = run_backend.parse_args()
            self.assertFalse(args.run_on_start)

    def test_env_var_pipeline_run_on_start(self):
        """Setting PIPELINE_RUN_ON_START=1 should default CLI to True when flag is absent."""
        with patch.dict(os.environ, {"PIPELINE_RUN_ON_START": "1"}):
            with patch.object(sys, "argv", ["run_backend.py"]):
                args = run_backend.parse_args()
                self.assertTrue(args.run_on_start)

    def test_env_var_overridden_by_cli_no_run_on_start(self):
        """Explicit --no-run-on-start overrides PIPELINE_RUN_ON_START=1."""
        with patch.dict(os.environ, {"PIPELINE_RUN_ON_START": "1"}):
            with patch.object(sys, "argv", ["run_backend.py", "--no-run-on-start"]):
                args = run_backend.parse_args()
                self.assertFalse(args.run_on_start)


if __name__ == "__main__":
    unittest.main()

