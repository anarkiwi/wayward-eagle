"""Compare clang-tidy's ``readability-function-cognitive-complexity`` scores with
:func:`wayward_eagle.c.query.cognitive` per function, to recheck the model after a
clang-tidy upgrade: ``cognitive_check.py FILE.c... [-I DIR]``; prints each difference."""

import argparse
import json
import sys

from tqdm import tqdm

from wayward_eagle import census
from wayward_eagle.c import query

CONFIG = json.dumps(
    {
        "Checks": "-*,readability-function-cognitive-complexity",
        "CheckOptions": {"readability-function-cognitive-complexity.Threshold": 0},
    }
)


def tidy_scores(path, flags):
    """``{function: score}`` clang-tidy gives, its score read from each message's end."""
    run = census.Tidy(path, flags)
    out = {}
    for d in run.wait():
        msg = d["DiagnosticMessage"]["Message"]
        if d["DiagnosticName"] == "readability-function-cognitive-complexity":
            name = msg.split("'")[1]
            out[name] = int(msg.split(" of ")[1].split()[0])
    return out


def main(argv=None):
    """Exit status 1 when any function's two scores differ."""
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("-I", dest="include", action="append", default=[])
    args = ap.parse_args(argv)
    census.CONFIG = CONFIG
    bad = total = 0
    for path in tqdm(args.files, desc="files", unit="file"):
        flags = [f"-I{d}" for d in args.include] + [f"-I{path.rsplit('/', 1)[0]}"]
        unit = census.Unit(path, flags)
        theirs = tidy_scores(path, flags)
        for f in unit.functions:
            ours = query.cognitive(f.body)
            total += 1
            if ours != theirs.get(f.decl.name, 0):
                bad += 1
                print(f"{path}: {f.decl.name}: tidy {theirs.get(f.decl.name, 0)} ours {ours}")
    print(f"{bad} of {total} functions differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
