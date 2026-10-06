"""Load config/defaults.yaml merged with the per-user overlay, and fail loudly on
anything malformed (docs/plan/D-architecture-and-ops.md D.2).

The overlay may only set keys that exist in defaults. After merging, the result is
validated against config/config.schema.json, so a missing overlay value (still null),
an unknown key or a wrong type stops the program at startup rather than mid-run. Names
one key must find in another (tasks.intake, check_intake) are checked then too.
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
# Planting seed data (eval judge calibration, B.5): part of the pre-registered eval, not a
# user tunable, so it sits next to defaults.yaml rather than behind a config key an overlay
# could change.
PLANTING_PATH = DEFAULTS_PATH.with_name("planting.yaml")


# Names another overlay for this process when set and non-empty. The plugin's MCP servers
# get it from the plugin's `config` option (plugin/.mcp.json), since a server's argv
# cannot leave out --config when that option is empty. Scheduled tasks never set it.
OVERLAY_ENV = "WHISPR_OVERLAY"


class ConfigError(ValueError):
    pass


def default_overlay_path() -> Path:
    named = os.environ.get(OVERLAY_ENV, "").strip()
    return Path(named) if named else Path(os.environ["APPDATA"]) / "whispr" / "config.yaml"


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


def read_yaml(path: Path) -> dict:
    """A YAML file that must hold a mapping (an empty file reads as {})."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a mapping")
    return data


def merged(overlay: dict) -> dict[str, Any]:
    """Defaults with `overlay` merged in, not yet validated: what /whispr-setup reads
    before the overlay is complete (pipeline/setup.py). An unknown key still fails."""
    return _merge(read_yaml(DEFAULTS_PATH), overlay)


def load_config(overlay_path: Optional[Path] = None, overlay: Optional[dict] = None) -> dict[str, Any]:
    """Return the validated config. `overlay` (a dict) is for tests and for setup's check
    of an overlay before it is written; otherwise the overlay file is read from
    `overlay_path` or the default %APPDATA% location."""
    if overlay is None:
        path = overlay_path or default_overlay_path()
        overlay = read_yaml(path) if path.exists() else {}
    cfg = merged(overlay)
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        schema = json.load(fh)
    errors = validate(cfg, schema)
    if errors:
        raise ConfigError("invalid config:\n  " + "\n  ".join(errors))
    check_intake(cfg)
    return cfg


def _folded(names: list[str], key: str) -> dict[str, str]:
    """Each name case-folded -> as written; two names alike but for letter case are a
    ConfigError, since every lookup ignores case."""
    folded: dict[str, str] = {}
    for name in names:
        if name.casefold() in folded:
            raise ConfigError(f"{key}: {folded[name.casefold()]!r} and {name!r} differ only in letter case")
        folded[name.casefold()] = name
    return folded


def intake_fields(cfg: dict, names: list[str], key: str) -> list[str]:
    """`names` as tasks.intake.fields spells them, matched with letter case ignored (as
    schedules.skip matches task names), each once. A name that is no field is a
    ConfigError naming it and `key`, so a typo can't leave a field out of the gate."""
    fields = _folded(list(cfg["tasks"]["intake"]["fields"]), "tasks.intake.fields")
    unknown = [n for n in names if n.casefold() not in fields]
    if unknown:
        raise ConfigError(f"tasks.intake.{key}: {unknown} not in tasks.intake.fields ({list(fields.values())})")
    return list(dict.fromkeys(fields[n.casefold()] for n in names))


def required_fields(cfg: dict) -> list[str]:
    """The fields the ready gate requires (tasks.intake.required_fields): never empty."""
    names = cfg["tasks"]["intake"]["required_fields"]
    if not names:
        raise ConfigError("tasks.intake.required_fields is empty: the ready gate would check nothing")
    return intake_fields(cfg, names, "required_fields")


def scope_field(cfg: dict) -> str:
    """The brief field that holds a task's scope (tasks.intake.scope_field)."""
    return intake_fields(cfg, [cfg["tasks"]["intake"]["scope_field"]], "scope_field")[0]


def check_intake(cfg: dict) -> None:
    """The intake's names must agree (docs/plan/task-intake-and-worker.md §3): fields and
    scope types defined, no two alike but for letter case, and required_fields and
    scope_field naming defined fields. Any break is a ConfigError at load."""
    intake = cfg["tasks"]["intake"]
    for key in ("fields", "scope_types"):
        if not intake[key]:
            raise ConfigError(f"tasks.intake.{key} is empty")
    _folded(list(intake["scope_types"]), "tasks.intake.scope_types")
    required_fields(cfg)
    scope_field(cfg)


def config_file(relative: str) -> Path:
    """Resolve a path held in config (prompts/, schema/, ontology) against config/."""
    return CONFIG_DIR / relative


def load_schema(cfg: dict, key: str) -> dict:
    """The JSON schema configured as schemas.<key>."""
    with open(config_file(cfg["schemas"][key]), encoding="utf-8") as fh:
        return json.load(fh)


def data_dir(cfg: dict, *parts: str) -> Path:
    """A folder under the per-user data dir, created on first use."""
    path = Path(cfg["paths"]["data_dir"]).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path

