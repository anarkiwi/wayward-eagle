"""virtuous-cycles adapter ``wayward-census``: the census' site counts and per-file measures
as keys (docs/census.md)."""

import glob
import pathlib
from collections import Counter

from .c.flow import REASONS
from .census import CLASSES, COGNITIVE, census_all

#: The per-file measures ``combine`` may weight beside classes: the most lines and statements
#: of a function, the definitions of automatic variables and those of each reason.
MEASURES = ("lines", "statements", "locals", *(f"locals:{r}" for r in REASONS))


def _measures(rep):
    """``{measure: value}`` of one file's report, :data:`MEASURES` in order."""
    out = {m: max(getattr(rep, m).values(), default=0) for m in ("lines", "statements")}
    out["locals"] = sum(rep.locals.values())
    for r in REASONS:
        out[f"locals:{r}"] = sum(c[r] for c in rep.reasons.values())
    return out


def adapter(options):  # pylint: disable=too-many-locals
    """virtuous-cycles adapter ``wayward-census``: site counts per key (docs/census.md).

    Options ``files`` (globs), ``include`` (directories), ``key`` (template over ``stem``,
    ``path``, ``class`` and ``function``), ``total`` (a file's complexity key), ``locals``
    (a file's number of local variables key), ``reasons`` (template also over ``reason``:
    a file's definitions of local variables of each of :data:`REASONS`), ``lines`` and
    ``statements`` (the most lines and statements of a function of the file keys); empty
    for none; ``combine`` (each name's weights of classes and :data:`MEASURES`: one key,
    ``class`` the name, of the weighted sum of their sites and per-file values, in place of
    theirs)."""
    files = sorted({p for g in options["files"] for p in glob.glob(g, recursive=True)})
    if not files:
        raise ValueError(f"no files match {options['files']}")
    template = options.get("key", "{stem}:{class}")
    total = options.get("total", "{stem}:cognitive-complexity")
    reasons = options.get("reasons", "{stem}:locals:{reason}")
    keys = {m: options.get(m, f"{{stem}}:{m}") for m in ("lines", "statements", "locals")}
    keys.update({f"locals:{r}": reasons and reasons.replace("{reason}", r) for r in REASONS})
    into = {k: (n, w) for n, ws in options.get("combine", {}).items() for k, w in ws.items()}
    unknown = set(into) - set(CLASSES) - set(MEASURES)
    if unknown:
        raise ValueError(f"combine names no class or measure: {sorted(unknown)}")
    out = Counter()
    for path, rep in census_all(files, options.get("include", ())).items():
        names = {"stem": pathlib.Path(path).stem, "path": path}
        held = [(k, "", 0) for k in ("{function}" not in template) * CLASSES[1:]]
        for cls, fn, n in held + [(s.cls, s.function, 1) for s in rep.sites]:
            if cls != COGNITIVE:
                cls, w = into.get(cls, (cls, 1))
                out[template.format(function=fn, **names, **{"class": cls})] += n * w
        out[total.format(**names)] += sum(rep.complexity.values())
        for m, v in _measures(rep).items():
            if m in into:
                name, w = into[m]
                out[template.format(function="", **names, **{"class": name})] += v * w
            elif keys[m] and m in ("lines", "statements"):
                k = keys[m].format(**names)
                out[k] = max(out[k], v)
            elif keys[m]:
                out[keys[m].format(**names)] += v
    return dict(out)
