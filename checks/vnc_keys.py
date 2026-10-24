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
        assert first.name == second.name == third.name == "ctrl-shift-a"
        assert first.codes == second.codes == third.codes == (0xFFE3, 0xFFE1, ord("a"))
        assert first.release_codes == (ord("a"), 0xFFE1, 0xFFE3)
        assert first.components == frozenset((0xFFE3, 0xFFE1, ord("a")))
        for chord in (first, second, third):
            reparsed = keys.parse_key(chord.name)
            assert reparsed.name == chord.name
            assert reparsed.codes == chord.codes

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

    def ascii_letters_keep_lowercase_without_modifier_shift() -> None:
        modifiers = (
            ("ctrl", 0xFFE3), ("ctrl_r", 0xFFE4),
            ("alt", 0xFFE9), ("alt_r", 0xFFEA),
            ("super", 0xFFEB), ("super_r", 0xFFEC),
        )
        letters = tuple("abcdefghijklmnopqrstuvwxyz")
        assert len(letters) * 2 == 52
        for modifier, modifier_code in modifiers:
            for lowercase in letters:
                for letter in (lowercase, lowercase.upper()):
                    chord = keys.parse_key(f"{modifier}+{letter}")
                    assert chord.name == f"{modifier}-{lowercase}"
                    assert chord.codes == (modifier_code, ord(lowercase))
                    assert chord.release_codes == (ord(lowercase), modifier_code)
                    reparsed = keys.parse_key(chord.name)
                    assert reparsed.name == chord.name
                    assert reparsed.codes == chord.codes

    def modifier_aliases_and_order() -> None:
        ctrl_forms = ("Ctrl+A", "CTRL-a", "ctrl+a")
        ctrl_chords = [keys.parse_key(value) for value in ctrl_forms]
        assert all(chord.name == "ctrl-a" for chord in ctrl_chords)
        assert all(chord.codes == (0xFFE3, ord("a")) for chord in ctrl_chords)

        super_forms = ("Meta+R", "WINDOWS-r", "win+R")
        super_chords = [keys.parse_key(value) for value in super_forms]
        assert all(chord.name == "super-r" for chord in super_chords)
        assert all(chord.codes == (0xFFEB, ord("r")) for chord in super_chords)

        alt = keys.parse_key("Alt+Z")
        super_key = keys.parse_key("Super+R")
        assert alt.name == "alt-z"
        assert alt.codes == (0xFFE9, ord("z"))
        assert super_key.name == "super-r"
        assert super_key.codes == (0xFFEB, ord("r"))
        for chord in (*ctrl_chords, *super_chords, alt, super_key):
            assert chord.release_codes == tuple(reversed(chord.codes))
            reparsed = keys.parse_key(chord.name)
            assert reparsed.name == chord.name
            assert reparsed.codes == chord.codes

    def explicit_shift_semantics() -> None:
        implicit = keys.parse_key("A")
        explicit = keys.parse_key("shift+A")
        right_shift = keys.parse_key("shift_r+A")
        assert implicit.codes == explicit.codes == (0xFFE1, ord("A"))
        assert implicit.name == explicit.name == "A"
        assert right_shift.codes == (0xFFE2, ord("A"))
        assert right_shift.name == "shift_r-A"
        assert keys.parse_key("shift+shift+A").codes == (0xFFE1, ord("A"))
        double_shift = keys.parse_key("shift+shift_r+A")
        assert double_shift.name == "shift-shift_r-A"
        assert double_shift.codes == (0xFFE1, 0xFFE2, ord("A"))
        assert keys.parse_key("shift+a").codes == (0xFFE1, ord("a"))
        assert keys.parse_key("shift_r+a").codes == (0xFFE2, ord("a"))
        assert keys.parse_key("ctrl+ctrl+a").codes == (0xFFE3, ord("a"))

    def unicode_keysyms_are_explicit() -> None:
        assert keys.keysyms("é") == [0xE9]
        assert keys.keysyms("€") == [0x010020AC]
        assert keys.keysyms("🙂") == [0x0101F642]
        # A non-ASCII capital does not imply a guessed guest Shift/layout.
        assert keys.keysyms("É") == [0xC9]
        assert keys.keysyms("ctrl+É") == [0xFFE3, 0xC9]
        assert keys.keysyms("ctrl+🙂") == [0xFFE3, 0x0101F642]

    def malformed_and_ambiguous_inputs_fail() -> None:
        invalid = (
            "", "   ", "\x00", "\n", "\t", "\x7f", "ctrl+", "+ctrl", "ctrl--a", "ctrl++a",
            "not-a-real-key", "a+a", "ctrl+a+A", "e\u0301", "\ud800", "\udfff",
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
    check("ascii_letters_keep_lowercase_without_modifier_shift", ascii_letters_keep_lowercase_without_modifier_shift)
    check("modifier_aliases_and_order", modifier_aliases_and_order)
    check("explicit_shift_semantics", explicit_shift_semantics)
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
