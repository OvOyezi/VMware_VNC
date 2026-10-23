"""Bounded, read-only capture timing diagnostics for an explicit VNC endpoint."""

import argparse
import json
from pathlib import Path
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
OUTPUT = SOURCE / "checks/results"
sys.path.insert(0, str(SOURCE / "src"))
from vnc_mcp import client
from vnc_mcp.rfb import runtime_info


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--capture-count", type=int, default=3, help="number of read-only captures (1-10)")
    parser.add_argument("--interval-ms", type=int, default=100, help="delay between captures (0-5000)")
    return parser


def _capture_metadata(session: client.VNCSession, image, index: int, elapsed: float) -> dict:
    row = {
        "success": True,
        "index": index,
        "call_elapsed_seconds": round(elapsed, 6),
        **image.metadata(),
    }
    connection = session.client
    if connection is not None:
        row["frame"] = {
            "sequence": connection.frame_sequence,
            "id": connection.frame_id,
            "time": connection.frame_time,
            "updated_regions": list(connection.updated_regions),
            "width": connection.width,
            "height": connection.height,
        }
    return row


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    options = parser.parse_args(argv)
    if not 1 <= options.capture_count <= 10:
        parser.error("--capture-count must be between 1 and 10")
    if not 0 <= options.interval_ms <= 5000:
        parser.error("--interval-ms must be between 0 and 5000")

    output = options.output_directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    session = client.VNCSession(
        timeout=options.timeout,
        action_timeout=options.timeout,
        screenshot_mode="refresh",
    )
    started = time.monotonic()
    report = {
        "input_sent": False,
        "capture_count_requested": options.capture_count,
        "interval_ms": options.interval_ms,
        "captures": [],
    }
    try:
        report["connection"] = session.connect(options.host, options.port)
        report["runtime"] = runtime_info()
        for index in range(options.capture_count):
            capture_started = time.monotonic()
            try:
                image = session.capture("png")
                row = _capture_metadata(session, image, index + 1, time.monotonic() - capture_started)
                report["captures"].append(row)
                if index == 0:
                    image_path = output / "vnc-refresh-diagnosis.png"
                    image_path.write_bytes(image.data)
                    report["first_image"] = str(image_path)
            except Exception as error:
                report["captures"].append({
                    "index": index + 1,
                    "call_elapsed_seconds": round(time.monotonic() - capture_started, 6),
                    "success": False,
                    "error": f"{type(error).__name__}: {error}",
                })
                break
            if index + 1 < options.capture_count:
                time.sleep(options.interval_ms / 1000)
        report["success"] = len(report["captures"]) == options.capture_count and all(
            "error" not in item for item in report["captures"]
        )
    except Exception as error:
        report["success"] = False
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report.setdefault("runtime", runtime_info())
        try:
            report["disconnect"] = session.disconnect()
        except Exception as error:
            report["disconnect_error"] = f"{type(error).__name__}: {error}"
            report["success"] = False
        report["elapsed_seconds"] = round(time.monotonic() - started, 6)
        report_path = output / "vnc-refresh-diagnosis.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
