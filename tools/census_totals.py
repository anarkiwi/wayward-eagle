"""Totals of ``wayward-eagle census --json`` outputs, per file and over all, side by side:
``census_totals.py OLD.json NEW.json``. Prints locals by reason, each class, the cognitive
complexity and the largest function's lines and statements, where they differ."""

import json
import pathlib
import sys
from collections import Counter


def totals(path):
    """``{file name: Counter}`` of a census JSON output."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for name, rep in data.items():
        got = Counter()
        for reasons in rep["reasons"].values():
            got.update({f"locals:{k}": v for k, v in reasons.items()})
        got.update(rep["counts"])
        got["lines"] = max(rep["lines"].values(), default=0)
        got["statements"] = max(rep.get("statements", {}).values(), default=0)
        out[pathlib.Path(name).name] = got
    return out


def main(old, new):
    """Print each file's and the overall differences."""
    a, b = totals(old), totals(new)
    whole_a, whole_b = Counter(), Counter()
    for name in sorted(set(a) | set(b)):
        x, y = a.get(name, Counter()), b.get(name, Counter())
        whole_a.update(x)
        whole_b.update(y)
        diff = {k: (x[k], y[k]) for k in sorted(set(x) | set(y)) if x[k] != y[k]}
        if diff:
            print(name, diff)
    keys = sorted(set(whole_a) | set(whole_b))
    print("total", {k: (whole_a[k], whole_b[k]) for k in keys if whole_a[k] != whole_b[k]})
    la = sum(v for k, v in whole_a.items() if k.startswith("locals:"))
    lb = sum(v for k, v in whole_b.items() if k.startswith("locals:"))
    print("locals", (la, lb))


if __name__ == "__main__":
    main(*sys.argv[1:3])
