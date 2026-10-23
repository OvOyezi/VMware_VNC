# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Canonical keyboard chord parsing for RFB key events.

Key chords are represented as physical X11/RFB keysyms. Uppercase ASCII and
the legacy shifted-punctuation set carry an implicit left Shift, matching
VMware console behavior. Unicode characters are sent as Unicode keysyms
directly; this module does not infer a guest keyboard layout.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_BASE_KEYS = {
    "bsp": 0xFF08, "tab": 0xFF09, "enter": 0xFF0D, "esc": 0xFF1B,
    "ins": 0xFF63, "delete": 0xFFFF, "del": 0xFFFF, "home": 0xFF50,
    "end": 0xFF57, "left": 0xFF51, "up": 0xFF52, "right": 0xFF53,
    "down": 0xFF54, "pgup": 0xFF55, "pgdn": 0xFF56, "space": 0x20,
    "shift": 0xFFE1, "shift_r": 0xFFE2, "ctrl": 0xFFE3, "ctrl_r": 0xFFE4,
    "alt": 0xFFE9, "alt_r": 0xFFEA, "meta": 0xFFEB, "super": 0xFFEB,
    "super_r": 0xFFEC, "capslock": 0xFFE5, "numlock": 0xFF7F,
    "scrolllock": 0xFF14, "print": 0xFF61, "sysrq": 0xFF15,
    "pause": 0xFF13, "break": 0xFF6B, "menu": 0xFF67,
    "minus": 0x2D, "equal": 0x3D, "slash": 0x2F, "backslash": 0x5C,
    # A named form makes a literal plus unambiguous inside canonical chords.
    "plus": 0x2B,
}
_BASE_KEYS.update({f"f{index}": 0xFFBD + index for index in range(1, 36)})

# Preserve the historical public map, including its original aliases.
KEYS: dict[str, int] = dict(_BASE_KEYS)
KEYS.update({
    "backspace": 0xFF08,
    "escape": 0xFF1B,
    "return": 0xFF0D,
    "insert": 0xFF63,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "windows": 0xFFEB,
    "win": 0xFFEB,
})

_ALIASES = {
    "backspace": "bsp",
    "del": "delete",
    "escape": "esc",
    "insert": "ins",
    "meta": "super",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "return": "enter",
    "win": "super",
    "windows": "super",
}

_MODIFIER_ORDER = (
    "ctrl", "ctrl_r", "alt", "alt_r", "shift", "shift_r", "super", "super_r",
)
_MODIFIER_CODES = {KEYS[name] for name in _MODIFIER_ORDER}
_MODIFIER_NAME_BY_CODE = {KEYS[name]: name for name in _MODIFIER_ORDER}
_MODIFIER_RANK = {name: index for index, name in enumerate(_MODIFIER_ORDER)}
_SHIFTED_ASCII = frozenset('~!@#$%^&*()_+{}|:"<>?')
_UNICODE_KEYS_PREFIX = 0x01000000


@dataclass(frozen=True)
class KeyChord:
    """An immutable, canonical sequence of keysym-down events.

    ``components`` contains physical keysym values, so aliases and implicit
    modifiers compare consistently when a caller checks held-key overlap.
    ``release_codes`` is the corresponding reverse key-up order.
    """

    name: str
    codes: tuple[int, ...]
    components: frozenset[int]

    @property
    def release_codes(self) -> tuple[int, ...]:
        return tuple(reversed(self.codes))


@dataclass(frozen=True)
class _Atom:
    name: str
    code: int
    implicit_shift: bool = False


def _validate_text(key: str) -> str:
    if not isinstance(key, str):
        raise ValueError("key must be a string")
    if "\x00" in key:
        raise ValueError("key must not contain NUL")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
        raise ValueError("key must not contain surrogate code points")
    if any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in key):
        raise ValueError("use named keys for control characters")
    if len(key) == 1:
        return key
    value = key.strip()
    if not value:
        raise ValueError("key must not be empty")
    # Spaces around separators are harmless input formatting. Whitespace in a
    # name is never a key name; the space key has the explicit name "space".
    value = re.sub(r"\s*([+-])\s*", r"\1", value)
    if any(character.isspace() for character in value):
        raise ValueError("key chord contains whitespace inside a key name")
    return value


def _split_chord(value: str) -> list[str]:
    if len(value) == 1 and value in "+-":
        return [value]

    # A doubled terminal separator denotes the literal second punctuation
    # key: ctrl++ is Ctrl plus, ctrl+- is Ctrl minus, and ctrl-+ is also
    # Ctrl plus. Named "plus" / "minus" forms are available as well.
    if len(value) >= 2 and value[-1] in "+-" and value[-2] in "+-":
        prefix = value[:-2]
        if not prefix:
            raise ValueError("malformed key chord")
        parts = re.split(r"[+-]", prefix)
        if any(not part for part in parts):
            raise ValueError("malformed key chord")
        return [*parts, value[-1]]

    if "+" not in value and "-" not in value:
        return [value]
    parts = re.split(r"[+-]", value)
    if any(not part for part in parts):
        raise ValueError("malformed key chord")
    return parts


def _atom(value: str) -> _Atom:
    if value in {"+", "-"}:
        canonical = "plus" if value == "+" else "minus"
        return _Atom(value, KEYS[canonical], value == "+")

    if len(value) == 1:
        if value == " ":
            return _Atom("space", KEYS["space"])
        codepoint = ord(value)
        code = codepoint if codepoint <= 0xFF else _UNICODE_KEYS_PREFIX | codepoint
        shifted = ("A" <= value <= "Z") or value in _SHIFTED_ASCII
        return _Atom(value, code, shifted)

    lowered = value.lower()
    canonical = _ALIASES.get(lowered, lowered)
    if canonical not in KEYS:
        raise ValueError(f"unknown key name: {value[:40]}")
    if KEYS[canonical] in _MODIFIER_CODES:
        canonical = _MODIFIER_NAME_BY_CODE[KEYS[canonical]]
    return _Atom(canonical, KEYS[canonical], canonical == "plus")


def parse_key(key: str) -> KeyChord:
    """Parse a key name or +/- chord into canonical physical keysyms."""
    value = _validate_text(key)
    atoms = [_atom(part) for part in _split_chord(value)]

    modifiers: dict[int, str] = {}
    nonmodifiers: list[_Atom] = []
    nonmodifier_codes: set[int] = set()
    needs_shift = False
    for atom in atoms:
        if atom.code in _MODIFIER_CODES:
            # Modifier aliases can map to the same physical keysym.
            modifiers.setdefault(atom.code, _MODIFIER_NAME_BY_CODE[atom.code])
        else:
            if atom.code in nonmodifier_codes:
                raise ValueError("duplicate non-modifier key in chord")
            nonmodifier_codes.add(atom.code)
            nonmodifiers.append(atom)
            needs_shift = needs_shift or atom.implicit_shift

    if needs_shift and not any(
        code in {KEYS["shift"], KEYS["shift_r"]} for code in modifiers
    ):
        modifiers[KEYS["shift"]] = "shift"

    ordered_modifiers = sorted(
        ((name, code) for code, name in modifiers.items()),
        key=lambda item: _MODIFIER_RANK[item[0]],
    )
    # A chord is a simultaneous set of keysyms. Sort its non-modifier keys by
    # code so equivalent input order produces the same wire sequence and name.
    ordered_nonmodifiers = sorted(nonmodifiers, key=lambda atom: (atom.code, atom.name))
    codes = tuple(
        [code for _name, code in ordered_modifiers]
        + [atom.code for atom in ordered_nonmodifiers]
    )
    # The shifted character itself already names its implicit left Shift.
    # Hiding that redundant modifier also makes `A` and `shift+A` canonicalize
    # to the same name as well as the same physical keysym sequence.
    hidden_shift = (
        needs_shift
        and KEYS["shift"] in modifiers
        and KEYS["shift_r"] not in modifiers
    )
    names = [
        name for name, code in ordered_modifiers
        if not (hidden_shift and code == KEYS["shift"])
    ]
    names.extend(atom.name for atom in ordered_nonmodifiers)
    if len(names) == 1:
        canonical_name = {"plus": "+", "minus": "-"}.get(names[0], names[0])
    else:
        # Inside a chord, spell the literal separator keys as names so the
        # canonical form remains parseable (for example, ctrl-plus).
        parts = [{"+": "plus", "-": "minus"}.get(name, name) for name in names]
        canonical_name = "-".join(parts)
    return KeyChord(canonical_name, codes, frozenset(codes))


def normalize_key(key: str) -> str:
    """Return the canonical name accepted by :func:`parse_key`."""
    return parse_key(key).name


def keysyms(key: str) -> list[int]:
    """Return ordered key-down keysyms for a key or chord."""
    return list(parse_key(key).codes)
