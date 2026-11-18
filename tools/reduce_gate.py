"""Reduce each tune's lifted C with the c_ast reducer and compare per-function cognitive
complexity (clang-tidy's) with another reduction of the same C:
``reduce_gate.py OUTDIR TUNEDIR... [-I DIR] [--status-type T]``, each ``TUNEDIR`` holding
``lifted/<tune>.c`` and ``reduced/<tune>.c``. Writes ``OUTDIR/<tune>/<tune>.c``."""

import argparse
import pathlib
import shutil
import sys
import time

from pycparser import c_ast
from tqdm import tqdm

from wayward_eagle import census
from wayward_eagle.c import front
from wayward_eagle.c.reduce import reduce_unit


def owner(name, originals):
    """The function of ``originals`` piece ``name`` was cut from."""
    if name in originals:
        return name
    got = [o for o in originals if name.startswith(o + "_")]
    return max(got, key=len) if got else name


def scores(path, include, originals):
    """``{original function: summed cognitive complexity of its pieces}`` of C ``path``."""
    _, per = census.Unit(path, [f"-I{d}" for d in include]).tidy([f"-I{d}" for d in include])
    out = dict.fromkeys(originals, 0)
    for name, score in per.items():
        o = owner(name, originals)
        out[o] = out.get(o, 0) + score
    return out


def main(argv=None):  # pylint: disable=too-many-locals
    """Reduce, score and print a table per tune; exit status 1 when any function is worse."""
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("tunes", nargs="+")
    ap.add_argument("-I", dest="include", action="append", default=[])
    ap.add_argument("--status-type", default="uint8_t")
    args = ap.parse_args(argv)
    status = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType([args.status_type]))
    worse = 0
    for tune_dir in tqdm(args.tunes, desc="tunes", unit="tune"):
        tune = pathlib.Path(tune_dir)
        name = tune.name
        lifted = tune / "lifted" / f"{name}.c"
        out = pathlib.Path(args.outdir) / name
        out.mkdir(parents=True, exist_ok=True)
        for h in (tune / "lifted").glob("*.h"):
            shutil.copy(h, out / h.name)
        include = [str(tune / "lifted"), *args.include]
        start = time.time()
        unit = front.load(lifted, include)
        loaded = time.time()
        (out / f"{name}.c").write_text(reduce_unit(unit, status), encoding="utf-8")
        done = time.time()
        originals = {f.decl.name for f in unit.functions}
        base = scores(lifted, include, originals)
        theirs = scores(tune / "reduced" / f"{name}.c", include, originals)
        ours = scores(out / f"{name}.c", include, originals)
        print(f"\n{name}: load {loaded - start:.2f} s, reduce {done - loaded:.2f} s")
        print(f"{'function':16} {'lifted':>7} {'theirs':>7} {'ours':>7}")
        for f in sorted(originals, key=lambda o, b=base: (-b.get(o, 0), o)):
            if base.get(f, 0) or theirs.get(f, 0) or ours.get(f, 0):
                mark = " worse" if ours.get(f, 0) > theirs.get(f, 0) else ""
                print(f"{f:16} {base.get(f, 0):7} {theirs.get(f, 0):7} {ours.get(f, 0):7}{mark}")
                worse += bool(mark)
        total = [sum(d.values()) for d in (base, theirs, ours)]
        print(f"{'total':16} {total[0]:7} {total[1]:7} {total[2]:7}")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
