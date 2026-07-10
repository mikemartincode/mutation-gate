"""Unit tests for the PURE scoping logic of mutation_gate.gate.

These never execute radon, mutmut, or git. Radon-shaped `cc -j` items are
synthesized from the same source strings via `ast`, so line spans always match
the source under test. What is exercised here:

  * hunk-header parsing (`_ranges_from_diff`)
  * the blast-radius rule (`enforced_names` / `_module_changed_symbols`):
    docstring-only, import-only, function-body, module-constant,
    unbindable-statement, and syntax-error diffs
  * gated-path coverage (`_is_gated_source`)
  * mutant identity + allowlist keying/matching
    (`_file_of_mutant`, `_func_of_mutant`, `_sha_of_change`, `load_allowlist`)

Deliberately NOT covered (impure by nature, exercised only end-to-end):
`compute_enforced` (shells radon + git), `run_mutmut` (patches the host
pyproject and runs mutmut), and `_mutation_sha` (shells `mutmut show` -
its pure half, `_sha_of_change`, is covered).
"""

from __future__ import annotations

import ast
import json
import textwrap

from mutation_gate.gate import (
    _file_of_mutant,
    _func_of_mutant,
    _is_gated_source,
    _module_changed_symbols,
    _overlaps,
    _ranges_from_diff,
    _sha_of_change,
    enforced_names,
    load_allowlist,
)

# -- helpers: build radon-shaped items from the source itself ----------------


def _defs(source: str) -> list[ast.stmt]:
    return [n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def radon_items(source: str, complexity: int = 12) -> list[dict]:
    """Synthesize radon `cc -j` entries for every top-level def in `source`."""
    return [
        {"name": n.name, "complexity": complexity, "lineno": n.lineno, "endline": n.end_lineno}
        for n in _defs(source)
    ]


def span_of(source: str, predicate) -> tuple[int, int]:
    """(lineno, end_lineno) of the first module-level statement matching `predicate`."""
    for node in ast.parse(source).body:
        if predicate(node):
            return node.lineno, node.end_lineno or node.lineno
    raise AssertionError("no matching statement in test source")


MODULE = textwrap.dedent(
    '''\
    """Module docstring.

    Two lines of it.
    """

    import os
    from pathlib import Path

    LIMIT = 10
    PATTERN: str = "a+b"


    def uses_limit(x):
        if x > 0:
            return x < LIMIT
        return False


    def independent(x):
        return x * 2
    '''
)


# -- hunk parsing -------------------------------------------------------------


def test_ranges_from_diff_parses_new_side_hunks():
    diff = (
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -3,0 +4,2 @@\n"
        "+x = 1\n"
        "+y = 2\n"
        "@@ -10 +12 @@\n"
        "-a\n"
        "+b\n"
    )
    assert _ranges_from_diff(diff) == [(4, 5), (12, 12)]


def test_ranges_from_diff_excludes_pure_deletions():
    # A deletion-only hunk has a new-side count of 0 - no new-side lines to gate.
    diff = "@@ -5,2 +4,0 @@\n-gone\n-gone too\n"
    assert _ranges_from_diff(diff) == []


def test_overlaps_is_inclusive_interval_intersection():
    assert _overlaps(5, 10, [(10, 12)])
    assert _overlaps(5, 10, [(1, 5)])
    assert not _overlaps(5, 10, [(11, 12), (1, 4)])


# -- blast radius: what a change affects --------------------------------------


def test_docstring_only_change_enforces_nothing():
    lo, hi = span_of(MODULE, lambda n: isinstance(n, ast.Expr))  # the module docstring
    assert enforced_names(MODULE, radon_items(MODULE), [(lo, hi)], cc=11, gate_all=False) == set()


def test_import_only_change_enforces_nothing():
    lo, hi = span_of(MODULE, lambda n: isinstance(n, ast.ImportFrom))
    assert enforced_names(MODULE, radon_items(MODULE), [(lo, hi)], cc=11, gate_all=False) == set()


def test_changed_function_body_targets_that_function_only():
    fn = next(n for n in _defs(MODULE) if n.name == "uses_limit")
    ranges = [(fn.lineno + 1, fn.lineno + 1)]  # a line inside its body
    got = enforced_names(MODULE, radon_items(MODULE), ranges, cc=11, gate_all=False)
    assert got == {"uses_limit"}


def test_below_threshold_function_is_not_enforced():
    fn = next(n for n in _defs(MODULE) if n.name == "uses_limit")
    ranges = [(fn.lineno + 1, fn.lineno + 1)]
    # Same diff, but every function's CC is below the threshold: nothing enforced.
    got = enforced_names(MODULE, radon_items(MODULE, complexity=5), ranges, cc=11, gate_all=False)
    assert got == set()


def test_changed_module_constant_targets_referencing_functions():
    lo, hi = span_of(MODULE, lambda n: isinstance(n, ast.Assign))  # LIMIT = 10
    got = enforced_names(MODULE, radon_items(MODULE), [(lo, hi)], cc=11, gate_all=False)
    assert got == {"uses_limit"}  # `independent` never references LIMIT


def test_changed_annotated_constant_is_a_bound_symbol():
    src = MODULE.replace("return x * 2", "return PATTERN * x")
    lo, hi = span_of(src, lambda n: isinstance(n, ast.AnnAssign))  # PATTERN: str = ...
    got = enforced_names(src, radon_items(src), [(lo, hi)], cc=11, gate_all=False)
    assert got == {"independent"}


def test_unbindable_module_statement_falls_back_to_whole_file():
    src = MODULE + "\nif os.environ.get('X'):\n    LIMIT = 2\n"
    lo, hi = span_of(src, lambda n: isinstance(n, ast.If))
    got = enforced_names(src, radon_items(src), [(lo, hi)], cc=11, gate_all=False)
    assert got == {"uses_limit", "independent"}  # every complex fn: blast radius unbindable


def test_unparseable_source_falls_back_to_whole_file():
    broken = "def broken(:\n"
    items = [
        {"name": "a", "complexity": 12, "lineno": 1, "endline": 1},
        {"name": "b", "complexity": 12, "lineno": 2, "endline": 2},
    ]
    assert _module_changed_symbols(broken, [(1, 1)]) == (set(), True)
    assert enforced_names(broken, items, [(1, 1)], cc=11, gate_all=False) == {"a", "b"}


def test_gate_all_enforces_every_complex_function_regardless_of_diff():
    got = enforced_names(MODULE, radon_items(MODULE), [], cc=11, gate_all=True)
    assert got == {"uses_limit", "independent"}


def test_module_changed_symbols_collects_assign_targets_only_on_changed_lines():
    lo, hi = span_of(MODULE, lambda n: isinstance(n, ast.Assign))
    symbols, unbounded = _module_changed_symbols(MODULE, [(lo, hi)])
    assert symbols == {"LIMIT"}
    assert unbounded is False
    # An untouched module: no symbols, bounded.
    assert _module_changed_symbols(MODULE, [(10_000, 10_001)]) == (set(), False)


# -- gated-path coverage ------------------------------------------------------


def test_is_gated_source_respects_roots_and_exclusions():
    roots = ("src", "plugins")
    assert _is_gated_source("src/pkg/mod.py", roots)
    assert _is_gated_source("plugins/thing/api.py", roots)
    assert not _is_gated_source("scripts/tooling.py", roots)  # outside roots
    assert not _is_gated_source("src/pkg/tests/test_mod.py", roots)  # tests dir
    assert not _is_gated_source("src/pkg/test_mod.py", roots)  # test_ file
    assert not _is_gated_source("src/_archive/old.py", roots)  # archived


def test_is_gated_source_with_no_roots_gates_every_non_test_file():
    assert _is_gated_source("anywhere/mod.py", ())
    assert not _is_gated_source("anywhere/test_mod.py", ())


# -- mutant identity + allowlist keying/matching ------------------------------


def test_file_and_func_of_mutant_for_plain_functions():
    name = "pkg.sub.mod.x_parse_window__mutmut_28"
    assert _file_of_mutant(name) == "pkg/sub/mod.py"
    assert _func_of_mutant(name) == "parse_window"


def test_func_of_mutant_strips_method_class_prefix():
    assert _func_of_mutant("pkg.mod.xǁParserǁresolve__mutmut_3") == "resolve"


def test_sha_of_change_ignores_line_numbers_and_context():
    a = "--- x\n+++ y\n@@ -10,1 +10,1 @@\n context\n-    return a >= b\n+    return a > b\n"
    b = "--- x\n+++ y\n@@ -99,1 +99,1 @@\n other context\n-    return a >= b\n+    return a > b\n"
    c = "--- x\n+++ y\n@@ -10,1 +10,1 @@\n-    return a >= b\n+    return a < b\n"
    assert _sha_of_change(a) == _sha_of_change(b)  # position-independent
    assert _sha_of_change(a) != _sha_of_change(c)  # change-sensitive


def test_allowlist_matching_accepts_only_the_keyed_pair(tmp_path):
    path = tmp_path / "allow.jsonl"
    path.write_text(
        json.dumps({"function": "parse_window", "mutation_sha": "3f6c2a9b8d1e4f70", "reason": "equivalent"})
        + "\n"
        + json.dumps({"function": "merge_spans", "mutation_sha": "a1b2c3d4e5f60718", "reason": "equivalent"})
        + "\n"
    )
    allow = load_allowlist(path)
    assert ("parse_window", "3f6c2a9b8d1e4f70") in allow  # exact key: accepted
    assert ("parse_window", "ffffffffffffffff") not in allow  # same fn, other mutation: blocked
    assert ("merge_spans", "3f6c2a9b8d1e4f70") not in allow  # sha of a different fn: blocked


def test_allowlist_accepts_json_array_format(tmp_path):
    path = tmp_path / "allow.json"
    path.write_text(json.dumps([{"function": "f", "mutation_sha": "abc", "reason": "r"}]))
    assert load_allowlist(path) == {("f", "abc")}


def test_allowlist_missing_or_malformed_accepts_nothing(tmp_path):
    assert load_allowlist(tmp_path / "absent.json") == set()
    bad = tmp_path / "bad.json"
    bad.write_text("[{not json")
    assert load_allowlist(bad) == set()  # fail closed
    badline = tmp_path / "bad.jsonl"
    badline.write_text('{"function": "f", "mutation_sha": "abc"}\n{oops\n')
    assert load_allowlist(badline) == set()  # one bad line poisons nothing into acceptance


def test_allowlist_entry_without_key_fields_is_ignored(tmp_path):
    path = tmp_path / "allow.jsonl"
    path.write_text('{"function": "f", "reason": "missing sha"}\n{"function": "g", "mutation_sha": "abc"}\n')
    assert load_allowlist(path) == {("g", "abc")}
