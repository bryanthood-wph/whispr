"""The extract step: one model call per prepared episode -> JSON validated against
config/schema/extract.json (docs/plan/D-architecture-and-ops.md D.1; lesson L18).

The call runs with --json-schema and no tools, through pipeline/models.py. Output is
validated again here; only valid output is returned or cached. Retrying a failed
item is the caller's job (the queue's max_attempts / quarantine, D.1), so this makes
at most one call.

Cache key (D.1): sha256 of the exact request: model, effort, system prompt, schema
hash and the rendered prompt. The model is the configured name; an alias such as
"haiku" can resolve to a newer model later, so each entry records the model the CLI
actually ran.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pipeline import models, prompts
from pipeline.config import config_file
from pipeline.jsonschema_lite import validate
from pipeline.prepare import Prepared


class ExtractError(RuntimeError):
    pass


@dataclass
class Request:
    role: str
    prompt: str
    system_prompt: Optional[str]
    schema: dict
    template_sha256: str
    schema_sha256: str
    key: str


@dataclass
class Extraction:
    key: str
    output: dict
    cached: bool
    model: str
    auth_source: Optional[str]
    cost_usd: float


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_schema(cfg: dict) -> dict:
    with open(config_file(cfg["schemas"]["extract"]), encoding="utf-8") as fh:
        return json.load(fh)


def prompt_values(prep: Prepared, cfg: dict) -> dict[str, str]:
    missing = cfg["extract"]["missing_value"]
    meta = prep.meta
    return {
        "CALL_TITLE": meta["call_title"] or missing,
        "DATE": meta["date"] or missing,
        "CALL_TYPE": meta["call_type"],
        "ORGANIZER": meta["organizer"] or missing,
        "ATTENDEES": cfg["extract"]["list_separator"].join(meta["attendees"]) or missing,
        "OWNER_NAME": cfg["owner"]["name"],
        "TRANSCRIPT": prep.render(),
    }


def build_request(prep: Prepared, cfg: dict, *, role: str, system_prompt: Optional[str] = None) -> Request:
    template = prompts.load(cfg["prompts"]["extract"])
    prompt = prompts.render(template, prompt_values(prep, cfg))
    schema = load_schema(cfg)
    schema_sha = prompts.sha256_text(_canonical(schema))
    spec = cfg["models"][role]
    key = hashlib.sha256(_canonical({
        "model": spec["model"], "effort": spec["effort"], "system_prompt": system_prompt,
        "schema": schema_sha, "prompt": prompt,
    }).encode("utf-8")).hexdigest()
    return Request(role, prompt, system_prompt, schema, prompts.sha256_text(template), schema_sha, key)


def _read_cache(path: Path, schema: dict) -> Optional[dict]:
    """A cached entry, or None if absent, unreadable or no longer valid."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or validate(entry.get("output"), schema):
        return None
    return entry


def _write_cache(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def extract(prep: Prepared, cfg: dict, *, role: str, max_budget_usd: float,
            system_prompt: Optional[str] = None, cache_dir: Optional[Path] = None,
            ledger: Optional[Path] = None) -> Extraction:
    """Extract one episode. Raises ExtractError for a stub or invalid output, and lets
    models.AuthError / ModelCallError propagate."""
    if prep.is_stub:
        raise ExtractError(f"stub episode ({prep.words} words): no model call")
    req = build_request(prep, cfg, role=role, system_prompt=system_prompt)
    cache_path = Path(cache_dir) / f"{req.key}.json" if cache_dir else None
    if cache_path:
        entry = _read_cache(cache_path, req.schema)
        if entry:
            return Extraction(req.key, entry["output"], True, entry["model"], entry["auth_source"], 0.0)

    result = models.call(cfg, role, req.prompt, max_budget_usd=max_budget_usd, json_schema=req.schema,
                         system_prompt=system_prompt, ledger=ledger, request_key=req.key)
    errors = validate(result.structured, req.schema)
    if errors:
        raise ExtractError(f"output failed {cfg['schemas']['extract']}:\n  " + "\n  ".join(errors[:20]))
    if cache_path:
        _write_cache(cache_path, {
            "key": req.key, "output": result.structured, "model": result.model, "effort": result.effort,
            "auth_source": result.auth_source, "cost_usd": result.cost_usd,
            "template_sha256": req.template_sha256, "schema_sha256": req.schema_sha256,
            "sources": prep.sources, "prepare_key": prep.key, "ts": datetime.now(timezone.utc).isoformat(),
        })
    return Extraction(req.key, result.structured, False, result.model, result.auth_source, result.cost_usd)
