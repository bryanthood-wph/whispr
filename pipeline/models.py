"""The only model-call path (docs/plan/D-architecture-and-ops.md D.3; lessons L3, L8).

Every call runs the Claude Code CLI in print mode with no tools, the prompt on stdin,
and the event stream on stdout. Before and during each call this module:
- refuses to start if a variable in auth.refuse_if_set is set (a nested session dies
  instantly with zero tokens: CLAUDE.md, observed 2026-08-03 and 2026-09-08)
- strips inherited CLAUDE*/ANTHROPIC* variables from the child only, so the call uses
  the user's sign-in rather than an API key
- reads the init event and kills the process if its apiKeySource is not approved,
  before the model is called
- passes --max-budget-usd, and appends model, effort, tokens, cost and auth source
  to the run ledger, including for calls that then fail (the money is spent either way)

The API path is deferred (README §3) and would sit behind this same `call` function.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pipeline.config import data_dir

# Where npm puts the native binary relative to its claude.cmd / claude.ps1 shim.
_NATIVE_FROM_SHIM = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"


class ModelCallError(RuntimeError):
    pass


class AuthError(ModelCallError):
    """The call would not run on an approved auth source. Fails closed."""


@dataclass
class CallResult:
    role: str
    model: str
    effort: Optional[str]
    auth_source: Optional[str]
    structured: Optional[dict]
    text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    duration_ms: int
    is_error: bool
    request_key: Optional[str] = None
    raw_result: dict = field(default_factory=dict, repr=False)


def resolve_executable(name: str) -> str:
    """A path to a directly runnable CLI. An npm shim (.cmd/.ps1) can't take our
    arguments safely through a shell, so prefer the native exe beside it."""
    if Path(name).is_file():
        return name
    found = shutil.which(name)
    if not found:
        raise ModelCallError(f"CLI {name!r} not found on PATH")
    if found.lower().endswith((".cmd", ".ps1", ".bat")):
        native = Path(found).parent / _NATIVE_FROM_SHIM
        if native.is_file():
            return str(native)
        raise ModelCallError(f"{found} is a shell shim and no native exe was found at {native}")
    return found


def child_env(cfg: dict) -> dict[str, str]:
    prefixes = tuple(cfg["auth"]["strip_env_prefixes"])
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(prefixes)}


def build_args(cfg: dict, role: str, json_schema: Optional[dict], system_prompt: Optional[str],
               max_budget_usd: float) -> list[str]:
    spec = cfg["models"][role]
    args = [resolve_executable(cfg["cli"]["executable"]), *cfg["cli"]["base_args"], "--model", spec["model"]]
    if spec["effort"]:
        args += ["--effort", spec["effort"]]
    if json_schema is not None:
        args += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
    if system_prompt is not None:
        args += ["--system-prompt", system_prompt]
    args += ["--max-budget-usd", f"{max_budget_usd:.2f}"]
    return args


def _append_ledger(ledger: Optional[Path], record: dict) -> None:
    if ledger is None:
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def call(cfg: dict, role: str, prompt: str, *, max_budget_usd: float,
         json_schema: Optional[dict] = None, system_prompt: Optional[str] = None,
         ledger: Optional[Path] = None, request_key: Optional[str] = None) -> CallResult:
    """Run one model call for `role`. Raises AuthError / ModelCallError on failure."""
    for name in cfg["auth"]["refuse_if_set"]:
        if os.environ.get(name):
            raise ModelCallError(f"refusing to call the model: {name} is set (nested Claude session)")

    args = build_args(cfg, role, json_schema, system_prompt, max_budget_usd)
    spec = cfg["models"][role]
    approved = set(cfg["auth"]["approved_sources"])
    started = time.monotonic()
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=data_dir(cfg, "work"), env=child_env(cfg), text=True, encoding="utf-8", errors="replace",
    )
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(cfg["cli"]["timeout_s"], _kill_on_timeout)
    timer.start()
    stderr_chunks: list[str] = []
    drain = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    drain.start()
    auth_source: Optional[str] = None
    model = spec["model"]
    result: dict[str, Any] = {}
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                auth_source = event.get("apiKeySource")
                model = event.get("model") or model
                if auth_source not in approved:
                    proc.kill()
                    raise AuthError(f"auth source {auth_source!r} is not approved {sorted(approved)}; call killed before the model ran")
            elif event.get("type") == "result":
                result = event
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        drain.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()

    usage = result.get("usage") or {}
    out = CallResult(
        role=role, model=model, effort=spec["effort"], auth_source=auth_source,
        structured=result.get("structured_output"), text=result.get("result") or "",
        cost_usd=float(result.get("total_cost_usd") or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0), output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        duration_ms=int((time.monotonic() - started) * 1000),
        is_error=bool(result.get("is_error")) or not result or proc.returncode != 0,
        request_key=request_key, raw_result=result,
    )
    record = {k: v for k, v in asdict(out).items() if k not in ("structured", "text", "raw_result")}
    record["ts"] = datetime.now(timezone.utc).isoformat()
    _append_ledger(ledger, record)

    if timed_out.is_set():
        raise ModelCallError(f"{role} call timed out after {cfg['cli']['timeout_s']}s")
    if out.is_error:
        tail = "".join(stderr_chunks)[-500:]
        raise ModelCallError(f"{role} call failed (exit {proc.returncode}): {out.text[:300]!r} {tail!r}")
    if json_schema is not None and out.structured is None:
        raise ModelCallError(f"{role} call returned no structured_output")
    return out
