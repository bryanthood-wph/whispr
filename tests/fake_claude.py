"""Stand-in for the claude CLI in tests: emits a stream-json init + result.

Behavior comes from FAKE_CLAUDE_MODE: ok | apikey | noinit | error | nostructured | hang |
badargs (rejects its arguments: stderr only, no events, exit 1) | hooksonly (one non-init
event, then exit 1 with no result) | silenthang (no events, then hangs) | junk (a bare JSON
value on stdout before the normal events).
--version prints FAKE_CLAUDE_VERSION (default FAKE_VERSION) and exits.
FAKE_CLAUDE_ARGS_OUT, if set, receives {"argv", "stdin", "system_append", "env_keys", "env_max"}
as JSON (system_append: the --append-system-prompt-file content, or null; env_max: the
MAX_* variables and their values, such as the thinking budget).
FAKE_CLAUDE_STRUCTURED, if set, is the structured_output JSON to return.
FAKE_CLAUDE_BY_PROPERTY, if set, is {property: structured_output}: the first entry whose
property the --json-schema declares wins over FAKE_CLAUDE_STRUCTURED. An entry with
"answer": null answers the prompt's first "ALLOWED ANSWERS:" option (the judge prompt).
"""

import json
import os
import sys
import time

from fake_version import FAKE_VERSION   # the script's own directory is on sys.path

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if "--version" in sys.argv:
    print(f"{os.environ.get('FAKE_CLAUDE_VERSION', FAKE_VERSION)} (Claude Code)")
    sys.exit(0)
stdin = sys.stdin.read()
system_append = None
if "--append-system-prompt-file" in sys.argv:
    with open(sys.argv[sys.argv.index("--append-system-prompt-file") + 1], encoding="utf-8") as fh:
        system_append = fh.read()
if os.environ.get("FAKE_CLAUDE_ARGS_OUT"):
    with open(os.environ["FAKE_CLAUDE_ARGS_OUT"], "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "stdin": stdin, "system_append": system_append,
                   "env_keys": sorted(os.environ),
                   "env_max": {k: v for k, v in os.environ.items() if k.upper().startswith("MAX_")}}, fh)

if mode == "badargs":
    print("Error: --json-schema is not a valid JSON Schema (test)", file=sys.stderr)
    sys.exit(1)
if mode == "hooksonly":
    print(json.dumps({"type": "system", "subtype": "hook_started"}), flush=True)
    sys.exit(1)
if mode == "silenthang":
    time.sleep(60)
if mode == "junk":
    print("true", flush=True)
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "unknown"
if mode != "noinit":
    print(json.dumps({"type": "system", "subtype": "init", "model": model,
                      "apiKeySource": "ANTHROPIC_API_KEY" if mode == "apikey" else "none"}), flush=True)
if mode == "hang":
    time.sleep(60)
structured = json.loads(os.environ.get("FAKE_CLAUDE_STRUCTURED", '{"ok": true}'))
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1]) if "--json-schema" in sys.argv else {}
by_property = json.loads(os.environ.get("FAKE_CLAUDE_BY_PROPERTY", "{}"))
structured = next((out for prop, out in by_property.items() if prop in schema.get("properties", {})), structured)
if isinstance(structured, dict) and "answer" in structured and structured["answer"] is None:
    allowed = stdin.rsplit("ALLOWED ANSWERS:", 1)[-1].splitlines()[0]
    structured = {**structured, "answer": allowed.split("|")[0].strip()}
print(json.dumps({
    "type": "result", "is_error": mode == "error", "total_cost_usd": 0.0123,
    "result": "boom" if mode == "error" else json.dumps(structured),
    "structured_output": None if mode in ("nostructured", "error") else structured,
    "num_turns": 1,
    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0,
              "output_tokens_details": {"thinking_tokens": 12}},
}), flush=True)
sys.exit(1 if mode == "error" else 0)
