"""Reduce C files under each combination of the optional rewrites; rank them by the census.

``combos.py OUT FILE.c... [-I DIR]``: carries, recut on or off, copies strict or always, in
OUT/<combination>/ and .json; ``combos.py --rank OUT``: rank OUT/*.json by total cognitive
complexity, then gotos, with each key differing from the best (``--only`` tunes)."""

import argparse
import concurrent.futures
import itertools
import json
import os
import pathlib
import subprocess
import sys

from tqdm import tqdm

from wayward_eagle.c import consts, front, reduce_unit

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "2")


def name(carries, recut, copies):
    """The directory name of a combination."""
    return f"carries{int(carries)}-recut{int(recut)}-copies-{copies}"


def reduced(job):
    """Write ``path`` reduced under one combination into ``out``; the written path."""
    path, include, out, carries, recut, copies = job
    if copies == "always":
        consts._better = lambda new, old: True  # pylint: disable=protected-access
    text = reduce_unit(front.load(path, include), carries=carries, recut=recut)
    dest = pathlib.Path(out) / pathlib.Path(path).name
    dest.write_text(text, encoding="utf-8")
    return str(dest)


def totals(report):
    """``(cognitive, gotos, {key: total})`` of a census JSON report."""
    keys = {}
    for rep in report.values():
        for k, v in rep["counts"].items():
            keys[k] = keys.get(k, 0) + v
        for m in ("locals", "lines", "statements"):
            keys[m] = keys.get(m, 0) + sum(rep[m].values())
    cognitive = sum(sum(rep["complexity"].values()) for rep in report.values())
    return cognitive, keys.get("goto", 0), keys


def census(out, include):
    """The census JSON report of the C files in ``out``."""
    files = sorted(str(p) for p in pathlib.Path(out).glob("*.c"))
    inc = [x for d in (str(out), *include) for x in ("-I", d)]
    exe = pathlib.Path(sys.executable).with_name("wayward-eagle")
    cmd = [str(exe), "census", "--json", "-j", "2", *inc, *files]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def main(argv=None):
    """Run every combination; print the ranking."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", action="store_true", help="rank OUT's reports only")
    ap.add_argument("--only", action="append", default=None, metavar="STEM")
    ap.add_argument("out")
    ap.add_argument("files", nargs="*")
    ap.add_argument("-I", dest="include", action="append", default=[])
    ap.add_argument("-j", "--jobs", type=int, default=4)
    args = ap.parse_args(argv)
    if args.rank:
        rank(args.out, args.only)
        return
    combos = list(itertools.product((False, True), (False, True), ("strict", "always")))
    jobs = []
    for c in combos:
        out = pathlib.Path(args.out) / name(*c)
        out.mkdir(parents=True, exist_ok=True)
        for f in args.files:
            for h in pathlib.Path(f).parent.glob("*.h"):
                (out / h.name).write_bytes(h.read_bytes())
            jobs.append((f, tuple(args.include), str(out), *c))
    with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
        list(tqdm(pool.map(reduced, jobs), total=len(jobs), desc="reduce", unit="file"))
    for c in tqdm(combos, desc="census", unit="combination"):
        got = census(pathlib.Path(args.out) / name(*c), args.include)
        (pathlib.Path(args.out) / f"{name(*c)}.json").write_text(got, encoding="utf-8")
    rank(args.out)


def rank(out, only=None):
    """Print the census reports OUT/*.json ranked, best first (``only``: file stems)."""
    rows = []
    for p in sorted(pathlib.Path(out).glob("*.json")):
        report = json.loads(p.read_text(encoding="utf-8"))
        if only:
            report = {k: v for k, v in report.items() if pathlib.Path(k).stem in only}
        rows.append((totals(report), p.stem))
    rows.sort(key=lambda r: (r[0][0], r[0][1]))
    keys = sorted(set().union(*(r[0][2] for r in rows)))
    base = rows[0][0][2]
    for (cog, gotos, k), n in rows:
        moved = {x: k.get(x, 0) for x in keys if k.get(x, 0) != base.get(x, 0)}
        print(f"{n}: cognitive {cog} gotos {gotos} vs best {moved}")


if __name__ == "__main__":
    main()
