"""P2b: is whispr's knowledge used first? (docs/plan/task-intake-and-worker.md §7)

`python -m eval graph-first [--dry-run | --rescore RUN_ID]`, configured by
eval.graph_first. For each arm (eval.graph_first.arms, in order) and each agreed question
(config/<eval.graph_first.questions>), it runs one fresh live `claude -p` session through
pipeline/models.py: the session's model and effort come from models.<role>, its budget
from max_budget_per_session_usd, and its stream-json events are kept. The arms differ
only in graph_first_note.switch_env, which turns the SessionStart hook's graph-first note
off (before) or on (after). Everything else is as a live session has it: your settings,
plugins, hooks and MCP servers load, the plugin from plugin_dirs_env, in working_dir. The
tools are read-only: --tools builtin_tools, --allowedTools allowed_tools,
--disallowedTools disallowed_tools, and --permission-mode dontAsk in cli_args, so nothing
outside the allowlist runs.

A session passes when its first knowledge lookup (the first graph tool, other knowledge
tool, knowledge skill or file read over notes or transcripts) is a graph tool, and its
answer quotes text that a graph tool returned in that session (`score_session`, a pure
function). The scored arm passes when at least pass_min of its sessions pass; the other
arms are baselines.

Writes, under <data_dir>/eval/graph-first/: ledger.jsonl (models.py's row for every
session; spend is checked against cap_usd before each one) and results/<run id>/ with
run.json (plan and provenance), raw/<arm>-<question id>.jsonl (each session's stream
events), scored.jsonl (one row per session) and summary.json.

Spends money only from a one-shot scheduled task (B.9, G.3): models.py refuses inside a
Claude session. The dry run makes no model call; it prints the plan, the cost estimate
and its basis (ledger history for this role and model, else cost_seed_usd), and what the
hook gives each arm (the hook run as Claude Code runs it, which reads alerts read-only).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from eval import preflight
from pipeline import models
from pipeline.config import config_file, data_dir, read_yaml
from whispr.fileio import atomic_write_text

DIR = ("eval", "graph-first")
LEDGER_FILE = "ledger.jsonl"
SCORED_FILE = "scored.jsonl"
SUMMARY_FILE = "summary.json"
RUN_FILE = "run.json"
RAW_DIR = "raw"
HOOK = Path("hooks") / "session_start.py"           # under the plugin folder
HOOK_EVENT = {"hook_event_name": "SessionStart", "source": "startup"}
EXIT_PASS, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2

GRAPH, OTHER = "graph", "other"                     # knowledge-lookup kinds
_ELLIPSIS = re.compile(r"\.\.\.|…|\[\.\.\.\]")
# Quoted spans in an answer: straight or curly double quotes, and blockquote lines.
_QUOTED = (re.compile(r'"([^"\n]+)"'), re.compile(r"“([^”]+)”"), re.compile(r"^\s*>\s?(.+)$", re.MULTILINE))


# --- config ------------------------------------------------------------------------

def settings(cfg: dict) -> dict:
    return cfg["eval"]["graph_first"]


@dataclass(frozen=True)
class Question:
    id: str
    text: str


def questions(cfg: dict) -> list[Question]:
    """The agreed question set, in file order. Ids must be unique and texts non-empty."""
    rows = read_yaml(config_file(settings(cfg)["questions"])).get("questions") or []
    out = [Question(str(r["id"]), str(r["text"]).strip()) for r in rows]
    if not out or any(not q.text for q in out) or len({q.id for q in out}) != len(out):
        raise ValueError(f"{settings(cfg)['questions']}: needs questions with unique ids and non-empty text")
    return out


@dataclass(frozen=True)
class Rules:
    """What counts as a knowledge lookup, and how a quote is matched (eval.graph_first)."""
    graph_tools: tuple[str, ...]
    other_tools: tuple[str, ...]
    skill_tool: str
    skill_input: str
    skills: tuple[str, ...]
    file_tools: tuple[str, ...]
    file_inputs: tuple[str, ...]
    file_patterns: tuple[str, ...]
    quote_min_words: int


def rules(cfg: dict) -> Rules:
    g, k = settings(cfg), settings(cfg)["knowledge"]
    patterns = list(k["file_patterns"])
    if cfg["paths"]["transcripts"]:                  # the transcripts folder always counts
        patterns.append("(?i)" + re.escape(str(Path(cfg["paths"]["transcripts"]))))
    return Rules(tuple(k["graph_tools"]), tuple(k["other_tools"]), k["skill_tool"], k["skill_input"],
                 tuple(s.casefold() for s in k["skills"]), tuple(k["file_tools"]), tuple(k["file_inputs"]),
                 tuple(patterns), g["quote_min_words"])


# --- scoring (pure) -------------------------------------------------------------------

@dataclass
class ToolCall:
    id: Optional[str]
    name: str
    input: dict


@dataclass
class Session:
    calls: list[ToolCall] = field(default_factory=list)
    results: dict[str, list[str]] = field(default_factory=dict)   # tool_use id -> result texts
    answer: str = ""
    result: dict = field(default_factory=dict)                     # the result event


def _content_texts(content: Any) -> list[str]:
    """A tool_result's content as texts: a string, or the text of each text block."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return []


def parse_stream(events: Iterable[dict]) -> Session:
    """Tool calls in order, their results, and the final answer (the result event's text,
    else the last assistant text) from a session's stream-json events."""
    s, last_text = Session(), ""
    for event in events:
        if not isinstance(event, dict):
            continue
        kind, message = event.get("type"), event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if kind == "assistant" and isinstance(blocks, list):
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            if any(texts):
                last_text = "\n".join(t for t in texts if t)
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("name"), str):
                    s.calls.append(ToolCall(b.get("id"), b["name"], b.get("input") if isinstance(b.get("input"), dict) else {}))
        elif kind == "user" and isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                    s.results.setdefault(b["tool_use_id"], []).extend(_content_texts(b.get("content")))
        elif kind == "result":
            s.result = event
    s.answer = s.result.get("result") if isinstance(s.result.get("result"), str) and s.result.get("result") else last_text
    return s


def lookup_kind(call: ToolCall, r: Rules) -> Optional[str]:
    """GRAPH, OTHER, or None when the call is no knowledge lookup (ToolSearch, say)."""
    if call.name.startswith(r.graph_tools):
        return GRAPH
    if call.name.startswith(r.other_tools):
        return OTHER
    if call.name == r.skill_tool:
        skill = str(call.input.get(r.skill_input) or "").casefold()
        return OTHER if skill.rsplit(":", 1)[-1] in r.skills else None
    if call.name in r.file_tools:
        values = [str(call.input[k]) for k in r.file_inputs if call.input.get(k)]
        return OTHER if any(re.search(p, v) for p in r.file_patterns for v in values) else None
    return None


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold().replace("’", "'").replace("‘", "'")))


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _result_strings(text: str) -> list[str]:
    """A tool result's strings: every string value of a JSON result, else the text."""
    try:
        return list(_strings(json.loads(text)))
    except ValueError:
        return [text]


def quoted_spans(answer: str) -> list[str]:
    """Every quoted span in an answer, in pattern order."""
    return [m.group(1).strip() for rx in _QUOTED for m in rx.finditer(answer or "")]


def cited_quote(answer: str, graph_texts: list[str], min_words: int) -> Optional[str]:
    """The first quoted span of `answer` found in `graph_texts`, compared word by word
    (case, punctuation and curly apostrophes ignored). A span may elide with an
    ellipsis: each fragment of at least `min_words` words must be found, and fragments
    shorter than that are ignored; a span with no such fragment proves nothing."""
    hay = " " + " | ".join(_words(s) for t in graph_texts for s in _result_strings(t)) + " "
    for span in quoted_spans(answer):
        parts = [_words(p) for p in _ELLIPSIS.split(span)]
        parts = [p for p in parts if len(p.split()) >= min_words]
        if parts and all(f" {p} " in hay for p in parts):
            return span
    return None


def score_session(events: Iterable[dict], r: Rules) -> dict:
    """One session's score: the first knowledge lookup, whether a graph tool was used,
    the answer's quote that a graph tool returned (or None), and pass/fail."""
    s = parse_stream(events)
    lookups = [(c, kind) for c in s.calls if (kind := lookup_kind(c, r))]
    graph_ids = {c.id for c, kind in lookups if kind == GRAPH}
    graph_texts = [t for i in graph_ids for t in s.results.get(i, [])]
    first = lookups[0] if lookups else None
    quote = cited_quote(s.answer, graph_texts, r.quote_min_words)
    first_is_graph = first is not None and first[1] == GRAPH
    return {"first_knowledge_tool": first[0].name if first else None, "first_is_graph": first_is_graph,
            "graph_used": bool(graph_ids), "cited_quote": quote, "passed": first_is_graph and quote is not None,
            "knowledge_tools": [c.name for c, _ in lookups], "tool_calls": len(s.calls),
            "answer_chars": len(s.answer), "is_error": bool(s.result.get("is_error")) or not s.result,
            "permission_denials": len(s.result.get("permission_denials") or [])}


def summarize(rows: list[dict], arms: Iterable[str], scored_arm: str, pass_min: int, n_questions: int) -> dict:
    """Per arm: sessions, passes, and the bar (at least pass_min of n_questions)."""
    out = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        passed = sum(1 for r in mine if r["passed"])
        out[arm] = {"sessions": len(mine), "passed": passed, "of": n_questions, "pass_min": pass_min,
                    "meets_bar": passed >= pass_min, "scored": arm == scored_arm,
                    "first_is_graph": sum(1 for r in mine if r["first_is_graph"]),
                    "graph_used": sum(1 for r in mine if r["graph_used"]),
                    "cited": sum(1 for r in mine if r["cited_quote"] is not None)}
    return out


# --- the plan -------------------------------------------------------------------------

def gf_dir(cfg: dict) -> Path:
    return data_dir(cfg, *DIR)


def ledger_path(cfg: dict) -> Path:
    return gf_dir(cfg) / LEDGER_FILE


def plugin_dir(cfg: dict) -> Path:
    named = settings(cfg)["plugin_dir"]
    return Path(named).resolve() if named else preflight.REPO / "plugin"


def working_dir(cfg: dict) -> Path:
    named = settings(cfg)["working_dir"]
    return Path(named).resolve() if named else Path.home()


def cli_base(cfg: dict) -> list[str]:
    g = settings(cfg)
    return [*g["cli_args"], "--tools", ",".join(g["builtin_tools"]),
            "--allowedTools", ",".join(g["allowed_tools"]),
            *(["--disallowedTools", ",".join(g["disallowed_tools"])] if g["disallowed_tools"] else [])]


def session_env(cfg: dict, note_on: bool) -> dict[str, str]:
    """The variables set in every session of an arm: the note switch, and the plugin."""
    note = cfg["graph_first_note"]
    return {note["switch_env"]: note["on_value"] if note_on else note["off_value"],
            settings(cfg)["plugin_dirs_env"]: str(plugin_dir(cfg))}


def estimate(cfg: dict, rows: list[dict]) -> tuple[float, str]:
    """(per-session cost, its basis): the mean measured cost of this role and model's
    ledger rows, else cost_seed_usd. Rows booked at their cap (no result) are left out."""
    g = settings(cfg)
    role, model = g["role"], cfg["models"][g["role"]]["model"]
    costs = [float(r.get("cost_usd") or 0.0) for r in rows
             if r.get("role") == role and str(r.get("model") or "").startswith(model)
             and not r.get("cost_is_upper_bound") and not r.get("event")]
    if costs:
        return sum(costs) / len(costs), f"ledger: mean of {len(costs)} {role}/{model} session(s) in {ledger_path(cfg)}"
    return g["cost_seed_usd"], (f"config seed eval.graph_first.cost_seed_usd (no {role}/{model} rows in "
                                f"{ledger_path(cfg)} yet)")


def hook_context(cfg: dict, note_on: bool, *, python: str = sys.executable) -> Optional[str]:
    """What the plugin's SessionStart hook gives Claude in an arm (its additionalContext,
    "" for nothing), run as Claude Code runs it; None when it could not run. Reads the
    alerts read-only; makes no model call."""
    env = {k: v for k, v in os.environ.items() if k != settings(cfg)["plugin_dirs_env"]}
    env.update(session_env(cfg, note_on))
    try:
        proc = subprocess.run([python, "-s", str(plugin_dir(cfg) / HOOK)], input=json.dumps(HOOK_EVENT),
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout.strip() else ""
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def refusals(cfg: dict, qs: list[Question], contexts: Mapping[str, Optional[str]]) -> list[str]:
    """Reasons a real run would not start: a bar it cannot meet, an unknown scored arm,
    or a hook that does not give each arm what it should (the manipulation check)."""
    g, note = settings(cfg), cfg["graph_first_note"]["text"].strip()
    out = []
    if g["scored_arm"] not in g["arms"]:
        out.append(f"eval.graph_first.scored_arm {g['scored_arm']!r} is not one of the arms {list(g['arms'])}")
    if g["pass_min"] > len(qs):
        out.append(f"eval.graph_first.pass_min {g['pass_min']} is more than the {len(qs)} questions")
    if not (plugin_dir(cfg) / HOOK).is_file():
        out.append(f"no SessionStart hook at {plugin_dir(cfg) / HOOK}")
    for arm, on in g["arms"].items():
        ctx = contexts.get(arm)
        if ctx is None:
            out.append(f"arm {arm}: the plugin's SessionStart hook did not run")
        elif (note in ctx) != on:
            out.append(f"arm {arm}: the hook's context {'lacks' if on else 'has'} the graph-first note "
                       f"(is {plugin_dir(cfg)} the plugin with P2b merged?)")
    return out


# --- running --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def spend(cfg: dict, key: str, *, run: bool = False) -> float:
    """Ledger spend of one session (its request key), or with `run` of every session of
    the run whose id `key` is."""
    def mine(k: str) -> bool:
        return k.startswith(key + ":") if run else k == key
    return sum(float(r.get("cost_usd") or 0.0) for r in models.read_jsonl(ledger_path(cfg))
               if mine(str(r.get("request_key") or "")))


def print_plan(cfg: dict, qs: list[Question], contexts: Mapping[str, Optional[str]],
               out: Callable[[str], None]) -> None:
    g = settings(cfg)
    spec = cfg["models"][g["role"]]
    n = len(g["arms"]) * len(qs)
    per, basis = estimate(cfg, models.read_jsonl(ledger_path(cfg)))
    out(f"graph-first (P2b): {len(g['arms'])} arm(s) x {len(qs)} questions = {n} sessions;"
        f" role {g['role']} = {spec['model']} / {spec['effort']}")
    out(f"  working dir: {working_dir(cfg)}")
    out(f"  plugin: {plugin_dir(cfg)} (as {g['plugin_dirs_env']})")
    out(f"  cli: {' '.join(cli_base(cfg))} --model {spec['model']} --effort {spec['effort']}"
        f" --max-budget-usd {g['max_budget_per_session_usd']:.2f}")
    switch = cfg["graph_first_note"]["switch_env"]
    for arm, on in g["arms"].items():
        ctx = contexts.get(arm)
        seen = "hook did not run" if ctx is None else (
            "note present" if cfg["graph_first_note"]["text"].strip() in ctx else "no note")
        out(f"  arm {arm}: note {'on' if on else 'off'} ({switch}={session_env(cfg, on)[switch]}); hook check: {seen}")
    out(f"  bar: arm {g['scored_arm']} passes with at least {g['pass_min']} of {len(qs)} sessions passing")
    out(f"  estimate: ${per:.2f} per session x {n} = ${per * n:.2f} ({basis})")
    out(f"  worst case: ${g['max_budget_per_session_usd'] * n:.2f} ({n} x the per-session cap);"
        f" the run stops before a session that could pass ${g['cap_usd']:.2f}")
    for arm in g["arms"]:
        for q in qs:
            out(f"    {arm:7} {q.id}  {q.text}")


def _write_raw(path: Path) -> Callable[[dict], None]:
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(event: dict) -> None:
        models.append_jsonl(path, event)
    return write


def _session(cfg: dict, run_id: str, arm: str, on: bool, q: Question, raw: Path, r: Rules,
             call: Callable[..., models.CallResult]) -> dict:
    g = settings(cfg)
    key = f"{run_id}:{arm}:{q.id}"
    error = None
    try:
        call(cfg, g["role"], q.text, max_budget_usd=g["max_budget_per_session_usd"], ledger=ledger_path(cfg),
             request_key=key, cli_base=cli_base(cfg), cwd=working_dir(cfg), env=session_env(cfg, on),
             on_event=_write_raw(raw))
    except models.AuthError:
        raise                                        # fail closed: never score a session on the wrong sign-in
    except models.ModelCallError as exc:             # the cap, a timeout, an error result: still scored
        error = str(exc)
    return {"arm": arm, "question_id": q.id, "question": q.text, "request_key": key, "raw": str(raw),
            **score_session(models.read_jsonl(raw), r), "cost_usd": spend(cfg, key), "error": error, "ts": _now()}


def _finish(cfg: dict, run_dir: Path, rows: list[dict], qs: list[Question], stopped: Optional[str],
            out: Callable[[str], None]) -> int:
    g = settings(cfg)
    summary = summarize(rows, g["arms"], g["scored_arm"], g["pass_min"], len(qs))
    scored = summary.get(g["scored_arm"], {})
    complete = stopped is None and all(s["sessions"] == len(qs) for s in summary.values())
    verdict = ("PASS" if scored.get("meets_bar") else "FAIL") if complete else "INCOMPLETE"
    atomic_write_text(run_dir / SUMMARY_FILE, json.dumps(
        {"verdict": verdict, "stopped": stopped, "arms": summary, "ts": _now()}, indent=1))
    for arm, s in summary.items():
        out(f"arm {arm}{' (scored)' if s['scored'] else ' (baseline)'}: {s['passed']} / {s['of']} pass"
            f" (bar {s['pass_min']}); graph first {s['first_is_graph']}, graph used {s['graph_used']},"
            f" graph quote cited {s['cited']}")
    if stopped:
        out(f"STOPPED: {stopped}")
    out(f"verdict: {verdict}  ({run_dir})")
    return EXIT_PASS if verdict == "PASS" else EXIT_FAIL if verdict == "FAIL" else EXIT_REFUSED


def rescore(cfg: dict, run_id: str, *, out: Callable[[str], None] = print) -> int:
    """Score a finished run's raw transcripts again (no model call), rewriting its
    scored.jsonl and summary.json: a scorer fix never needs a paid rerun."""
    run_dir = gf_dir(cfg) / "results" / run_id
    old = models.read_jsonl(run_dir / SCORED_FILE)
    if not old:
        out(f"no scored sessions in {run_dir}")
        return EXIT_REFUSED
    r = rules(cfg)
    rows = [{**row, **score_session(models.read_jsonl(Path(row["raw"])), r)} for row in old]
    atomic_write_text(run_dir / SCORED_FILE, "".join(json.dumps(row) + "\n" for row in rows))
    return _finish(cfg, run_dir, rows, questions(cfg), None, out)


def run(cfg: dict, *, dry_run: bool = False, out: Callable[[str], None] = print,
        call: Callable[..., models.CallResult] = models.call,
        contexts: Optional[Mapping[str, Optional[str]]] = None) -> int:
    """Plan, then (unless dry_run) run every arm's sessions and score them."""
    g = settings(cfg)
    qs = questions(cfg)
    if contexts is None:
        contexts = {arm: hook_context(cfg, on) for arm, on in g["arms"].items()}
    print_plan(cfg, qs, contexts, out)
    refused = refusals(cfg, qs, contexts)
    if dry_run:
        for reason in refused:
            out(f"would refuse: {reason}")
        out("dry run: zero model calls made")
        return EXIT_PASS
    refused += [f"{name} is set (a nested Claude session): start this from the one-shot task"
                for name in models.refused_env(cfg)]
    if refused:
        for reason in refused:
            out(f"REFUSED: {reason}")
        return EXIT_REFUSED

    run_id = f"gf-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    run_dir = gf_dir(cfg) / "results" / run_id
    run_dir.mkdir(parents=True)
    atomic_write_text(run_dir / RUN_FILE, json.dumps(
        {"run_id": run_id, "started": _now(), "provenance": preflight.provenance(cfg),
         "role": g["role"], "model": cfg["models"][g["role"]], "settings": g,
         "questions": [asdict(q) for q in qs], "cli_base": cli_base(cfg), "working_dir": str(working_dir(cfg)),
         "plugin_dir": str(plugin_dir(cfg)), "hook_contexts": dict(contexts)}, indent=1))
    out(f"run {run_id}: {run_dir}")
    r, rows, stopped = rules(cfg), [], None
    for arm, on in g["arms"].items():
        for q in qs:
            spent = spend(cfg, run_id, run=True)
            if spent + g["max_budget_per_session_usd"] > g["cap_usd"] + 1e-9:
                stopped = (f"spent ${spent:.2f}; the next session (cap ${g['max_budget_per_session_usd']:.2f})"
                           f" could pass eval.graph_first.cap_usd ${g['cap_usd']:.2f}")
                break
            try:
                row = _session(cfg, run_id, arm, on, q, run_dir / RAW_DIR / f"{arm}-{q.id}.jsonl", r, call)
            except models.AuthError as exc:          # fail closed: no session runs on the wrong sign-in
                stopped = f"{arm} {q.id}: {exc}"
                break
            models.append_jsonl(run_dir / SCORED_FILE, row)
            rows.append(row)
            out(f"  {arm:7} {q.id}  {'PASS' if row['passed'] else 'fail'}  first {row['first_knowledge_tool']}"
                f"  quote {'yes' if row['cited_quote'] else 'no'}"
                f"  ${row['cost_usd']:.2f}"
                f"{'  ERROR ' + row['error'][:120] if row['error'] else ''}")
        if stopped:
            break
    out(f"spent ${spend(cfg, run_id, run=True):.2f} (ledger {ledger_path(cfg)})")
    return _finish(cfg, run_dir, rows, qs, stopped, out)
