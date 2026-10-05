"""Cached, schema-validated model calls: the layer every structured call goes through
(extract here; reference, matcher and judges in the eval).

Cache key (D.1, B.9): sha256 of the exact request: the CLI's base and isolation arguments, model,
effort, system prompt, schema hash and the prompt actually sent, plus a replicate
index when it is nonzero. Base arguments are in it because they change how the CLI
runs (setting sources, tools), so output made under other arguments is no hit.
Repeated samples of one request (judge votes, the reference stability rerun) use
replicate 1, 2, ... so each is a real call rather than a cache hit of the first;
replicate 0 leaves the key exactly as it was. A prompt sent in two parts
(models.split_prompt) also keys the split point, so the same text sent whole is no hit;
an unsplit prompt's key is unchanged. The model is the configured name; an alias
such as "haiku" can resolve to a newer model later, so each entry records the model
the CLI actually ran. Only output that passes the schema is returned or cached.

`before_call(key, max_budget_usd)` runs only on a cache miss, from models.call
immediately before the CLI starts. The eval passes its budget guard here: it can stop
before spending, and it reserves the call's cap in the ledger under `key` until the
call's own row lands.

Calls may run on several threads (eval.workers). Two with the same key never run at
once: the second waits and is served from the first's cache entry, as it would be if
the calls ran one at a time.
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pipeline import models
from pipeline.config import config_file
from pipeline.prompts import canonical, sha256_text  # canonical re-exported: callers import it from here
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


def schema_sha(schema: dict) -> str:
    return sha256_text(canonical(schema))


def config_file_sha(relative: str) -> str:
    """sha256 of a config-relative file the way eval/PREREGISTRATION.md registers it: a
    JSON file in canonical form, any other file as UTF-8 text with CRLF converted to LF,
    so git's line-ending conversion on checkout never changes it."""
    path = config_file(relative)
    text = path.read_text(encoding="utf-8")
    return schema_sha(json.loads(text)) if path.suffix == ".json" else sha256_text(text.replace("\r\n", "\n"))


def request_key(cfg: dict, role: str, prompt: str, schema: dict, system_prompt: Optional[str],
                replicate: int = 0) -> str:
    spec = cfg["models"][role]
    request = {"base_args": models.cli_args(cfg), "model": spec["model"], "effort": spec["effort"],
               "system_prompt": system_prompt, "schema": schema_sha(schema), "prompt": prompt}
    if replicate:
        request["replicate"] = replicate
    if spec["thinking_tokens"] is not None:     # keyed only when set: a null budget keeps its key
        request["thinking_tokens"] = spec["thinking_tokens"]
    prefix, _ = models.split_prompt(cfg, prompt)
    if prefix is not None:          # delivered in two parts: a different request to the model
        request["system_prefix_chars"] = len(prefix)
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


_in_flight: dict[str, threading.Lock] = {}
_in_flight_lock = threading.Lock()


@contextlib.contextmanager
def _one_call_per_key(key: str):
    with _in_flight_lock:
        lock = _in_flight.setdefault(key, threading.Lock())
    with lock:
        yield


def cached_call(cfg: dict, role: str, prompt: str, *, schema: dict, max_budget_usd: float, ledger: Path,
                cache_dir: Optional[Path] = None, system_prompt: Optional[str] = None,
                before_call: Optional[Callable[[str, float], None]] = None,
                provenance: Optional[dict] = None, replicate: int = 0) -> Cached:
    """One structured call, served from the cache when possible. Raises OutputError for
    schema-invalid output; models.AuthError / ModelCallError propagate."""
    key = request_key(cfg, role, prompt, schema, system_prompt, replicate)
    with _one_call_per_key(key):
        return _cached_call(cfg, role, prompt, key, schema=schema, max_budget_usd=max_budget_usd, ledger=ledger,
                            cache_dir=cache_dir, system_prompt=system_prompt, before_call=before_call,
                            provenance=provenance)


def _cached_call(cfg: dict, role: str, prompt: str, key: str, *, schema: dict, max_budget_usd: float,
                 ledger: Path, cache_dir: Optional[Path], system_prompt: Optional[str],
                 before_call: Optional[Callable[[str, float], None]], provenance: Optional[dict]) -> Cached:
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
