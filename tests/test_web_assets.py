"""Static web-asset sanity checks: locales, themes, contrast.

These run offline and protect the two "pure data" promises of M5:
every locale file speaks about the same keys, and every theme fills the same
variable set at readable contrast.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "council" / "web" / "static"

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

REQUIRED_VARS = {
    "--bg",
    "--bg-elev",
    "--bg-panel",
    "--border",
    "--text",
    "--text-dim",
    "--text-faint",
    "--accent",
    "--accent-contrast",
    "--accent-soft",
    "--ok",
    "--ok-soft",
    "--warn",
    "--warn-soft",
    "--err",
    "--err-soft",
    "--code-bg",
    "--code-text",
}

# (foreground var, background var, minimum WCAG ratio)
CONTRAST_PAIRS = [
    ("--text", "--bg", 4.5),
    ("--text-dim", "--bg", 3.0),
    ("--accent-contrast", "--accent", 3.0),
    ("--ok", "--bg", 3.0),
    ("--warn", "--bg", 3.0),
    ("--err", "--bg", 3.0),
]


def _load(path: str) -> dict[str, object]:
    return json.loads((ASSETS / path).read_text(encoding="utf-8"))


def _channel(value: str) -> float:
    value = value[1:]
    if len(value) in (3, 4):
        value = "".join(ch * 2 for ch in value)
    r, g, b, *_ = (int(value[i : i + 2], 16) for i in range(0, 6, 2))
    return (r, g, b)


def _linear(channel: int) -> float:
    scaled = channel / 255.0
    return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    r, g, b = _channel(color)
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def test_locales_are_key_symmetric() -> None:
    locales = ["zh", "en"]
    key_sets: dict[str, set[str]] = {}
    for lang in locales:
        data = _load(f"i18n/{lang}.json")
        keys = set(data)
        assert keys, lang
        key_sets[lang] = keys
        assert all(isinstance(value, str) and value for value in data.values()), lang
    assert key_sets["zh"] == key_sets["en"]


def test_themes_fill_every_required_variable() -> None:
    theme_dir = ASSETS / "themes"
    files = sorted(path.name for path in theme_dir.glob("*.json"))
    assert {"light.json", "dark.json", "high-contrast.json"} <= set(files)
    for name in files:
        theme = _load(f"themes/{name}")
        assert theme["mode"] in ("light", "dark"), name
        colors = theme["colors"]
        assert isinstance(colors, dict)
        missing = REQUIRED_VARS - set(colors)
        assert not missing, f"{name}: 缺少 {sorted(missing)}"
        for key, value in colors.items():
            assert isinstance(value, str) and _HEX.match(value), f"{name}: {key}={value!r}"
        extra = set(colors) - REQUIRED_VARS
        assert not extra, f"{name}: 多余的变量 {sorted(extra)}"


def test_theme_contrast_meets_wcag_floor() -> None:
    for name in ("light.json", "dark.json", "high-contrast.json"):
        theme = _load(f"themes/{name}")
        colors = theme["colors"]
        failures = []
        for fg_var, bg_var, floor in CONTRAST_PAIRS:
            fg, bg = colors[fg_var], colors[bg_var]
            ratio = _contrast(fg, bg)
            if ratio < floor:
                failures.append(f"{fg_var} on {bg_var}: {ratio:.2f} < {floor}")
        assert not failures, f"{name}:\n" + "\n".join(failures)


def test_index_references_existing_assets() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    for href in ("style.css", "app.js"):
        assert href in html
    for lang in ("zh", "en"):
        assert (ASSETS / "i18n" / f"{lang}.json").is_file()
