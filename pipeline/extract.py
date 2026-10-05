"""The extract step: one model call per prepared episode -> JSON validated against
config/schema/extract.json (docs/plan/D-architecture-and-ops.md D.1; lesson L18).

The call runs with --json-schema and no tools, through pipeline/calls.py (cache +
schema validation) and pipeline/models.py. Retrying a failed item is the caller's
job (the queue's max_attempts / quarantine, D.1), so this makes at most one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pipeline import calls, config, prompts
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


def load_schema(cfg: dict) -> dict:
    return config.load_schema(cfg, "extract")


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
    return Request(role, prompt, system_prompt, schema, prompts.sha256_text(template),
                   calls.schema_sha(schema),
                   calls.request_key(cfg, role, prompt, schema, system_prompt))


def extract(prep: Prepared, cfg: dict, *, role: str, max_budget_usd: float, ledger: Path,
            system_prompt: Optional[str] = None, cache_dir: Optional[Path] = None,
            before_call: Optional[Callable[[str, float], None]] = None) -> calls.Cached:
    """Extract one episode. Raises ExtractError for a stub or invalid output, and lets
    models.AuthError / ModelCallError propagate."""
    if prep.is_stub:
        raise ExtractError(f"stub episode ({prep.words} words): no model call")
    req = build_request(prep, cfg, role=role, system_prompt=system_prompt)
    try:
        return calls.cached_call(
            cfg, role, req.prompt, schema=req.schema, max_budget_usd=max_budget_usd, ledger=ledger,
            cache_dir=cache_dir, system_prompt=system_prompt, before_call=before_call,
            provenance={"template_sha256": req.template_sha256, "schema_sha256": req.schema_sha256,
                        "sources": prep.sources, "prepare_key": prep.key},
        )
    except calls.OutputError as exc:
        raise ExtractError(str(exc)) from exc
