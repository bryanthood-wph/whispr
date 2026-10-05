"""Stand-in for the claude CLI in tests: emits a stream-json init + result.

Behavior comes from FAKE_CLAUDE_MODE: ok | apikey | noinit | error | nostructured | hang.
FAKE_CLAUDE_ARGS_OUT, if set, receives {"argv", "stdin", "env_keys"} as JSON.
FAKE_CLAUDE_STRUCTURED, if set, is the structured_output JSON to return.
"""

import json
import os
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
stdin = sys.stdin.read()
if os.environ.get("FAKE_CLAUDE_ARGS_OUT"):
    with open(os.environ["FAKE_CLAUDE_ARGS_OUT"], "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "stdin": stdin, "env_keys": sorted(os.environ)}, fh)

model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "unknown"
if mode != "noinit":
    print(json.dumps({"type": "system", "subtype": "init", "model": model,
                      "apiKeySource": "ANTHROPIC_API_KEY" if mode == "apikey" else "none"}), flush=True)
if mode == "hang":
    time.sleep(60)
structured = json.loads(os.environ.get("FAKE_CLAUDE_STRUCTURED", '{"ok": true}'))
print(json.dumps({
    "type": "result", "is_error": mode == "error", "total_cost_usd": 0.0123,
    "result": "boom" if mode == "error" else json.dumps(structured),
    "structured_output": None if mode in ("nostructured", "error") else structured,
    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0},
}), flush=True)
sys.exit(1 if mode == "error" else 0)
