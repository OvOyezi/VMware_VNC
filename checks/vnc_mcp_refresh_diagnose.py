"""Read-only framebuffer completion diagnostics against an explicit VNC endpoint."""

from collections import Counter
import argparse
import json
from pathlib import Path
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
OUTPUT = SOURCE / "checks/results"
sys.path.insert(0, str(SOURCE / "src"))
from vnc_mcp import client

counts = Counter()
updates = []
requests = []
started = time.monotonic()
events = []


class TraceClient(client._ClipboardClient):
    def setImageMode(self):
        super().setImageMode()
        if options.explicit_format:
            self.setPixelFormat(self.pixel_format)

    def framebufferUpdateRequest(self, *args, **kwargs):
        requests.append({"at": time.monotonic() - started, "args": args, "kwargs": kwargs})
        return super().framebufferUpdateRequest(*args, **kwargs)

    def captureScreen(self, *args, **kwargs):
        events.append({"at": time.monotonic() - started, "event": "capture_enter"})
        deferred = super().captureScreen(*args, **kwargs)
        def completed(value):
            events.append({"at": time.monotonic() - started, "event": "capture_complete", "type": type(value).__name__})
            return value
        deferred.addBoth(completed)
        return deferred

    def commitUpdate(self, rectangles=None):
        counts["empty" if not rectangles else "nonempty"] += 1
        if len(updates) < 30:
            updates.append({"at": time.monotonic() - started, "rectangles": len(rectangles or []), "has_screen": self.screen is not None, "waiting": self.deferred is not None})
        super().commitUpdate(rectangles)


parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--output-directory", type=Path, default=OUTPUT)
parser.add_argument("--raw-only", action="store_true")
parser.add_argument("--timeout", type=float, default=5)
parser.add_argument("--explicit-format", action="store_true")
options = parser.parse_args()
OUTPUT = options.output_directory.resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
if options.raw_only:
    client._ClipboardFactory.pseudodesktop = False
    client._ClipboardFactory.last_rect = False
    client._ClipboardFactory.qemu_extended_key = False
client._ClipboardFactory.protocol = TraceClient
session = client.VNCSession(timeout=options.timeout)
report = {"input_sent": False, "raw_only": options.raw_only, "explicit_format": options.explicit_format}
try:
    report["connection"] = session.connect(options.host, options.port)
    session.client.protocol.screen.save(OUTPUT / "vnc-initial-frame.png")
    try:
        data, width, height = session.screenshot()
        report["refresh"] = {"success": True, "width": width, "height": height}
    except Exception as error:
        report["refresh"] = {"success": False, "error": f"{type(error).__name__}: {error}"}
        events.append({"at": time.monotonic() - started, "event": "timeout"})
finally:
    try:
        session.disconnect()
    except Exception as error:
        report["disconnect_error"] = str(error)
    report["update_counts"] = dict(counts)
    report["requests"] = requests
    report["events"] = events
    report["first_updates"] = updates
    suffix = "-format" if options.explicit_format else "-raw" if options.raw_only else ""
    (OUTPUT / f"vnc-refresh-diagnosis{suffix}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
