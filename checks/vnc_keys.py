"""Focused behavioral checks for canonical RFB key chords."""
from __future__ import annotations

import argparse
from dataclasses import FrozenInstanceError
import importlib
import json
from pathlib import Path
import sys
import traceback


def _load(source: Path):
    src = source / "src"
    if not (src / "vmware_vnc" / "keys.py").is_file():
        raise FileNotFoundError("src/vmware_vnc/keys.py was not found under --source")
    sys.path.insert(0, str(src))
    return importlib.import_module("vmware_vnc.keys")


def _run(keys) -> dict[str, object]:
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, operation) -> None:
        try:
            operation()
            checks[name] = True
        except Exception as exc:  # the JSON report records the exact failed behavior
            checks[name] = False
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    def aliases_and_canonical_order() -> None:
        cases = (
            ("escape", "esc"), ("Backspace", "bsp"), ("Return", "enter"),
            ("insert", "ins"), ("pageup", "pgup"), ("pagedown", "pgdn"),
            ("windows", "super"), ("meta", "super"), ("del", "delete"),
        )
        for alias, canonical in cases:
            assert keys.normalize_key(alias) == canonical
        assert keys.normalize_key("plus") == "+"
        assert keys.normalize_key("minus") == "-"
        first = keys.parse_key("ctrl+shift+A")
        second = keys.parse_key("SHIFT-CTRL-A")
        third = keys.parse_key("A+shift+ctrl")
        assert first.name == second.name == third.name == "ctrl-A"
        assert first.codes == second.codes == third.codes == (0xFFE3, 0xFFE1, ord("A"))
        assert first.release_codes == (ord("A"), 0xFFE1, 0xFFE3)
        assert first.components == frozenset((0xFFE3, 0xFFE1, ord("A")))

    def actual_sequences_and_releases() -> None:
        assert keys.keysyms("A") == [0xFFE1, ord("A")]
        assert keys.parse_key("A").release_codes == (ord("A"), 0xFFE1)
        assert keys.keysyms("?") == [0xFFE1, ord("?")]
        assert keys.keysyms("+") == [0xFFE1, ord("+")]
        assert keys.keysyms("-") == [ord("-")]
        uppercase = keys.parse_key("A")
        explicit_shift = keys.parse_key("shift+A")
        assert uppercase.name == explicit_shift.name == "A"
        assert uppercase.codes == explicit_shift.codes == (0xFFE1, ord("A"))
        assert keys.keysyms("ctrl+l") == [0xFFE3, ord("l")]
        assert keys.parse_key("ctrl+l").release_codes == (ord("l"), 0xFFE3)
        assert keys.KEYS["f35"] == 0xFFE0

    def punctuation_chords() -> None:
        plus_forms = ("ctrl++", "ctrl-+", "ctrl+plus", "plus+ctrl")
        plus_chords = [keys.parse_key(value) for value in plus_forms]
        assert all(chord.name == "ctrl-plus" for chord in plus_chords)
        assert all(chord.codes == (0xFFE3, 0xFFE1, ord("+")) for chord in plus_chords)
        minus_forms = ("ctrl+-", "ctrl--", "ctrl+minus", "minus+ctrl")
        minus_chords = [keys.parse_key(value) for value in minus_forms]
        assert all(chord.name == "ctrl-minus" for chord in minus_chords)
        assert all(chord.codes == (0xFFE3, ord("-")) for chord in minus_chords)
        assert keys.normalize_key("ctrl + shift + ?") == "ctrl-?"
        assert keys.keysyms("ctrl+shift+?") == [0xFFE3, 0xFFE1, ord("?")]

    def implicit_shift_deduplicates() -> None:
        implicit = keys.parse_key("A")
        explicit = keys.parse_key("shift+A")
        right_shift = keys.parse_key("shift_r+A")
        assert implicit.codes == explicit.codes == (0xFFE1, ord("A"))
        assert right_shift.codes == (0xFFE2, ord("A"))
        assert keys.parse_key("shift+shift+A").codes == (0xFFE1, ord("A"))
        double_shift = keys.parse_key("shift+shift_r+A")
        assert double_shift.name == "shift-shift_r-A"
        assert double_shift.codes == (0xFFE1, 0xFFE2, ord("A"))
        assert keys.parse_key("ctrl+ctrl+a").codes == (0xFFE3, ord("a"))

    def unicode_keysyms_are_explicit() -> None:
        assert keys.keysyms("é") == [0xE9]
        assert keys.keysyms("€") == [0x010020AC]
        assert keys.keysyms("🙂") == [0x0101F642]
        # A non-ASCII capital does not imply a guessed guest Shift/layout.
        assert keys.keysyms("É") == [0xC9]
        assert keys.keysyms("ctrl+🙂") == [0xFFE3, 0x0101F642]

    def malformed_and_ambiguous_inputs_fail() -> None:
        invalid = (
            "", "   ", "\x00", "\n", "\t", "\x7f", "ctrl+", "+ctrl", "ctrl--a", "ctrl++a",
            "not-a-real-key", "a+a", "e\u0301", "\ud800", "\udfff",
        )
        for value in invalid:
            try:
                keys.parse_key(value)
            except ValueError:
                continue
            raise AssertionError(f"invalid key unexpectedly accepted: {value!r}")
        for value in (None, 1, []):
            try:
                keys.parse_key(value)
            except ValueError:
                continue
            raise AssertionError(f"non-string key unexpectedly accepted: {value!r}")

    def chords_are_frozen() -> None:
        chord = keys.parse_key("ctrl+a")
        try:
            chord.name = "alt+a"
        except FrozenInstanceError:
            return
        raise AssertionError("KeyChord accepted mutation")

    check("aliases_and_canonical_order", aliases_and_canonical_order)
    check("actual_sequences_and_releases", actual_sequences_and_releases)
    check("punctuation_chords", punctuation_chords)
    check("implicit_shift_deduplicates", implicit_shift_deduplicates)
    check("unicode_keysyms_are_explicit", unicode_keysyms_are_explicit)
    check("malformed_and_ambiguous_inputs_fail", malformed_and_ambiguous_inputs_fail)
    check("chords_are_frozen", chords_are_frozen)
    result: dict[str, object] = {"success": not failures, "checks": checks}
    if failures:
        result["failure"] = "\n".join(failures)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)

    try:
        keys = _load(args.source.resolve())
        report = _run(keys)
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"module_load": False},
            "failure": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
