"""Load config/defaults.yaml merged with the per-user overlay, and fail loudly on
anything malformed (docs/plan/D-architecture-and-ops.md D.2).

The overlay may only set keys that exist in defaults. After merging, the result is
validated against config/config.schema.json, so a missing overlay value (still null),
an unknown key or a wrong type stops the program at startup rather than mid-run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from pipeline.jsonschema_lite import validate

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULTS_PATH = CONFIG_DIR / "defaults.yaml"
SCHEMA_PATH = CONFIG_DIR / "config.schema.json"


class ConfigError(ValueError):
    pass


def default_overlay_path() -> Path:
    return Path(os.environ["APPDATA"]) / "whispr" / "config.yaml"


def _merge(base: dict, overlay: dict, path: str = "") -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        where = f"{path}.{key}" if path else key
        if key not in base:
            raise ConfigError(f"overlay sets unknown key {where!r}")
        if isinstance(base[key], dict) and isinstance(value, dict):
            merged[key] = _merge(base[key], value, where)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a mapping")
    return data


def load_config(overlay_path: Optional[Path] = None, overlay: Optional[dict] = None) -> dict[str, Any]:
    """Return the validated config. `overlay` (a dict) is for tests; otherwise the
    overlay file is read from `overlay_path` or the default %APPDATA% location."""
    cfg = _read_yaml(DEFAULTS_PATH)
    if overlay is None:
        path = overlay_path or default_overlay_path()
        overlay = _read_yaml(path) if path.exists() else {}
    cfg = _merge(cfg, overlay)
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        schema = json.load(fh)
    errors = validate(cfg, schema)
    if errors:
        raise ConfigError("invalid config:\n  " + "\n  ".join(errors))
    return cfg


def config_file(relative: str) -> Path:
    """Resolve a path held in config (prompts/, schema/, ontology) against config/."""
    return CONFIG_DIR / relative


def data_dir(cfg: dict, *parts: str) -> Path:
    """A folder under the per-user data dir, created on first use."""
    path = Path(cfg["paths"]["data_dir"]).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path

