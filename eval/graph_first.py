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
tool, knowledge skill, or any Read/Grep/Glob) is a graph tool, and its answer quotes text
that a graph tool returned in that session (`score_session`, a pure function). The scored
arm passes when at least pass_min of its sessions pass; the other arms are baselines.
Sessions run question by question, each arm in turn, so a spend stop never leaves one
arm with fewer questions than the other by more than one.

Fail fast: each session's init event must show the graph reachable (`reach_problems`),
or the run stops after that session. Before any session, the run refuses unless your
live Claude Code settings load this checkout's plugin and whispr folder
(`live_problems`), so the hook checked is the hook the sessions load.

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
# A gap inside a quoted span: an ellipsis or a bracketed insertion ("[the team]").
_GAP = re.compile(r"\.\.\.|…|\[[^\]]*\]")
# Quoted spans in an answer: straight or curly double quotes, and blockquote lines.
_QUOTED = (re.compile(r'"([^"\n]+)"'), re.compile(r"“([^”]+)”"), re.compile(r"^\s*>\s?(.+)$", re.MULTILINE))
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
_JSON_ESCAPE = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt])')
_ESCAPED = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
# A Read result's line numbers ("    12\t" or "    12→"), and an absolute path in a result.
_LINE_NO = re.compile(r"(?m)^\s*\d+(?:\t|→)")
_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|*?]+|/(?:[^\s\"'<>|*?/]+/)+[^\s\"'<>|*?]+")


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
    read_tool: str
    read_input: str
    quote_min_words: int


def rules(cfg: dict) -> Rules:
    g, k = settings(cfg), settings(cfg)["knowledge"]
    return Rules(tuple(k["graph_tools"]), tuple(k["other_tools"]), k["skill_tool"], k["skill_input"],
                 tuple(s.casefold() for s in k["skills"]), tuple(k["file_tools"]), k["read_tool"], k["read_input"],
                 g["quote_min_words"])


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
        return OTHER
    return None


def _unescape(text: str) -> str:
    """JSON string escapes decoded, for a result that is not valid JSON (cut short, say)."""
    def one(m: re.Match) -> str:
        e = m.group(1)
        return chr(int(e[1:], 16)) if e[0] == "u" else _ESCAPED.get(e, e)
    return _JSON_ESCAPE.sub(one, text)


def _words(text: str) -> str:
    """Words only: curly and straight quotes folded, case and punctuation ignored, and
    whitespace collapsed."""
    return " ".join(re.findall(r"\w+", text.translate(_FOLD).casefold()))


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
    """A tool result's strings: every string value of a JSON result, else the text with
    its JSON escapes decoded and any Read line numbers dropped."""
    try:
        return list(_strings(json.loads(text)))
    except ValueError:
        return [_LINE_NO.sub("", _unescape(text))]


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.rstrip(".,;:)]}'\"")))


def saved_paths(texts: Iterable[str]) -> set[str]:
    """Absolute paths named in graph results: where the CLI saved a result too large to return."""
    return {_path_key(m.group(0)) for t in texts for s in (t, _unescape(t)) for m in _PATH.finditer(s)}


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
        parts = [_words(p) for p in _GAP.split(span.translate(_FOLD))]
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
    saved = saved_paths(graph_texts)                 # a large graph result, read back from its file
    graph_texts += [t for c in s.calls if c.name == r.read_tool and c.id not in graph_ids
                    and _path_key(str(c.input.get(r.read_input) or "")) in saved for t in s.results.get(c.id, [])]
    first = lookups[0] if lookups else None
    quote = cited_quote(s.answer, graph_texts, r.quote_min_words)
    first_is_graph = first is not None and first[1] == GRAPH
    return {"first_knowledge_tool": first[0].name if first else None, "first_is_graph": first_is_graph,
            "graph_used": bool(graph_ids), "cited_quote": quote, "passed": first_is_graph and quote is not None,
            "knowledge_tools": [c.name for c, _ in lookups], "tool_calls": len(s.calls),
            "answer_chars": len(s.answer), "is_error": bool(s.result.get("is_error")) or not s.result,
            "permission_denials": len(s.result.get("permission_denials") or [])}


def init_event(events: Iterable[dict]) -> dict:
    return next((e for e in events if isinstance(e, dict) and e.get("type") == "system"
                 and e.get("subtype") == "init"), {})


def reach_problems(events: Iterable[dict], reach: dict, plugin: Path) -> list[str]:
    """Why a session's init event shows the graph unreachable ([] when it is reachable):
    no graph server connected, or (when the event lists plugins) the plugin missing or
    loaded from another folder."""
    init = init_event(events)
    if not init:
        return ["the session reported no init event"]
    servers = {str(m.get("name")): m.get("status") for m in init.get("mcp_servers") or [] if isinstance(m, dict)}
    out = []
    if not any(servers.get(name) == reach["connected"] for name in reach["graph_servers"]):
        seen = {n: servers[n] for n in reach["graph_servers"] if n in servers}
        out.append(f"no graph server connected ({seen or 'none of ' + str(reach['graph_servers']) + ' listed'})")
    plugins = init.get("plugins")
    if isinstance(plugins, list):
        mine = [p for p in plugins if isinstance(p, dict) and p.get("name") == reach["plugin_name"]]
        if not mine:
            out.append(f"plugin {reach['plugin_name']!r} not loaded")
        elif mine[0].get("path") and _path_key(str(mine[0]["path"])) != _path_key(str(plugin)):
            out.append(f"plugin {reach['plugin_name']!r} loaded from {mine[0]['path']}, not {plugin}")
    return out


def note_in(note: str, text: str) -> bool:
    """Whether `text` carries the graph-first note."""
    return note.strip() in text


def note_in_stream(events: Iterable[dict], note: str) -> Optional[bool]:
    """Whether hook output in the stream carries the note's first line; None when the
    stream shows no hook output at all (the CLI need not emit it)."""
    first = note.strip().splitlines()[0]
    hooks = [json.dumps(e, ensure_ascii=False) for e in events if isinstance(e, dict) and e.get("type") == "system"
             and "hook" in str(e.get("subtype") or "").casefold()]
    return any(first in h for h in hooks) if hooks else None


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
                    "cited": sum(1 for r in mine if r["cited_quote"] is not None),
                    "note_in_stream": {str(v): sum(1 for r in mine if r.get("note_in_stream") is v)
                                       for v in (True, False, None)}}
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


def live_settings_path(cfg: dict) -> Path:
    named = settings(cfg)["live_settings"]
    return Path(named) if named else Path.home() / ".claude" / "settings.json"


def live_problems(cfg: dict) -> list[str]:
    """Why your live Claude Code settings would not load this checkout ([] when they
    do): their `env` names another plugin folder, or the plugin's whispr folder option
    (else the folder above the plugin) is not this checkout. Read-only."""
    g, path = settings(cfg), live_settings_path(cfg)
    try:
        live = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError) as exc:
        return [f"the live settings {path} do not read ({exc})"]
    env = live.get("env") if isinstance(live.get("env"), dict) else {}
    options = (((live.get("pluginConfigs") or {}).get(g["reach"]["plugin_name"]) or {}).get("options") or {})
    out = []
    dirs = [d for d in str(env.get(g["plugin_dirs_env"]) or "").split(os.pathsep) if d.strip()]
    if dirs and _path_key(str(plugin_dir(cfg))) not in {_path_key(d) for d in dirs}:
        out.append(f"your settings load the plugin from {os.pathsep.join(dirs)} ({g['plugin_dirs_env']}),"
                   f" not {plugin_dir(cfg)}: run this from that checkout")
    root = options.get(g["root_option"]) or str(plugin_dir(cfg).parent)
    if _path_key(str(root)) != _path_key(str(preflight.REPO)):
        out.append(f"the plugin's {g['root_option']} is {root}, not this checkout {preflight.REPO}:"
                   f" the hook and servers would run that code")
    return out


def hook_context(cfg: dict, note_on: bool, *, python: str = sys.executable) -> Optional[str]:
    """What the plugin's SessionStart hook gives Claude in an arm (its additionalContext,
    "" for nothing), run as Claude Code runs it, with no console window (models.NO_WINDOW,
    as every launch from the one-shot task); None when it could not run. Reads the alerts
    read-only; makes no model call."""
    env = {k: v for k, v in os.environ.items() if k != settings(cfg)["plugin_dirs_env"]}
    env.update(session_env(cfg, note_on))
    try:
        proc = subprocess.run([python, "-s", str(plugin_dir(cfg) / HOOK)], input=json.dumps(HOOK_EVENT),
                              capture_output=True, text=True, encoding="utf-8", env=env,
                              timeout=settings(cfg)["hook_timeout_s"],
                              creationflags=models.NO_WINDOW)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout.strip() else ""
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def refusals(cfg: dict, qs: list[Question], contexts: Mapping[str, Optional[str]]) -> list[str]:
    """Reasons a real run would not start: a bar it cannot meet, an unknown scored arm,
    or a hook that does not give each arm what it should (the manipulation check)."""
    g, note = settings(cfg), cfg["graph_first_note"]["text"]
    out = live_problems(cfg)
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
        elif note_in(note, ctx) != on:
            out.append(f"arm {arm}: the hook's context {'lacks' if on else 'has'} the graph-first note "
                       f"(is {plugin_dir(cfg)} the plugin with P2b merged?)")
    return out


# --- running --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def spend(cfg: dict, key: str, *, run: bool = False) -> float:
    """Ledger spend of one session (its request key), or with `run` of every session of
    the run whose id `key` is. models.py books a session with no result (timed out,
    killed) at its cap."""
    def mine(k: str) -> bool:
        return k.startswith(key + ":") if run else k == key
    return sum(float(r.get("cost_usd") or 0.0) for r in models.read_jsonl(ledger_path(cfg))
               if mine(str(r.get("request_key") or "")))


def run_spend(cfg: dict, run_id: str, started: Iterable[str]) -> float:
    """The run's spend: its ledger rows, plus each started session with no row yet at
    its cap (so a session whose row was never written still counts)."""
    booked = {str(r.get("request_key")) for r in models.read_jsonl(ledger_path(cfg))}
    missing = sum(1 for key in started if key not in booked)
    return spend(cfg, run_id, run=True) + missing * settings(cfg)["max_budget_per_session_usd"]


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
        seen = ("hook did not run" if ctx is None else
                "note present" if note_in(cfg["graph_first_note"]["text"], ctx) else "no note")
        out(f"  arm {arm}: note {'on' if on else 'off'} ({switch}={session_env(cfg, on)[switch]}); hook check: {seen}")
    out(f"  live settings: {live_settings_path(cfg)}"
        f" ({'; '.join(live_problems(cfg)) or 'they load this checkout'})")
    out(f"  bar: arm {g['scored_arm']} passes with at least {g['pass_min']} of {len(qs)} sessions passing")
    out(f"  estimate: ${per:.2f} per session x {n} = ${per * n:.2f} ({basis})")
    out(f"  worst case: ${g['max_budget_per_session_usd'] * n:.2f} ({n} x the per-session cap);"
        f" the run stops before a session whose cap could take it past ${g['cap_usd']:.2f}")
    for q, arm, _ in sessions(cfg, qs):
        out(f"    {q.id} {arm:7} {q.text}")


def sessions(cfg: dict, qs: list[Question]) -> list[tuple[Question, str, bool]]:
    """(question, arm, note on) in run order: question by question, each arm in turn."""
    return [(q, arm, on) for q in qs for arm, on in settings(cfg)["arms"].items()]


def _write_raw(path: Path) -> Callable[[dict], None]:
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(event: dict) -> None:
        models.append_jsonl(path, event)
    return write


def session_key(run_id: str, arm: str, q: Question) -> str:
    return f"{run_id}:{arm}:{q.id}"


def _session(cfg: dict, run_id: str, arm: str, on: bool, q: Question, raw: Path, r: Rules,
             call: Callable[..., models.CallResult]) -> dict:
    g = settings(cfg)
    key = session_key(run_id, arm, q)
    error = None
    try:
        call(cfg, g["role"], q.text, max_budget_usd=g["max_budget_per_session_usd"], ledger=ledger_path(cfg),
             request_key=key, cli_base=cli_base(cfg), cwd=working_dir(cfg), env=session_env(cfg, on),
             on_event=_write_raw(raw))
    except models.AuthError:
        raise                                        # fail closed: never score a session on the wrong sign-in
    except models.ModelCallError as exc:             # the cap, a timeout, an error result: still scored
        error = str(exc)
    events = models.read_jsonl(raw)
    return {"arm": arm, "question_id": q.id, "question": q.text, "request_key": key, "raw": str(raw),
            **score_session(events, r), "reach": reach_problems(events, g["reach"], plugin_dir(cfg)),
            "note_in_stream": note_in_stream(events, cfg["graph_first_note"]["text"]),
            "cost_usd": spend(cfg, key), "error": error, "ts": _now()}


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
        seen = s["note_in_stream"]
        out(f"arm {arm}{' (scored)' if s['scored'] else ' (baseline)'}: {s['passed']} / {s['of']} pass"
            f" (bar {s['pass_min']}); graph first {s['first_is_graph']}, graph used {s['graph_used']},"
            f" graph quote cited {s['cited']}; note in the stream: yes {seen['True']}, no {seen['False']},"
            f" hook output not shown {seen['None']}")
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
    r, rows, stopped, started = rules(cfg), [], None, []
    for q, arm, on in sessions(cfg, qs):
        spent = run_spend(cfg, run_id, started)
        if spent + g["max_budget_per_session_usd"] > g["cap_usd"] + 1e-9:
            stopped = (f"spent ${spent:.2f}; the next session's cap (${g['max_budget_per_session_usd']:.2f})"
                       f" would take the run past eval.graph_first.cap_usd ${g['cap_usd']:.2f}")
            break
        started.append(session_key(run_id, arm, q))
        try:
            row = _session(cfg, run_id, arm, on, q, run_dir / RAW_DIR / f"{arm}-{q.id}.jsonl", r, call)
        except models.AuthError as exc:              # fail closed: no session runs on the wrong sign-in
            stopped = f"{arm} {q.id}: {exc}"
            break
        models.append_jsonl(run_dir / SCORED_FILE, row)
        rows.append(row)
        out(f"  {q.id} {arm:7} {'PASS' if row['passed'] else 'fail'}  first {row['first_knowledge_tool']}"
            f"  quote {'yes' if row['cited_quote'] else 'no'}  ${row['cost_usd']:.2f}"
            f"{'  ERROR ' + row['error'][:g['error_chars']] if row['error'] else ''}")
        if row["reach"]:
            stopped = f"graph unreachable in {arm} {q.id}: {'; '.join(row['reach'])}"
            break
    out(f"spent ${run_spend(cfg, run_id, started):.2f} (ledger {ledger_path(cfg)})")
    return _finish(cfg, run_dir, rows, qs, stopped, out)
