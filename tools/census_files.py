"""Per-file totals of two census JSON reports: cognitive complexity, the largest function's
lines plus statements, gotos and locals. ``census_files.py OLD.json NEW.json``."""

import json
import sys


def row(rep):
    """``(cognitive, size, gotos, locals)`` of one file's census report."""
    size = max(rep["lines"].values(), default=0) + max(rep["statements"].values(), default=0)
    return sum(rep["complexity"].values()), size, rep["counts"]["goto"], sum(rep["locals"].values())


def main(old, new):
    """Print each file's row in ``old`` and ``new``."""
    with open(old, encoding="utf-8") as f:
        a = json.load(f)
    with open(new, encoding="utf-8") as f:
        b = json.load(f)
    for k in sorted(a):
        if k in b and not k.split("/")[-1].startswith("fuzz"):
            print(k.split("/")[-1], "cognitive/size/gotos/locals", row(a[k]), "->", row(b[k]))


if __name__ == "__main__":
    main(*sys.argv[1:3])
