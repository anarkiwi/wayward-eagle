"""Differences between two ``wayward-eagle census --json`` outputs, matched by file name:
``census_diff.py OLD.json NEW.json``. Prints each class count and site that differs."""

import json
import pathlib
import sys


def by_name(path):
    """``{file name: report}`` of a census JSON output."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {
        str(pathlib.Path(p).relative_to(pathlib.Path(p).parents[2])): r for p, r in data.items()
    }


def main(old, new):
    """Print the differences; exit status 1 when there are any."""
    a, b = by_name(old), by_name(new)
    bad = 0
    for name in sorted(set(a) | set(b)):
        ra, rb = a.get(name), b.get(name)
        if ra is None or rb is None:
            print(f"{name}: only in {'new' if ra is None else 'old'}")
            bad += 1
            continue
        for cls in sorted(set(ra["counts"]) | set(rb["counts"])):
            ca, cb = ra["counts"].get(cls, 0), rb["counts"].get(cls, 0)
            if ca != cb:
                print(f"{name}: {cls} {ca} -> {cb}")
                bad += 1
        sa = {tuple(s.values()) for s in ra["sites"]}
        sb = {tuple(s.values()) for s in rb["sites"]}
        for s in sorted(sa - sb):
            print(f"{name}: - {s}")
        for s in sorted(sb - sa):
            print(f"{name}: + {s}")
        bad += len(sa ^ sb)
        if ra["complexity"] != rb["complexity"]:
            print(f"{name}: complexity differs")
            bad += 1
    print(f"{bad} differences")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
