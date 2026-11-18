"""``wayward-eagle`` command line: ``census FILE.c... [--json] [-I DIR]``."""

import argparse
import json
import sys

from .census import census_all


def main(argv=None):
    """Run a subcommand; exit status 0."""
    ap = argparse.ArgumentParser(prog="wayward-eagle")
    sub = ap.add_subparsers(dest="command", required=True)
    cen = sub.add_parser("census", help="readability defects of C files, by class")
    cen.add_argument("files", nargs="+", metavar="FILE.c")
    cen.add_argument("-I", dest="include", action="append", default=[], metavar="DIR")
    cen.add_argument("-j", "--jobs", type=int, default=None)
    cen.add_argument("--json", action="store_true", help="sites, counts and complexity as JSON")
    args = ap.parse_args(argv)
    reports = census_all(args.files, args.include, args.jobs)
    if args.json:
        out = {
            path: {
                "sites": [s._asdict() for s in rep.sites],
                "counts": rep.counts(),
                "complexity": rep.complexity,
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
