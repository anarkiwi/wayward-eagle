"""List each definition of a local the census counts under a reason (default ``single-use``),
with its function, line and defining statement: ``locals_by_reason.py [-r REASON] [-I DIR]
FILE.c...``."""

import argparse
import pathlib
import sys

from pycparser import c_ast
from tqdm import tqdm

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wayward_eagle import c  # pylint: disable=wrong-import-position
from wayward_eagle.census import Unit  # pylint: disable=wrong-import-position


def sites(path, reason, args=()):
    """``[(function, line, statement)]`` of each local of ``path`` defined for ``reason``."""
    unit = Unit(path, args)
    out = []
    for f in unit.functions:
        unit._enter(f)  # pylint: disable=protected-access
        for d, r in unit.flow(f).temporaries():
            if r != reason or unit.c.symbols[d.var].kind != "VarDecl":
                continue
            n, coord = d.node, getattr(d.node, "coord", None)
            text = f"{n.name} = {c.text(n.init)}" if isinstance(n, c_ast.Decl) else c.text(n)
            out.append((f.decl.name, getattr(coord, "line", 0), " ".join(text.split())))
    return out


def main(argv=None):
    """Print each site, one per line, grouped by file."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", type=pathlib.Path)
    ap.add_argument("-r", "--reason", default="single-use")
    ap.add_argument("-I", dest="include", action="append", default=[])
    args = ap.parse_args(argv)
    flags = [f"-I{d}" for d in args.include]
    for path in tqdm(args.files, desc=args.reason, unit="file", file=sys.stderr):
        for fn, line, text in sites(path, args.reason, flags):
            print(f"{path.name}:{line}: {fn}: {text}")


if __name__ == "__main__":
    main()
