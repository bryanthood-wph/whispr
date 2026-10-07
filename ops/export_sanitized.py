"""python ops/export_sanitized.py: a sanitized snapshot of this repository, in a separate
local repository, for a remote that must not get the full history or the sensitive text.

Its rules come from a YAML file kept outside every repository (--rules; default
%APPDATA%/whispr/sanitize/sanitize.yaml), because they name the sensitive terms; this
script holds none, and the export leaves out ops/ (this folder). From the rules:

1. `git archive <ref>` into a temp folder: tracked files only, so nothing untracked leaks.
2. `exclude` drops paths; `swap` replaces files with reviewed stand-ins (swap/<path> beside
   the rules file), and stops if a source file changed since its stand-in was made
   (`source_sha256`); `replace` applies ordered, case-insensitive regex replacements to
   text and file names, keeping the matched case.
3. The leak gate fails on any `deny` term, any email outside `allowed_email_domains`, any
   Windows user folder outside `allowed_user_dirs`, and any person, organization or
   project name or alias in the live knowledge graph (read-only), as a whole word.
4. The test gate runs the unit suite on the exported tree.
5. --write only: the tree replaces the export repo's working tree (a fresh `git init` the
   first time, so it shares no objects with this one) and is committed as one snapshot by
   the rules' `author`. Nothing is ever pushed from here.

--check runs steps 1 to 4 and writes nothing. Exit 0 clean, 1 the leak gate failed, 2 bad rules or setup,
3 the test gate failed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterator, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = Path(os.environ.get("APPDATA", "")) / "whispr" / "sanitize" / "sanitize.yaml"
EXIT_OK, EXIT_LEAK, EXIT_RULES, EXIT_TESTS = 0, 1, 2, 3
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
USER_DIR = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s'\"`<>%$*]+)", re.I)


class RulesError(Exception):
    pass


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def archive(ref: str, dest: Path) -> None:
    data = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", ref],
                          check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def tree_files(tree: Path) -> Iterator[Path]:
    """Every file but the test gate's throwaway repo at the tree root."""
    return (p for p in sorted(tree.rglob("*")) if p.is_file() and p.relative_to(tree).parts[0] != ".git")


def decode(data: bytes) -> Optional[str]:
    """UTF-8 or BOM-marked UTF-16 text, else None (binary)."""
    try:
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16")
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def remove(path: Path) -> None:
    """Delete a file or folder, read-only git objects included (Windows)."""
    def writable(fn, p, _exc):
        os.chmod(p, 0o666)
        fn(p)
    shutil.rmtree(path, onexc=writable) if path.is_dir() else (os.chmod(path, 0o666), path.unlink())


def relpath(tree: Path, path: Path) -> str:
    return path.relative_to(tree).as_posix()


def matches(path: str, pattern: str) -> bool:
    return path.startswith(pattern) if pattern.endswith("/") else fnmatch.fnmatchcase(path, pattern)


def keep_case(found: str, new: str) -> str:
    if found.isupper():
        return new.upper()
    if found.islower():
        return new.lower()
    return new


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- building the tree ----------------------------------------------------------------

def drop_excluded(tree: Path, patterns: list[str]) -> list[str]:
    """Remove excluded files; returns the patterns that matched nothing (stale rules)."""
    used = set()
    for path in list(tree_files(tree)):
        hit = [p for p in patterns if matches(relpath(tree, path), p)]
        if hit:
            used.update(hit)
            path.unlink()
    for folder in sorted((p for p in tree.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    return [p for p in patterns if p not in used]


def apply_swaps(tree: Path, swaps: dict, swap_dir: Path) -> list[str]:
    """Put each stand-in in place; returns why any could not be (the export then stops)."""
    problems = []
    for rel, recorded in swaps.items():
        source, stand_in = tree / rel, swap_dir / rel
        if not source.is_file():
            problems.append(f"swap {rel}: not in the exported tree (rename the rule)")
            continue
        if not stand_in.is_file():
            problems.append(f"swap {rel}: no stand-in at {stand_in}")
            continue
        now = sha256(source)
        if recorded != now:
            problems.append(f"swap {rel}: the source is {now}, the rules record {recorded}; review "
                            f"{stand_in} against it, then set source_sha256 to {now}")
            continue
        shutil.copyfile(stand_in, source)
    return problems


def compile_replacements(rules: list) -> list[tuple[re.Pattern, str]]:
    try:
        return [(re.compile(pattern, re.I), new) for pattern, new in rules]
    except (ValueError, re.error) as exc:
        raise RulesError(f"replace: {exc}") from None


def replace_text(text: str, replacements: list[tuple[re.Pattern, str]]) -> str:
    for pattern, new in replacements:
        text = pattern.sub(lambda m: keep_case(m.group(0), new), text)
    return text


def apply_replacements(tree: Path, replacements: list[tuple[re.Pattern, str]]) -> int:
    """Rewrite UTF-8 text files (line endings kept) and file names; returns how many files
    changed. UTF-16 and binary files are left as is: the gate still scans the UTF-16 ones,
    and main lists the binary ones for a person to look at."""
    changed = 0
    for path in list(tree_files(tree)):
        data = path.read_bytes()
        text = decode(data)
        if text is not None and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
            new = replace_text(text, replacements)
            if new != text:
                path.write_bytes(new.encode("utf-8"))
                changed += 1
        renamed = tree / replace_text(relpath(tree, path), replacements)
        if renamed != path:
            if renamed.exists():
                raise RulesError(f"replace: renaming {relpath(tree, path)} would overwrite {relpath(tree, renamed)}")
            renamed.parent.mkdir(parents=True, exist_ok=True)
            path.rename(renamed)
            changed += 1
    return changed


# ---- the leak gate --------------------------------------------------------------------

def kg_names(spec: dict) -> list[str]:
    """Live graph names (person, organization, project, ... per spec.types) and their
    aliases, read-only; at least spec.min_chars long."""
    sys.path.insert(0, str(ROOT))
    from kg import db
    from pipeline.config import load_config
    conn = db.connect_readonly(load_config())
    try:
        types = list(spec["types"])
        marks = ",".join("?" * len(types))
        rows = conn.execute(f"SELECT canonical_name FROM entity WHERE type IN ({marks})"
                            f" UNION SELECT a.alias FROM alias a JOIN entity e ON e.id = a.entity_id"
                            f" WHERE e.type IN ({marks})", types + types).fetchall()
    finally:
        conn.close()
    return sorted({r[0].strip() for r in rows if r[0] and len(r[0].strip()) >= spec["min_chars"]}, key=len, reverse=True)


def leak_patterns(rules: dict, names: list[str]) -> list[tuple[str, re.Pattern]]:
    """The deny terms, and the graph names not under `allow` (which never excuses a deny
    term). Matched per line, so a term split across a line break is not caught."""
    deny = rules["deny"] or []
    if not deny or not all(isinstance(d, str) and d.strip() for d in deny):
        raise RulesError("deny: every entry must be a non-empty pattern, and there must be one")
    try:
        out = [("deny", re.compile("|".join(f"(?:{d})" for d in deny), re.I))]
    except re.error as exc:
        raise RulesError(f"deny: {exc}") from None
    allow = {a.casefold() for a in rules.get("allow") or []}
    kept = [n for n in names if n.casefold() not in allow]
    if kept:
        out.append(("graph name", re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, kept)) + r")(?!\w)", re.I)))
    return out


def scan_text(label: str, text: str, patterns: list[tuple[str, re.Pattern]], rules: dict) -> list[str]:
    domains = tuple(d.casefold() for d in rules["allowed_email_domains"])
    users = {u.casefold() for u in rules["allowed_user_dirs"]}
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, pattern in patterns:
            hits += [f"{label}:{n}: {kind}: {m.group(0)!r}" for m in pattern.finditer(line)]
        for m in EMAIL.finditer(line):
            domain = m.group(1).casefold()
            if not any(domain == d or domain.endswith("." + d) for d in domains):
                hits.append(f"{label}:{n}: email: {m.group(0)!r}")
        hits += [f"{label}:{n}: user folder: {m.group(0)!r}" for m in USER_DIR.finditer(line)
                 if m.group(1).casefold() not in users]
    return hits


def scan_tree(tree: Path, patterns: list[tuple[str, re.Pattern]], rules: dict) -> tuple[list[str], list[str]]:
    """(hits, binary files). A binary file is scanned as latin-1, which finds plain ASCII
    terms but not compressed or otherwise encoded ones, so each is listed for review."""
    hits, binary = [], []
    for path in tree_files(tree):
        rel = relpath(tree, path)
        hits += scan_text(f"(file name) {rel}", rel, patterns, rules)
        data = path.read_bytes()
        text = decode(data)
        if text is None:
            binary.append(rel)
            text = data.decode("latin-1")
        hits += scan_text(rel, text, patterns, rules)
    return hits, binary


# ---- the test gate --------------------------------------------------------------------

def run_tests(tree: Path) -> tuple[bool, str]:
    """The suite in the tree as a clone would have it: a committed git repo, since the eval
    preflight reads `git status`. That throwaway .git is never copied to the export repo."""
    git("init", "-q", cwd=tree)
    git("add", "-A", "-f", cwd=tree)                # -f: a tracked file a .gitignore matches stays
    git("-c", "user.name=export", "-c", "user.email=export@example.com", "commit", "-qm", "export", cwd=tree)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",)}
    done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=tree, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-25:])
    return done.returncode == 0, tail


# ---- writing the export repo ----------------------------------------------------------

def write_export(tree: Path, rules: dict, message: str) -> str:
    repo = Path(rules["export_repo"])
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git("init", "-b", "main", cwd=repo)
    git("config", "user.name", rules["author"]["name"], cwd=repo)
    git("config", "user.email", rules["author"]["email"], cwd=repo)
    for child in repo.iterdir():
        if child.name != ".git":
            remove(child)
    shutil.copytree(tree, repo, dirs_exist_ok=True, ignore=lambda folder, names: [".git"] if Path(folder) == tree else [])
    git("add", "-A", "-f", cwd=repo)
    if not git("status", "--porcelain", cwd=repo).strip():
        return "no change since the last snapshot; nothing committed"
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-F", "-"], input=message.encode("utf-8"), check=True)
    return git("log", "--oneline", "-1", cwd=repo).strip()


# ---- main -----------------------------------------------------------------------------

def load_rules(path: Path) -> dict:
    try:
        rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RulesError(f"{path}: {exc}") from None
    required = ("export_repo", "author", "exclude", "swap", "replace", "deny", "allowed_email_domains",
                "allowed_user_dirs", "kg")
    missing = [k for k in required if k not in (rules or {})]
    if missing:
        raise RulesError(f"{path}: missing {', '.join(missing)}")
    return rules


def message_for(ref: str, sha: str, summary: Optional[Path]) -> str:
    body = summary.read_text(encoding="utf-8").strip() if summary else ""
    return f"Snapshot of {ref} at {sha[:7]}\n" + (f"\n{body}\n" if body else "")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--summary", type=Path, help="the snapshot commit's body (it is leak-scanned too)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    ap.add_argument("--skip-tests", action="store_true", help="--check only: the leak gate alone")
    args = ap.parse_args(argv)
    if args.write and args.skip_tests:
        ap.error("--write always runs the tests")
    try:
        return export(args)
    except RulesError as exc:
        print(f"rules: {exc}")
    except subprocess.CalledProcessError as exc:
        print(f"{' '.join(map(str, exc.cmd))}: exit {exc.returncode}\n{exc.stderr or ''}")
    except Exception as exc:                        # the graph read (config, a missing db)
        print(f"{type(exc).__name__}: {exc}")
    return EXIT_RULES


def export(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    replacements = compile_replacements(rules["replace"])
    patterns = leak_patterns(rules, kg_names(rules["kg"]))
    sha = git("rev-parse", args.ref).strip()
    message = message_for(args.ref, sha, args.summary)
    author = f"{rules['author']['name']} <{rules['author']['email']}>"
    with tempfile.TemporaryDirectory(prefix="whispr-export-", ignore_cleanup_errors=True) as tmp:
        tree = Path(tmp) / "tree"
        archive(sha, tree)
        stale = drop_excluded(tree, rules["exclude"])
        problems = apply_swaps(tree, rules["swap"] or {}, args.rules.parent / "swap")
        changed = apply_replacements(tree, replacements)
        print(f"{args.ref} ({sha[:7]}): {sum(1 for _ in tree_files(tree))} files exported, {changed} rewritten")
        for p in stale:
            print(f"warning: exclude {p!r} matched nothing")
        if problems:
            print("\n".join(problems))
            return EXIT_RULES
        hits, binary = scan_tree(tree, patterns, rules)
        hits += scan_text("(commit message)", message, patterns, rules) + scan_text("(author)", author, patterns, rules)
        if binary:
            print(f"binary files (scanned as latin-1 only; look at each): {', '.join(binary)}")
        if hits:
            print(f"leak gate: {len(hits)} hit(s)")
            print("\n".join(hits))
            return EXIT_LEAK
        print("leak gate: clean")
        if args.check and args.skip_tests:
            return EXIT_OK
        ok, tail = run_tests(tree)
        print(f"test gate: {'passed' if ok else 'FAILED'}\n{tail}")
        if not ok:
            return EXIT_TESTS
        if args.write:
            print(f"export repo {rules['export_repo']}: {write_export(tree, rules, message)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
