"""`python -m mutation_gate` - the form a pre-commit hook invokes."""

import sys

from mutation_gate.gate import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
