"""Config loader. Reads config.yaml from the repo root; resolves path keys to
absolute paths and creates the directories. No literals from config are duplicated
in code — modules read them through the returned dict.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Repo root = parent of the whispr package directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


def load_config(config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load and normalize config.

    - Resolves paths.* to absolute Paths (relative entries are relative to REPO_ROOT).
    - Creates the transcripts/recordings/logs directories if missing.
    """
    path = Path(config_path) if config_path else CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    paths = cfg.setdefault("paths", {})
    for key, default in (("transcripts", "transcripts"), ("recordings", "recordings"), ("logs", "logs")):
        raw = Path(paths.get(key, default))
        resolved = raw if raw.is_absolute() else (REPO_ROOT / raw)
        resolved.mkdir(parents=True, exist_ok=True)
        paths[key] = resolved

    # Optional: a bundled faster-whisper model cache (installer-populated distributions
    # only). Absent in Colby's own config.yaml, so dev-machine behavior — resolve via
    # faster-whisper's default HuggingFace cache — is unchanged.
    tcfg = cfg.get("transcription")
    if tcfg and tcfg.get("model_cache_dir"):
        raw = Path(tcfg["model_cache_dir"])
        tcfg["model_cache_dir"] = raw if raw.is_absolute() else (REPO_ROOT / raw)

    return cfg
