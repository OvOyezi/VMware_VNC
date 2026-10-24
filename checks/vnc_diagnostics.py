"""Standard-library checks for the bounded VMware VNC diagnostics helper."""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


_diagnostics = None


class DiagnosticsChecks(unittest.TestCase):
    def test_missing_distributions(self):
        missing = _diagnostics.metadata.PackageNotFoundError
        with mock.patch.object(_diagnostics.metadata, "version", side_effect=missing):
            versions = _diagnostics.get_diagnostics()["runtime_versions"]
        self.assertEqual(
            set(versions), {"mcp", "Pillow", "cryptography", "websockets"}
        )
        for dependency in versions.values():
            self.assertEqual(dependency["available"], False)
            self.assertIsNone(dependency["version"])
            self.assertEqual(dependency["reason"], "not_installed")

    def test_explicit_missing_vmrun_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = str(Path(temp_dir) / "missing-vmrun.exe")
            with mock.patch.dict(
                _diagnostics.os.environ,
                {"VNC_VMRUN_PATH": missing_path},
                clear=True,
            ), mock.patch.object(
                _diagnostics.shutil, "which", return_value="C:/fallback/vmrun.exe"
            ) as which:
                result = _diagnostics._discover_vmrun()

        self.assertEqual(result["source"], "VNC_VMRUN_PATH")
        self.assertEqual(result["path"], missing_path)
        self.assertFalse(result["exists"])
        which.assert_not_called()

    def test_path_vmrun_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vmrun_path = Path(temp_dir) / "vmrun.exe"
            vmrun_path.write_text("fixture", encoding="utf-8")
            with mock.patch.dict(_diagnostics.os.environ, {}, clear=True), mock.patch.object(
                _diagnostics.shutil, "which", return_value=str(vmrun_path)
            ):
                result = _diagnostics._discover_vmrun()

        self.assertEqual(result, {"source": "PATH", "path": str(vmrun_path), "exists": True})

    def test_program_files_vmrun_location_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vmrun_path = (
                Path(temp_dir)
                / "VMware"
                / "VMware Workstation"
                / "vmrun.exe"
            )
            vmrun_path.parent.mkdir(parents=True)
            vmrun_path.write_text("fixture", encoding="utf-8")
            with mock.patch.dict(
                _diagnostics.os.environ, {"ProgramFiles": temp_dir}, clear=True
            ), mock.patch.object(_diagnostics.shutil, "which", return_value=None), mock.patch.object(
                _diagnostics.platform, "system", return_value="Windows"
            ):
                result = _diagnostics._discover_vmrun()

        self.assertEqual(result, {"source": "ProgramFiles", "path": str(vmrun_path), "exists": True})

    def test_unrelated_secret_environment_values_are_not_returned(self):
        secret = "diagnostics-test-secret-value"
        with mock.patch.dict(
            _diagnostics.os.environ,
            {"UNRELATED_TEST_SECRET": secret, "VNC_VMRUN_PATH": "C:/tools/vmrun.exe"},
            clear=True,
        ):
            report = _diagnostics.get_diagnostics()

        serialized = json.dumps(report)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("UNRELATED_TEST_SECRET", serialized)

    def test_report_is_json_serializable(self):
        report = _diagnostics.get_diagnostics()
        encoded = json.dumps(report)
        self.assertIsInstance(encoded, str)
        self.assertEqual(report["schema_version"], 1)


def _load_diagnostics(source_root: Path):
    module_path = source_root / "src" / "vmware_vnc" / "diagnostics.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"Diagnostics module not found: {module_path}")
    sys.path.insert(0, str(source_root / "src"))
    module = importlib.import_module("vmware_vnc.diagnostics")
    if Path(module.__file__).resolve() != module_path.resolve():
        raise ImportError(f"Diagnostics module loaded from a different source: {module.__file__}")
    return module


def _run_checks() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DiagnosticsChecks)
    tests = list(suite)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    failed_ids = {
        test.id()
        for test, _ in (*result.failures, *result.errors)
    }
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed_ids
        for test in tests
    }
    report = {"success": result.wasSuccessful(), "checks": checks}
    if not result.wasSuccessful():
        report["details"] = output.getvalue()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)

    global _diagnostics
    try:
        _diagnostics = _load_diagnostics(args.source.resolve())
        report = _run_checks()
    except Exception as exc:
        report = {"success": False, "checks": {"module_loads": False},
                  "failure": f"{type(exc).__name__}: {exc}"}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
