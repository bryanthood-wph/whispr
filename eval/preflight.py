"""Reasons a scored run must not start (B.9, G.3). The auth source is enforced on
every call by pipeline/models.py, which fails closed before the model runs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.models import refused_env

REPO = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def provenance() -> dict:
    return {"commit": git("rev-parse", "HEAD"), "tags": git("tag", "--points-at", "HEAD").split()}


def run_tag(cfg: dict, stage: str) -> str:
    return cfg["eval"]["run_tag"].format(stage=stage)


def refusals(cfg: dict, stage: str) -> list[str]:
    reasons = [f"{name} is set (nested Claude session; run from a scheduled task)" for name in refused_env(cfg)]
    if git("status", "--porcelain"):
        reasons.append("working tree is dirty (scored runs come from a clean, tagged commit)")
    tag = run_tag(cfg, stage)
    if tag not in git("tag", "--points-at", "HEAD").split():
        reasons.append(f"HEAD is not tagged {tag} (G.3)")
    return reasons
