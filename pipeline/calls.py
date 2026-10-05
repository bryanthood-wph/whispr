"""Cached, schema-validated model calls: the layer every structured call goes through
(extract here; reference, matcher and judges in the eval).

Cache key (D.1, B.9): sha256 of the exact request: model, effort, system prompt,
schema hash and the prompt actually sent, plus a replicate index when it is nonzero.
Repeated samples of one request (judge votes, the reference stability rerun) use
replicate 1, 2, ... so each is a real call rather than a cache hit of the first;
replicate 0 leaves the key exactly as it was. The model is the configured name; an alias
such as "haiku" can resolve to a newer model later, so each entry records the model
the CLI actually ran. Only output that passes the schema is returned or cached.

`before_call(key, max_budget_usd)` runs only on a cache miss, from models.call
immediately before the CLI starts. The eval passes its budget guard here: it can stop
before spending, and it reserves the call's cap in the ledger under `key` until the
call's own row lands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pipeline import models
from pipeline.prompts import sha256_text
from pipeline.jsonschema_lite import validate
from whispr.fileio import atomic_write_text


class OutputError(RuntimeError):
    """The model returned output that fails its schema."""


@dataclass
class Cached:
    key: str
    output: dict
    cached: bool
    model: str
    auth_source: Optional[str]
    cost_usd: float


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_sha(schema: dict) -> str:
    return sha256_text(canonical(schema))


def request_key(cfg: dict, role: str, prompt: str, schema: dict, system_prompt: Optional[str],
                replicate: int = 0) -> str:
    spec = cfg["models"][role]
    request = {"model": spec["model"], "effort": spec["effort"], "system_prompt": system_prompt,
               "schema": schema_sha(schema), "prompt": prompt}
    if replicate:
        request["replicate"] = replicate
    return sha256_text(canonical(request))


def cache_path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / f"{key}.json"


def cached_entry(cache_dir: Optional[Path], key: str, schema: dict) -> Optional[dict]:
    """A cached entry, or None if absent, unreadable or no longer valid."""
    if not cache_dir:
        return None
    try:
        entry = json.loads(cache_path(cache_dir, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or validate(entry.get("output"), schema):
        return None
    return entry


def cached_call(cfg: dict, role: str, prompt: str, *, schema: dict, max_budget_usd: float, ledger: Path,
                cache_dir: Optional[Path] = None, system_prompt: Optional[str] = None,
                before_call: Optional[Callable[[str, float], None]] = None,
                provenance: Optional[dict] = None, replicate: int = 0) -> Cached:
    """One structured call, served from the cache when possible. Raises OutputError for
    schema-invalid output; models.AuthError / ModelCallError propagate."""
    key = request_key(cfg, role, prompt, schema, system_prompt, replicate)
    entry = cached_entry(cache_dir, key, schema)
    if entry:
        return Cached(key, entry["output"], True, entry["model"], entry["auth_source"], 0.0)
    result = models.call(cfg, role, prompt, max_budget_usd=max_budget_usd, json_schema=schema,
                         system_prompt=system_prompt, ledger=ledger, request_key=key,
                         before_launch=(lambda: before_call(key, max_budget_usd)) if before_call else None)
    errors = validate(result.structured, schema)
    if errors:
        raise OutputError(f"{role} output failed its schema:\n  " + "\n  ".join(errors[:20]))
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache_path(cache_dir, key), json.dumps({
            "key": key, "output": result.structured, "role": role, "model": result.model,
            "effort": result.effort, "auth_source": result.auth_source, "cost_usd": result.cost_usd,
            "ts": datetime.now(timezone.utc).isoformat(), **(provenance or {}),
        }, ensure_ascii=False, indent=1))
    return Cached(key, result.structured, False, result.model, result.auth_source, result.cost_usd)
