"""The only model-call path (docs/plan/D-architecture-and-ops.md D.3; lessons L3, L8).

Every call runs the Claude Code CLI in print mode with no tools, the prompt on stdin,
and the event stream on stdout. Before and during each call this module:
- refuses to start if a variable in auth.refuse_if_set is set (a nested session dies
  instantly with zero tokens: CLAUDE.md, observed 2026-08-03 and 2026-09-08)
- strips inherited CLAUDE*/ANTHROPIC* variables from the child only, so the call uses
  the user's sign-in rather than an API key
- reads the init event and kills the process if its apiKeySource is not approved,
  before the model is called
- fails closed if the stream never reports an auth source at all
- passes --max-budget-usd, and appends model, effort, tokens, cost and auth source
  (plus the turn count, the result text's length and any usage fields it does not
  parse, which show where output tokens went) to the run ledger for every launched call, including ones that fail or are killed
  (the money may be spent either way). A call with no result event is recorded at
  its budget cap, flagged as an upper bound, so the eval budget never undercounts,
  unless the CLI exited on its own without emitting a single event: events start
  before any API request, so such a call (rejected arguments, say) spent nothing.
- sends a prompt's shared prefix (a transcript many calls repeat) as the CLI's appended
  system prompt, which the CLI caches, and only the per-call rest over stdin
  (`split_prompt`)

The API path is deferred (README §3) and would sit behind this same `call` function.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pipeline.config import data_dir

# Where npm puts the native binary relative to its claude.cmd / claude.ps1 shim.
_NATIVE_FROM_SHIM = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"

# Held for every append_jsonl write: calls can run on several threads (eval.workers). The
# eval's budget guard also holds it from reading spend to booking its reservation.
JSONL_LOCK = threading.RLock()


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
    cost_is_upper_bound: bool = False
    num_turns: int = 0
    result_chars: int = 0             # the result text: prose beside a ~20-token structured answer
    usage_other: dict = field(default_factory=dict)   # usage fields not parsed above (thinking, say)
    raw_result: dict = field(default_factory=dict, repr=False)


# The result event's usage fields CallResult parses; the rest go to `usage_other`.
_PARSED_USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

# The token counts a ledger row carries (a CallResult's fields, so a new one shows up here).
TOKEN_FIELDS = tuple(f.name for f in fields(CallResult) if f.name.endswith("_tokens"))


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


def child_env(cfg: dict, role: Optional[str] = None) -> dict[str, str]:
    """os.environ less the auth.strip_env_prefixes variables and cli.thinking_tokens_env
    (never inherited), plus `role`'s thinking budget (models.<role>.thinking_tokens) in
    cli.thinking_tokens_env when it is set."""
    prefixes = (*cfg["auth"]["strip_env_prefixes"], cfg["cli"]["thinking_tokens_env"])
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(prefixes)}
    thinking = cfg["models"][role]["thinking_tokens"] if role else None
    if thinking is not None:
        env[cfg["cli"]["thinking_tokens_env"]] = str(thinking)
    return env


def split_prompt(cfg: dict, prompt: str) -> tuple[Optional[str], str]:
    """(the prompt's shared prefix, or None; the rest). `claude -p` sends stdin as one user
    block cached only at its end, so a prefix many calls share would be re-written to the
    prompt cache on every call (pilot 2026-10-05: each presence verdict wrote ~8k tokens
    and read ~2k); the CLI does cache its system prompt. The split is before the last line
    equal to one of cli.cached_prefix_until (a template's first per-call section, after
    its shared text); a prompt without one, or starting with one, is not split."""
    lines = prompt.splitlines(keepends=True)
    marks = set(cfg["cli"]["cached_prefix_until"])
    at = max((i for i, line in enumerate(lines) if line.rstrip("\r\n") in marks), default=0)
    if not at:
        return None, prompt
    return "".join(lines[:at]), "".join(lines[at:])


def cli_args(cfg: dict) -> list[str]:
    """The arguments every call passes before its own: cli.base_args, then cli.isolation_args."""
    return [*cfg["cli"]["base_args"], *cfg["cli"]["isolation_args"]]


def build_args(cfg: dict, role: str, json_schema: Optional[dict], system_prompt: Optional[str],
               max_budget_usd: float, system_append: Optional[Path] = None) -> list[str]:
    spec = cfg["models"][role]
    args = [resolve_executable(cfg["cli"]["executable"]), *cli_args(cfg), "--model", spec["model"]]
    if spec["effort"]:
        args += ["--effort", spec["effort"]]
    if json_schema is not None:
        # The CLI validates --json-schema against its own default draft and rejects a
        # "$schema" naming any other (2026-10-05: "no schema with key or ref
        # .../draft/2020-12/schema"). The declaration only names the dialect; local
        # validation and request keys still use the schema file unchanged.
        cli_schema = {k: v for k, v in json_schema.items() if k != "$schema"}
        args += ["--json-schema", json.dumps(cli_schema, separators=(",", ":"))]
    if system_prompt is not None:
        args += ["--system-prompt", system_prompt]
    if system_append is not None:     # a file: a transcript can pass Windows' 32k command-line limit
        args += ["--append-system-prompt-file", str(system_append)]
    args += ["--max-budget-usd", f"{max_budget_usd:.2f}"]
    return args


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line (the call ledger and the eval's run records)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_LOCK, open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def read_jsonl_counted(path: Path) -> tuple[list[dict], int]:
    """(the readable rows of a JSONL `append_jsonl` wrote, unreadable line count). A
    missing file is empty; a torn line (a process killed mid-append) or a non-object
    line is skipped and counted, never fatal."""
    if not path.exists():
        return [], 0
    rows, bad = [], 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def read_jsonl(path: Path) -> list[dict]:
    """The readable rows of a JSONL `append_jsonl` wrote (see read_jsonl_counted)."""
    return read_jsonl_counted(path)[0]


def refused_env(cfg: dict) -> list[str]:
    """Set variables that mark a nested Claude session; a model call refuses while any is set."""
    return [name for name in cfg["auth"]["refuse_if_set"] if os.environ.get(name)]


def call(cfg: dict, role: str, prompt: str, *, max_budget_usd: float,
         ledger: Path, json_schema: Optional[dict] = None, system_prompt: Optional[str] = None,
         request_key: Optional[str] = None,
         before_launch: Optional[Callable[[], None]] = None) -> CallResult:
    """Run one model call for `role`. Raises AuthError / ModelCallError on failure.
    `ledger` is required: no paid call may go unrecorded. `before_launch` runs after
    every pre-launch check, immediately before the CLI starts (the eval's budget guard
    reserves there, so a call refused before launch never leaves a reservation); once
    it has run, a ledger row is always written, even if the CLI fails to start."""
    if refused := refused_env(cfg):
        raise ModelCallError(f"refusing to call the model: {refused[0]} is set (nested Claude session)")
    launch = dict(max_budget_usd=max_budget_usd, ledger=ledger, json_schema=json_schema,
                  system_prompt=system_prompt, request_key=request_key, before_launch=before_launch)
    prefix, prompt = split_prompt(cfg, prompt)
    if prefix is None:
        return _launch(cfg, role, prompt, None, **launch)
    # Beside the CLI's working directory, not in it: nothing per call where the CLI looks.
    fd, name = tempfile.mkstemp(prefix="system-", suffix=".md", dir=data_dir(cfg, "prompt-parts"))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(prefix)
        return _launch(cfg, role, prompt, Path(name), **launch)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def _launch(cfg: dict, role: str, prompt: str, system_append: Optional[Path], *, max_budget_usd: float,
            ledger: Path, json_schema: Optional[dict], system_prompt: Optional[str],
            request_key: Optional[str], before_launch: Optional[Callable[[], None]]) -> CallResult:
    """`call` once the prompt is split: `prompt` is the part sent over stdin."""
    args = build_args(cfg, role, json_schema, system_prompt, max_budget_usd, system_append)
    spec = cfg["models"][role]
    approved = set(cfg["auth"]["approved_sources"])
    if before_launch:
        before_launch()
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=data_dir(cfg, "work"), env=child_env(cfg, role), text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:            # nothing ran, so nothing was spent; the row settles any reservation
        append_jsonl(ledger, {"role": role, "model": spec["model"], "request_key": request_key, "cost_usd": 0.0,
                              "is_error": True, "error": f"launch failed: {exc}",
                              "ts": datetime.now(timezone.utc).isoformat()})
        raise ModelCallError(f"{role} call could not start the CLI: {exc}") from exc
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
    streamed = False                  # any event arrived: the CLI got past its arguments and may have spent
    model = spec["model"]
    result: dict[str, Any] = {}
    failure: Optional[ModelCallError] = None
    try:
        send_error: Optional[OSError] = None
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError as exc:  # the child exited before reading its prompt (its stderr says why)
            send_error = exc
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            streamed = True
            if event.get("type") == "system" and event.get("subtype") == "init":
                auth_source = event.get("apiKeySource")
                model = event.get("model") or model
                if auth_source not in approved:
                    proc.kill()
                    failure = AuthError(f"auth source {auth_source!r} is not approved {sorted(approved)}; call killed")
                    break
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

    tail = "".join(stderr_chunks)[-500:]
    if failure is None and send_error is not None:
        failure = ModelCallError(f"{role} call: could not send the prompt ({send_error}) {tail!r}")
    if failure is None and timed_out.is_set():
        failure = ModelCallError(f"{role} call timed out after {cfg['cli']['timeout_s']}s")
    if failure is None and result and auth_source is None:
        failure = AuthError(f"{role} call reported no auth source (no init event); failing closed")
    usage = result.get("usage") or {}
    has_cost = "total_cost_usd" in result
    spent_nothing = not result and not streamed and not timed_out.is_set()
    out = CallResult(
        role=role, model=model, effort=spec["effort"], auth_source=auth_source,
        structured=result.get("structured_output"), text=result.get("result") or "",
        cost_usd=float(result["total_cost_usd"]) if has_cost else 0.0 if spent_nothing else max_budget_usd,
        input_tokens=int(usage.get("input_tokens") or 0), output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        num_turns=int(result.get("num_turns") or 0), result_chars=len(result.get("result") or ""),
        usage_other={k: v for k, v in usage.items() if k not in _PARSED_USAGE},
        duration_ms=int((time.monotonic() - started) * 1000),
        is_error=bool(result.get("is_error")) or not result or proc.returncode != 0,
        request_key=request_key, raw_result=result, cost_is_upper_bound=not has_cost and not spent_nothing,
    )
    record = {k: v for k, v in asdict(out).items() if k not in ("structured", "text", "raw_result")}
    record["ts"] = datetime.now(timezone.utc).isoformat()
    append_jsonl(ledger, record)

    if failure is not None:
        raise failure
    if out.is_error:
        raise ModelCallError(f"{role} call failed (exit {proc.returncode}): {out.text[:300]!r} {tail!r}")
    if json_schema is not None and out.structured is None:
        raise ModelCallError(f"{role} call returned no structured_output")
    return out
