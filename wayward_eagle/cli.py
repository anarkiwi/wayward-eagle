"""``wayward-eagle`` command line: ``census FILE.c... [--json] [-I DIR] [--reader NAME]`` and
``reduce FILE.c [-o OUT.c] [-I DIR] [--status-type T] [--carries] [--recut]``."""

import argparse
import json
import sys

from pycparser import c_ast

from .c import load, reduce_unit
from .census import READERS, census_all


def main(argv=None):
    """Run a subcommand; exit status 0."""
    ap = argparse.ArgumentParser(prog="wayward-eagle")
    sub = ap.add_subparsers(dest="command", required=True)
    cen = sub.add_parser("census", help="readability defects of C files, by class")
    cen.add_argument("files", nargs="+", metavar="FILE.c")
    cen.add_argument("-I", dest="include", action="append", default=[], metavar="DIR")
    cen.add_argument("-j", "--jobs", type=int, default=None)
    cen.add_argument("--json", action="store_true", help="sites, counts and complexity as JSON")
    cen.add_argument(
        "--reader",
        dest="readers",
        action="append",
        default=None,
        metavar="NAME",
        help="a function whose one call is no effect-in-condition (default io_read)",
    )
    red = sub.add_parser("reduce", help="each function of a C file reduced, as C")
    red.add_argument("file", metavar="FILE.c")
    red.add_argument("-o", dest="out", default=None, metavar="OUT.c")
    red.add_argument("-I", dest="include", action="append", default=[], metavar="DIR")
    red.add_argument("--status-type", default="int", help="type of exit codes (default int)")
    red.add_argument("--carries", action="store_true", help="add-with-carry chains as sums")
    red.add_argument("--recut", action="store_true", help="labelled tails left cut again")
    args = ap.parse_args(argv)
    if args.command == "reduce":
        status = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType([args.status_type]))
        text = reduce_unit(
            load(args.file, args.include), status, carries=args.carries, recut=args.recut
        )
        if args.out is None:
            sys.stdout.write(text)
        else:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
        return 0
    readers = READERS if args.readers is None else args.readers
    reports = census_all(args.files, args.include, args.jobs, readers=readers)
    if args.json:
        out = {
            path: {
                "sites": [s._asdict() for s in rep.sites],
                "counts": rep.counts(),
                "complexity": rep.complexity,
                "locals": rep.locals,
                "lines": rep.lines,
                "statements": rep.statements,
                "names": dict(zip(("opaque", "declared"), rep.names)),
                "reasons": rep.reasons,
            }
            for path, rep in reports.items()
        }
        json.dump(out, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    for path, rep in reports.items():
        print(path)
        for s in rep.sites:
            print(f"  {s.line}: {s.cls} [{s.function}] {s.detail}")
        print("  " + ", ".join(f"{c} {n}" for c, n in rep.counts().items() if n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
