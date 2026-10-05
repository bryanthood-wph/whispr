"""Reasons a scored run must not start (B.9, G.3). The auth source is enforced on
every call by pipeline/models.py, which fails closed before the model runs."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pipeline.calls import canonical, config_file_sha
from pipeline.config import CONFIG_DIR, PLANTING_PATH
from pipeline.models import ModelCallError, cli_version, refused_env
from pipeline.prompts import sha256_text

REPO = Path(__file__).resolve().parent.parent
PREREG = REPO / "eval" / "PREREGISTRATION.md"
# A row of the preregistration's Hashes table: | `config/<relative path>` | `<sha256>` |
_HASH_ROW = re.compile(r"^\|\s*`config/([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|", re.MULTILINE)


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def registered_files(cfg: dict) -> list[str]:
    """Config-relative paths of the files the preregistration hashes (B.2): every
    configured prompt and schema, the ontology, and the planting seed data."""
    return [*cfg["prompts"].values(), *cfg["schemas"].values(), cfg["ontology"],
            PLANTING_PATH.relative_to(CONFIG_DIR).as_posix()]


def registered_hashes() -> dict[str, str]:
    """The Hashes table of eval/PREREGISTRATION.md: config-relative path -> sha256."""
    return dict(_HASH_ROW.findall(PREREG.read_text(encoding="utf-8")))


def hash_refusals(cfg: dict) -> list[str]:
    """One reason per registered file whose hash (calls.config_file_sha) differs from its
    Hashes-table row, or that has no row."""
    table, reasons = registered_hashes(), []
    for rel in registered_files(cfg):
        if rel not in table:
            reasons.append(f"{rel} has no row in the {PREREG.name} Hashes table (register it first)")
        elif config_file_sha(rel) != table[rel]:
            reasons.append(f"{rel} differs from its registered hash in {PREREG.name} "
                           "(re-register it, or the run is exploratory)")
    return reasons


def provenance(cfg: dict) -> dict:
    """The run's commit and tags, the hash of the effective (merged) config, and each
    registered file's hash as registered, so a run is tied to eval/PREREGISTRATION.md."""
    return {"commit": git("rev-parse", "HEAD"), "tags": git("tag", "--points-at", "HEAD").split(),
            "effective_config_sha256": sha256_text(canonical(cfg)),
            "config_sha256": {rel: config_file_sha(rel) for rel in registered_files(cfg)},
            "cli_version": _version_or_error(cfg)}


def _version_or_error(cfg: dict) -> str:
    try:
        return cli_version(cfg)
    except (ModelCallError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"unknown ({exc})"


def version_refusals(cfg: dict) -> list[str]:
    """A reason when the CLI does not report the pinned cli.version: roles left at a CLI
    default would run with that version's defaults instead of the measured ones."""
    found, pinned = _version_or_error(cfg), cfg["cli"]["version"]
    return [] if found == pinned else [
        f"the claude CLI reports {found}, but cli.version pins {pinned} (re-pilot, then amend cli.version)"]


def run_tag(cfg: dict, stage: str) -> str:
    return cfg["eval"]["run_tag"].format(stage=stage)


def refusals(cfg: dict, stage: str) -> list[str]:
    reasons = [f"{name} is set (nested Claude session; run from a scheduled task)" for name in refused_env(cfg)]
    if git("status", "--porcelain"):
        reasons.append("working tree is dirty (scored runs come from a clean, tagged commit)")
    tag = run_tag(cfg, stage)
    if tag not in git("tag", "--points-at", "HEAD").split():
        reasons.append(f"HEAD is not tagged {tag} (G.3)")
    return reasons + hash_refusals(cfg) + version_refusals(cfg)
