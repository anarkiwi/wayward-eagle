"""Reduce only the named functions of a C file with the c_ast reducer:
``reduce_one.py IN.c OUT.c NAME... [-I DIR] [--status-type T]``; no names, every function
the rewrites change, one per line, on stdout."""

import argparse
import sys

from pycparser import c_ast

from wayward_eagle.c import front
from wayward_eagle.c.reduce import reduce_function, reduce_unit


def main(argv=None):
    """Write the file with those functions reduced, or list the functions it would change."""
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("names", nargs="*")
    ap.add_argument("-I", dest="include", action="append", default=[])
    ap.add_argument("--status-type", default="uint8_t")
    args = ap.parse_args(argv)
    unit = front.load(args.src, args.include)
    status = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType([args.status_type]))
    if not args.names:
        noreturn = {s.name for s in unit.symbols.values() if s.noreturn}
        for f in unit.functions:
            if reduce_function(f, noreturn, status) != [f]:
                print(f.decl.name)
        return 0
    with open(args.out, "w", encoding="utf-8") as out:
        out.write(reduce_unit(unit, status, set(args.names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
