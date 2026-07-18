# mutation-gate

[![ci](https://github.com/mikemartincode/mutation-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/mikemartincode/mutation-gate/actions/workflows/ci.yml)

A diff-scoped mutation-testing pre-commit gate for Python. When a commit
changes a complex function, the gate mutates that function's file, runs the
tests, and **blocks the commit** if any mutant survives or the function has no
tests at all. Correctly tested code goes in; everything else bounces.

## The philosophy

A green test suite proves the tests pass. It does not prove the tests would
*fail* if the code broke - and a suite that wouldn't catch the break is
theater. Mutation testing closes that gap mechanically: mutate the code
(flip an operator, drop a branch, change a constant), rerun the tests, and
demand that at least one test kills each mutant. A surviving mutant is a
behaviour change no test noticed.

This gate turns that standard into a commit-time invariant: **code that isn't
tested correctly isn't committed.** Not a dashboard, not a weekly CI report to
triage later - a wall the commit hits at the moment the untested change is
cheapest to fix.

## Why diff-scoped

Whole-repo mutation testing is far too slow to gate a commit - thousands of
mutants times a test-suite run each is an hours-scale job. But a commit does
not need the whole repo re-proven; it needs the *change* proven. So the gate:

1. looks only at staged `.py` files under the configured source roots,
2. enforces only **complex** functions (radon cyclomatic complexity at or
   above a threshold, default CC >= 11) - the code where an unnoticed
   behaviour change actually hides,
3. computes an AST-precise blast radius to decide *which* complex functions
   the diff affects (see below), and
4. runs mutmut on only the affected files, parallel across all cores
   (mutmut 3.x's trampoline does native test-impact selection, so each mutant
   runs only the tests that cover it).

A typical commit touches no complex function and passes in about a second. A
commit that edits one hot function pays seconds-to-minutes, proportional to
the change - which is exactly the property that makes mutation testing
gateable at all.

## How the AST scoping works

The unit of enforcement is a `(file, function)` pair. For each staged file the
gate parses the staged source (`git show :file`) and the `-U0` diff hunks, then
applies these rules:

- **Changed complex function** - a diff hunk overlapping a complex function's
  line span enforces that function. A changed simple function enforces
  nothing.
- **Changed module-level symbol** - an edit to a module-level `Assign` /
  `AnnAssign` (a shared constant, a compiled regex, a lookup table) is not
  attributable to one function, so the gate enforces every complex function
  in the file whose body references that name: "what it affects," not the
  whole file.
- **No blast radius by construction** - imports and the module docstring are
  excluded. A docstring edit or an added import pulls nothing in.
- **Unbindable fallback** - a changed module-level statement that can't be
  bound to a symbol (a bare call, a conditional assignment block) - or source
  that doesn't parse - falls back conservatively to whole-file: every complex
  function in the file is enforced.

Because the scoping walks the AST rather than grepping the diff, it is precise
in both directions: it never enforces a function because a comment above it
moved, and it never skips a function that consumes a constant the diff just
changed.

## The allowlist: equivalent mutants, on the record

Some mutants are *equivalent* - the mutation genuinely cannot change observable
behaviour, so no test can kill it. Those are accepted through a committed
allowlist, not a bypass flag:

```json
{"function": "parse_window", "mutation_sha": "3f6c2a9b8d1e4f70", "reason": "mutant swaps `>=` for `>` on a bound that is never hit exactly ..."}
```

- **Keyed**, not blanket: each entry accepts one mutation of one function,
  identified by a hash of the mutant's actual `+/-` change (line-number
  independent, so unrelated edits that shift the function don't invalidate
  it). Accepting one equivalent mutant silences nothing else.
- **Reason-carrying**: every entry records *why* the mutant is equivalent.
- **Committed and diff-visible**: adding an entry is a code change that goes
  through review like any other. A malformed allowlist accepts nothing.

The file may be a JSON array or JSON Lines - see
[`examples/allowlist.jsonl`](examples/allowlist.jsonl). Default location:
`tests/mutation_allowlist.json` in the host repo.

## Install and configure

```sh
pip install mutation-gate          # the gate itself (stdlib-only)
pip install radon "mutmut>=3.0"    # runtime requirements, or: pip install "mutation-gate[tools]"
```

Configure via `[tool.mutation-gate]` in the **host repo's** `pyproject.toml`
(every key also has a CLI flag, which wins):

```toml
[tool.mutation-gate]
roots        = ["src", "plugins"]   # gated source roots; unset = every staged .py
cc_threshold = 11                   # radon CC at/above which a function is "complex"
tests_dir    = ["tests"]            # where mutmut looks for covering tests
also_copy    = ["src", "plugins"]   # import closure mutmut copies (default: roots)
allowlist    = "tests/mutation_allowlist.json"
```

The gate drives mutmut by rewriting `paths_to_mutate`, `also_copy`, and
`tests_dir` in the host `pyproject.toml` for the duration of the run (restored
on exit), so the host must have a `[tool.mutmut]` section containing those
keys - placeholder values are fine. If `radon`/`mutmut` aren't on the hook's
PATH, point `MUTATION_GATE_RADON` / `MUTATION_GATE_MUTMUT` at the binaries.

## Wiring into pre-commit

The gate is a single exit-code-carrying command:

```sh
python -m mutation_gate                            # gate the staged diff
python -m mutation_gate --files a.py b.py          # gate specific files
python -m mutation_gate --files a.py --cc 1 --all  # whole-file audit of a.py
```

A minimal hook (full version in [`examples/pre-commit`](examples/pre-commit)):

```sh
echo "[mutation-gate] diff-scoped mutmut on complex changed functions"
if ! MUT_OUT=$(python -m mutation_gate 2>&1); then
  printf '%s\n' "$MUT_OUT" >&2
  exit 1
fi
printf '%s\n' "$MUT_OUT" | tail -1 >&2
```

On a violation the output names each surviving/untested mutant, shows the
`mutation_sha` to allowlist if it's genuinely equivalent, and points at
`mutmut show <name>` to see the exact change a test needs to kill. The escape
hatch is git's own: `git commit --no-verify`, deliberate and visible in your
shell history rather than hidden in configuration.

## Limits

- **Python only.** The scoping walks Python's `ast`; the tools are
  radon and mutmut.
- **radon and mutmut are required at runtime** (mutmut >= 3.x - the gate
  relies on its trampoline naming scheme and test-impact selection). If they
  are missing the gate *skips with a warning* rather than blocking, so
  installing the package without the tools does not brick commits - wire your
  environment so they're present, or the gate isn't gating.
- **Complexity is the trigger, not the proof.** Functions below the CC
  threshold are not enforced; the threshold is a budget knob, not a claim
  that simple code needs no tests. `--cc 1 --all` exists for full audits.
- **The host `pyproject.toml` is patched in place** during the mutmut run and
  restored afterwards; the gate fails loudly if the `[tool.mutmut]` keys it
  rewrites are absent.
- Detection of module-level symbol references is a word-boundary match over
  complex-function bodies of the *same file*; cross-module blast radius is
  out of scope (imports are covered by the importing module's own gate runs).

## Provenance

Extracted from a private codebase where it gates every commit. The scoping
logic, allowlist semantics, and hook wiring are the ones running there; the
repo-specific constants (source roots, thresholds, tool paths) became the
configuration surface documented above.

## License

MIT - see [LICENSE](LICENSE).
