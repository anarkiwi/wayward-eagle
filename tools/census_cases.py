"""Write every C snippet of the census tests to a directory, one file each, so the census of
two versions can be compared with ``census_diff.py``: ``census_cases.py OUTDIR``."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))

import test_census as tc  # pylint: disable=import-error,wrong-import-position


def snippets():
    """``[(name, C body, compiler arguments)]`` of every census test case."""
    out = []
    for cls, (good, bad, args) in sorted(tc.CASES.items()):
        out += [(f"{cls}-found", good, args), (f"{cls}-absent", bad, args)]
    for name in dir(tc):
        fn = getattr(tc, name)
        for mark in getattr(fn, "pytestmark", ()):
            if mark.name != "parametrize" or not mark.args[0].startswith(("body", "expr")):
                continue
            for k, row in enumerate(mark.args[1]):
                body = row[0]
                if name == "test_subsumed_cast_by_types":
                    body = (
                        "int k(uint8_t);\nint k2(uint16_t);\n"
                        f"int f(int e, uint8_t u, int *p) {{ return {body}; }}"
                    )
                out.append((f"{name}-{k}", body, ()))
    return out


def main(outdir):
    """Write the snippets under ``outdir``; print each file and its compiler arguments."""
    root = pathlib.Path(outdir)
    root.mkdir(parents=True, exist_ok=True)
    for name, body, args in snippets():
        (root / f"{name}.c").write_text(tc.HEAD + body + "\n", encoding="utf-8")
        if args:
            print(name, *args)


if __name__ == "__main__":
    main(sys.argv[1])
