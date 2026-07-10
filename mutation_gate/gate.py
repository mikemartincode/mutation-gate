"""Diff-scoped mutation + complexity commit gate.

The enforcement backbone for "code that isn't tested correctly isn't committed."
For the staged diff it:

  1. finds changed `.py` files under the configured gated source roots,
  2. runs radon to find the COMPLEX functions (cyclomatic CC >= threshold) in them,
  3. decides per file which complex functions a change *affects* (see below),
  4. runs mutmut on those files, parallel across all cores (mutmut 3.x does native
     test-impact selection via its trampoline), and
  5. BLOCKS the commit if any enforced complex function has a surviving mutant
     (not an accepted equivalent) or no tests at all.

What a change "affects" (blast radius):
  - A changed complex function is enforced (diff overlap).
  - A changed MODULE-LEVEL symbol (a shared constant / regex / class attr) is not
    attributable to one function, so the gate enforces every COMPLEX function that
    references that symbol - "what it affects", not the whole file. Imports and
    docstrings are excluded (no blast radius). A module-level change we can't bind
    to a symbol (a bare statement) falls back to whole-file (all complex funcs).
  This is AST-precise: a docstring edit or an added import pulls nothing extra in.

Equivalent mutants - mutations that can't be killed because they don't change
behaviour - are accepted via a committed allowlist, keyed by function +
mutation-hash + a written reason. A deliberate, diff-visible act.

Stdlib only. Shells out to `radon` and `mutmut` resolved from PATH; override
the binaries with MUTATION_GATE_RADON / MUTATION_GATE_MUTMUT (e.g. to point at
a venv the hook doesn't activate).

Configuration - `[tool.mutation-gate]` in the HOST repo's pyproject.toml,
overridden per-key by CLI flags (CLI > pyproject > defaults):

  [tool.mutation-gate]
  roots        = ["src", "plugins"]  # gated source roots; unset/[] = every staged .py
  cc_threshold = 11                  # radon CC at/above which a function is "complex"
  tests_dir    = ["tests"]           # where mutmut looks for covering tests
  also_copy    = ["src", "plugins"]  # import closure mutmut copies (default: roots)
  allowlist    = "tests/mutation_allowlist.json"

Usage:
  python -m mutation_gate                      # gate the staged diff (pre-commit)
  python -m mutation_gate --files A.py B.py    # gate specific files
  python -m mutation_gate --files A.py --cc 1 --all   # whole-file audit of A.py
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CC = 11  # radon rank C; functions at/above this are "complex" and must be mutation-clean
DEFAULT_TESTS_DIR = ("tests",)  # where mutmut looks for covering tests
DEFAULT_ALLOWLIST = "tests/mutation_allowlist.json"

# Gated roots come from configuration. Include every root whose SHIPPED source
# must be mutation-clean - and every destination code can relocate into:
# coverage tracks where the code lives, not where it came from, so a file that
# moves between roots stays gated.

_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MUTANT_FUNC_RE = re.compile(r"__mutmut_\d+$")


class GateError(Exception):
    """A misconfiguration the gate refuses to run past (fails loud, not open)."""


@dataclass(frozen=True)
class GateConfig:
    repo: Path
    roots: tuple[str, ...]  # () = no root restriction: every staged .py is gated
    cc: int
    tests_dir: tuple[str, ...]
    also_copy: tuple[str, ...]  # () = leave the host's mutmut also_copy untouched
    allowlist: Path
    radon: str | None  # resolved binary, or None if not found
    mutmut: str | None


def sh(args: list[str], cwd: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, **kw)


# -- configuration ------------------------------------------------------------


def _repo_root() -> Path:
    """Host repo root: git toplevel from the cwd, falling back to the cwd."""
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    out = r.stdout.strip()
    return Path(out) if r.returncode == 0 and out else Path.cwd()


def _resolve_tool(name: str) -> str | None:
    """Resolve a tool binary from PATH; MUTATION_GATE_<NAME> env var overrides."""
    override = os.environ.get(f"MUTATION_GATE_{name.upper()}")
    return override or shutil.which(name)


def _load_pyproject_config(repo: Path) -> dict:
    """`[tool.mutation-gate]` from the host repo's pyproject.toml (may be absent)."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.exists():
        return {}
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return {}
    return data.get("tool", {}).get("mutation-gate", {})


def build_config(args: argparse.Namespace) -> GateConfig:
    """Merge CLI flags over [tool.mutation-gate] over defaults (CLI wins per key)."""
    repo = _repo_root()
    cfg = _load_pyproject_config(repo)
    roots = args.roots if args.roots is not None else cfg.get("roots", [])
    cc = args.cc if args.cc is not None else int(cfg.get("cc_threshold", DEFAULT_CC))
    tests_dir = args.tests_dir if args.tests_dir is not None else cfg.get("tests_dir", list(DEFAULT_TESTS_DIR))
    # also_copy defaults to the gated roots: the mutated file's import closure
    # almost always lives under them, and mutmut needs it copied into mutants/.
    also_copy = args.also_copy if args.also_copy is not None else cfg.get("also_copy", list(roots))
    allowlist = args.allowlist if args.allowlist is not None else cfg.get("allowlist", DEFAULT_ALLOWLIST)
    allowlist_path = Path(allowlist)
    if not allowlist_path.is_absolute():
        allowlist_path = repo / allowlist_path
    return GateConfig(
        repo=repo,
        roots=tuple(roots),
        cc=cc,
        tests_dir=tuple(tests_dir),
        also_copy=tuple(also_copy),
        allowlist=allowlist_path,
        radon=_resolve_tool("radon"),
        mutmut=_resolve_tool("mutmut"),
    )


# -- diff scoping (pure where possible - this is the unit-tested core) --------


def _is_gated_source(f: str, roots: tuple[str, ...]) -> bool:
    """True if `f` is a source path the gate covers: under a gated root (when roots
    are configured) and not a test or archived file. Pure (no I/O) so the coverage
    rule is unit-testable."""
    if roots and not any(f.startswith(root + "/") for root in roots):
        return False
    if "/tests/" in f or Path(f).name.startswith("test_") or "_archive" in f:
        return False
    return True


def _staged_py_files(cfg: GateConfig) -> list[str]:
    r = sh(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "--", "*.py"], cwd=cfg.repo)
    return [f for f in r.stdout.split() if _is_gated_source(f, cfg.roots) and (cfg.repo / f).is_file()]


def _ranges_from_diff(diff_text: str) -> list[tuple[int, int]]:
    """New-side line ranges from a `-U0` unified diff (pure: parses hunk headers)."""
    ranges: list[tuple[int, int]] = []
    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            if count > 0:
                ranges.append((start, start + count - 1))
    return ranges


def _changed_ranges(f: str, repo: Path) -> list[tuple[int, int]]:
    """New-side line ranges touched in the staged diff of `f`."""
    return _ranges_from_diff(sh(["git", "diff", "--cached", "-U0", "--", f], cwd=repo).stdout)


def _overlaps(lo: int, hi: int, ranges: list[tuple[int, int]]) -> bool:
    """True if the line span [lo, hi] intersects any changed range [a, b]."""
    for a, b in ranges:
        # Two closed ranges intersect when each starts at or before the other ends.
        if a <= hi and lo <= b:
            return True
    return False


def _staged_source(f: str, repo: Path) -> str:
    """Staged (index) content of `f`, via `git show :<path>`. The gate must parse
    what is being COMMITTED, not the worktree copy, or line spans won't match the
    staged diff."""
    return sh(["git", "show", f":{f}"], cwd=repo).stdout


def _is_module_docstring(index: int, node: ast.stmt) -> bool:
    """The docstring is a bare string-literal expression as statement 0.
    (ast.get_docstring returns the text, not the node, so it can't tell us
    whether THIS statement is the docstring.)"""
    return (
        index == 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _module_changed_symbols(source: str, ranges: list[tuple[int, int]]) -> tuple[set[str], bool]:
    """Names assigned at MODULE level on changed lines, plus an `unbounded` flag.

    AST-precise. Excludes imports and the module docstring (no blast radius).
    Module-level Assign/AnnAssign -> collect target names. Functions/classes are
    handled separately (their own bodies). Any other module-level statement that
    a change touches -> unbounded (can't bind blast radius -> caller goes whole-file).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), True  # unparseable -> conservative whole-file
    symbols: set[str] = set()
    unbounded = False
    for i, node in enumerate(tree.body):
        lo = node.lineno
        # ast types end_lineno as Optional; fall back to the start line if it's
        # missing or None (CPython >= 3.8 always populates it).
        end = getattr(node, "end_lineno", None)
        hi = end if end is not None else lo
        if not _overlaps(lo, hi, ranges):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue  # import - blast radius is its users (their own tests cover them)
        if _is_module_docstring(i, node):
            continue  # module docstring - no blast radius
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # functions/methods handled via their own spans
        if isinstance(node, ast.Assign):
            # Only plain-Name targets bind symbols. Tuple / attribute / subscript
            # targets bind nothing here, so such a change enforces nothing extra.
            symbols.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols.add(node.target.id)
        else:
            unbounded = True
    return symbols, unbounded


def _span(item: dict) -> tuple[int, int]:
    """(first, last) line of a radon entry. radon omits `endline` for single-line
    defs, so the span degenerates to the start line."""
    lo = item.get("lineno", 0)
    hi = item.get("endline", lo)
    return lo, hi


def _references_any(body: str, symbols: set[str]) -> bool:
    """Whole-word text match, used deliberately instead of full AST resolution: a
    false positive only enforces one extra function (conservative-safe), and a
    \\b-bounded search never misses a genuine reference."""
    for sym in symbols:
        if re.search(rf"\b{re.escape(sym)}\b", body):
            return True
    return False


def enforced_names(
    source: str,
    items: list[dict],
    ranges: list[tuple[int, int]],
    cc: int,
    gate_all: bool,
) -> set[str]:
    """Function names in ONE file that must be mutation-clean for this diff.

    Pure core of the blast-radius rule (no git/radon calls): `items` are radon
    `cc -j` entries for the file, `ranges` the new-side changed line ranges.
    """
    complex_items = [it for it in items if it.get("complexity", 0) >= cc]
    complex_names = {it["name"] for it in complex_items}
    if gate_all:
        return complex_names

    names: set[str] = set()
    for it in complex_items:
        lo, hi = _span(it)
        if _overlaps(lo, hi, ranges):
            names.add(it["name"])  # the diff touches this function directly

    symbols, unbounded = _module_changed_symbols(source, ranges)
    if unbounded:
        names |= complex_names  # whole-file: blast radius unbindable
    elif symbols:
        src_lines = source.splitlines()
        for it in complex_items:
            lo, hi = _span(it)
            body = "\n".join(src_lines[lo - 1 : hi])
            if _references_any(body, symbols):
                names.add(it["name"])  # references a changed module-level symbol
    return names


def compute_enforced(files: list[str], cfg: GateConfig, gate_all: bool) -> set[tuple[str, str]]:
    """(file, funcname) pairs that must be mutation-clean for this diff."""
    r = sh([cfg.radon, "cc", "-j", "-s", *files], cwd=cfg.repo)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return set()

    enforced: set[tuple[str, str]] = set()
    for f, items in data.items():
        ranges = [] if gate_all else _changed_ranges(f, cfg.repo)
        source = "" if gate_all else _staged_source(f, cfg.repo)
        enforced |= {(f, n) for n in enforced_names(source, items, ranges, cfg.cc, gate_all)}
    return enforced


# -- mutant identity (pure helpers over mutmut's naming + diff output) --------


def _file_of_mutant(name: str) -> str:
    """`pkg.sub.mod.x_parse_window__mutmut_28` -> `pkg/sub/mod.py`."""
    dotted_module = name.rsplit(".", 1)[0]  # strip the trailing function part
    return dotted_module.replace(".", "/") + ".py"


def _func_of_mutant(name: str) -> str:
    """`pkg.mod.x_parse_window__mutmut_28` -> `parse_window`."""
    leaf = name.rsplit(".", 1)[-1]       # x_parse_window__mutmut_28
    leaf = _MUTANT_FUNC_RE.sub("", leaf)  # drop the __mutmut_<n> suffix
    if leaf.startswith("x_"):             # mutmut prefixes free functions with x_
        leaf = leaf[2:]
    # Methods are named xǁClassǁmethod; 'ǁ' (U+01C1) is mutmut's own separator,
    # legal in identifiers. The method name is the last segment (a no-op for
    # plain functions).
    return leaf.split("ǁ")[-1]


def _sha_of_change(diff_text: str) -> str:
    """Stable-ish hash of a mutant's actual +/- change (sans line numbers).

    Keyed on the changed lines only, so an unrelated edit that shifts the
    function down the file does not invalidate an allowlist entry.
    """
    changed_lines = []
    for line in diff_text.splitlines():
        is_change = line.startswith(("+", "-"))
        is_file_header = line.startswith(("+++", "---"))  # not part of the change
        if is_change and not is_file_header:
            changed_lines.append(line.strip())
    # sha1 here is an identity key, not a security boundary; 16 hex chars (64
    # bits) is ample against collisions at allowlist scale and easy to eyeball.
    digest = hashlib.sha1("\n".join(changed_lines).encode()).hexdigest()
    return digest[:16]


def _mutation_sha(name: str, cfg: GateConfig) -> str:
    return _sha_of_change(sh([cfg.mutmut, "show", name], cwd=cfg.repo).stdout)


# -- mutmut execution ---------------------------------------------------------


def _patch_mutmut_key(pyproject_text: str, key: str, values: list[str]) -> str:
    """Rewrite `key = [...]` (a [tool.mutmut] list) in the pyproject text.

    Fails loud if the key is absent: a gate that silently mutates the wrong
    paths is worse than one that refuses to run.
    """
    # Stdlib tomllib can only READ toml; there is no stdlib writer, so we rewrite
    # the one `key = [...]` entry textually. `[^\]]*` also spans multi-line lists
    # (a negated class matches newlines); mutmut's values are flat strings.
    quoted_values = ", ".join(f'"{v}"' for v in values)
    replacement = f"{key} = [{quoted_values}]"
    pattern = rf"{re.escape(key)}\s*=\s*\[[^\]]*\]"
    patched, n = re.subn(pattern, replacement, pyproject_text, count=1)
    if n == 0:
        raise GateError(
            f"pyproject.toml has no `{key} = [...]` entry under [tool.mutmut] - "
            "add one (any placeholder value; the gate rewrites it per run)"
        )
    return patched


def run_mutmut(files: list[str], cfg: GateConfig) -> str:
    """Mutate only `files`, run parallel across cores, return `mutmut results` text.

    Sets paths_to_mutate (the diff), also_copy (import closure), and tests_dir (the
    suite, so mutmut's test-selection finds each function's real tests) dynamically
    in the host pyproject; the pyproject is restored on exit.
    """
    pyproject = cfg.repo / "pyproject.toml"
    orig = pyproject.read_text()
    patched = _patch_mutmut_key(orig, "paths_to_mutate", list(files))
    if cfg.also_copy:
        patched = _patch_mutmut_key(patched, "also_copy", list(cfg.also_copy))
    patched = _patch_mutmut_key(patched, "tests_dir", list(cfg.tests_dir))
    shutil.rmtree(cfg.repo / "mutants", ignore_errors=True)
    try:
        pyproject.write_text(patched)
        nproc = str(os.cpu_count() or 4)
        sh([cfg.mutmut, "run", "--max-children", nproc], cwd=cfg.repo)
        return sh([cfg.mutmut, "results"], cwd=cfg.repo).stdout
    finally:
        pyproject.write_text(orig)


def parse_results(text: str) -> tuple[list[str], list[str]]:
    survived, no_tests = [], []
    for line in text.splitlines():
        line = line.strip()
        if line.endswith(": survived"):
            survived.append(line.rsplit(":", 1)[0].strip())
        elif line.endswith(": no tests"):
            no_tests.append(line.rsplit(":", 1)[0].strip())
    return survived, no_tests


# -- allowlist ----------------------------------------------------------------


def load_allowlist(path: Path) -> set[tuple[str, str]]:
    """Accepted equivalent mutants as {(function, mutation_sha)}.

    Two on-disk formats: a JSON array of entry objects, or JSON Lines (one
    object per line). Every entry carries a written `reason` - acceptance is a
    deliberate, diff-visible act, reviewed like code. A malformed allowlist
    accepts nothing (fail closed).
    """
    if not path.exists():
        return set()
    text = path.read_text().strip()
    if not text:
        return set()
    entries: list = []
    if text.startswith("["):
        try:
            entries = json.loads(text)
        except json.JSONDecodeError:
            return set()
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                return set()
    return {
        (e["function"], e["mutation_sha"])
        for e in entries
        if isinstance(e, dict) and "function" in e and "mutation_sha" in e
    }


# -- entry point --------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="mutation-gate", description="Diff-scoped mutation + complexity commit gate"
    )
    ap.add_argument("--files", nargs="*", help="explicit file list (default: staged diff)")
    ap.add_argument("--cc", type=int, default=None, help="complexity threshold (radon CC)")
    ap.add_argument("--all", action="store_true", help="enforce every complex fn (whole-file audit)")
    ap.add_argument("--roots", nargs="*", default=None, help="gated source roots (default: pyproject)")
    ap.add_argument("--tests-dir", nargs="*", default=None, help="dirs mutmut searches for covering tests")
    ap.add_argument("--also-copy", nargs="*", default=None, help="import-closure dirs mutmut copies")
    ap.add_argument("--allowlist", default=None, help="path to the equivalent-mutant allowlist")
    args = ap.parse_args(argv)
    cfg = build_config(args)

    files = args.files if args.files else _staged_py_files(cfg)
    if not files:
        print("mutation-gate: no gated .py files staged - skip")
        return 0
    if not cfg.mutmut or not cfg.radon:
        print(
            "mutation-gate: mutmut/radon not on PATH (pip install mutmut radon, "
            "or set MUTATION_GATE_MUTMUT / MUTATION_GATE_RADON) - skip",
            file=sys.stderr,
        )
        return 0

    enforced = compute_enforced(files, cfg, args.all)
    if not enforced:
        print(f"mutation-gate: no complex (CC>={cfg.cc}) function affected by the diff - pass")
        return 0

    print(f"mutation-gate: enforcing {len(enforced)} complex fn(s): {sorted(enforced)}")
    try:
        survived, no_tests = parse_results(run_mutmut(files, cfg))
    except GateError as e:
        print(f"mutation-gate: ✗ {e}", file=sys.stderr)
        return 1
    allow = load_allowlist(cfg.allowlist)

    violations: list[str] = []
    for name in survived:
        key = (_file_of_mutant(name), _func_of_mutant(name))
        if key not in enforced:
            continue
        sha = _mutation_sha(name, cfg)
        if (key[1], sha) not in allow:
            violations.append(f"  SURVIVED  {key[1]:<28} {name}  (mutation_sha={sha})")
    for name in no_tests:
        key = (_file_of_mutant(name), _func_of_mutant(name))
        if key in enforced:
            violations.append(f"  NO TESTS  {key[1]:<28} {name}")

    if not violations:
        print("mutation-gate: ✓ enforced functions mutation-clean")
        return 0

    try:
        allow_rel = cfg.allowlist.relative_to(cfg.repo)
    except ValueError:
        allow_rel = cfg.allowlist
    print("mutation-gate: ✗ BLOCKED - untested/under-tested complex code:")
    print("\n".join(sorted(violations)))
    print(
        "\nFix: add tests that KILL these mutants (`mutmut show <name>` shows the change).\n"
        "If a mutant is genuinely equivalent (no behaviour change), add it to "
        f"{allow_rel}:\n"
        '  {"function": "<fn>", "mutation_sha": "<sha>", "reason": "<why equivalent>"}\n'
        "Override (deliberate): git commit --no-verify"
    )
    return 1


def cli() -> None:
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
