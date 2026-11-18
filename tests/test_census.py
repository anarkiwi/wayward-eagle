"""Census of readability defects: each class found where it is and not where it is not."""

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from wayward_eagle import census as cen
from wayward_eagle.cli import main

FUZZ = pathlib.Path(__file__).parent / "fixtures" / "fuzz"
HEAD = "#include <stdint.h>\nint g;\nvoid tick(void);\n"

#: class: (C with a site of it, C with none, compiler arguments).
CASES = {
    "cognitive-complexity": (
        "void f(int c) { if (c) { tick(); } }",
        "void f(void) { tick(); }",
        (),
    ),
    "branch-clone": (
        "void f(int c) { if (c) { g = 1; } else { g = 1; } }",
        "void f(int c) { if (c) { g = 1; } else { g = 2; } }",
        (),
    ),
    "redundant-cast": (
        "int f(int c) { return (int)c; }",
        "long f(int c) { return (long)c * c; }",
        (),
    ),
    "redundant-expression": (
        "int f(int c) { return c == c; }",
        "int f(int c, int d) { return c == d; }",
        (),
    ),
    "simplifiable-boolean": (
        "bool f(bool x) { if (x) { return true; } return false; }",
        "bool f(bool x) { return x; }",
        ("-std=c2x",),
    ),
    "redundant-control-flow": (
        "void f(void) { tick(); return; }",
        "void f(void) { tick(); }",
        (),
    ),
    "else-after-jump": (
        "int f(int c) { if (c) { return 1; } else { return 0; } }",
        "int f(int c) { if (c) { return 1; } return 0; }",
        (),
    ),
    "assignment-in-if": (
        "void f(int c) { if ((g = c)) { tick(); } }",
        "void f(int c) { g = c; if (g) { tick(); } }",
        (),
    ),
    "inc-dec-in-condition": (
        "int f(int i) { if (i++ < 5 && i > 3) { return 1; } return 0; }",
        "int f(int i) { i++; if (i < 6 && i > 4) { return 1; } return 0; }",
        (),
    ),
    "nested-conditional": (
        "int f(int c) { return c ? (c > 1 ? 1 : 2) : 3; }",
        "int f(int c) { return c ? 1 : 3; }",
        (),
    ),
    "misleading-indentation": (
        "void f(int c) {\n  if (c)\n    tick();\n    tick();\n}",
        "void f(int c) {\n  if (c)\n    tick();\n  tick();\n}",
        (),
    ),
    "redundant-branch-condition": (
        "int h(void);\nvoid f(void) { int on = h(); if (on) { if (on) { tick(); } } }",
        "int h(void);\nvoid f(void) { int on = h(); if (on) { tick(); } }",
        (),
    ),
    "dead-store": (
        "int f(int a) { int x; x = a; x = 2; return x; }",
        "int f(int a) { int x; x = a; return x; }",
        (),
    ),
    "common-arm-statements": (
        "void f(int c, int x) { uint8_t v5;"
        " if (c) { v5 = 2; } else { g = x; v5 = 2; } g = v5; }",
        "void f(int c, int x) { uint8_t v5;"
        " if (c) { v5 = 2; } else { g = x; v5 = 3; } g = v5; }",
        (),
    ),
    "subsumed-cast": (
        "int f(int e) { return (int8_t)(uint8_t)(e) > 0; }",
        "int f(int e) { return (int8_t)(e) > 0; }",
        (),
    ),
    "effect-in-condition": (
        "void f(uint8_t v5) { while (((int8_t)v5 > 0) && (v5 = v5 - 1, 1)) { tick(); } }",
        "void f(uint8_t v5) { while ((int8_t)v5 > 0) { tick(); v5 = v5 - 1; } }",
        (),
    ),
    "uncounted-loop": (
        "void f(void) { uint8_t v = 2; while (v != 0) { tick(); v = v - 1; } }",
        "void f(void) { for (uint8_t v = 2; v != 0; v = v - 1) { tick(); } }",
        (),
    ),
    "goto": (
        "void f(int c) { if (c) { goto out; } tick(); out:; }",
        "void f(int c) { if (!c) { tick(); } }",
        (),
    ),
    "duplicate-code": (
        "void f(int c) { if (c) { g = 1; tick(); } g = 2; g = 1; tick(); }",
        "void f(int c) { if (c) { g = 1; tick(); } g = 2; g = 3; tick(); }",
        (),
    ),
}


def sites(tmp_path, body, args=(), cls=None):
    """The sites (of ``cls``) of C ``body`` after the common declarations."""
    path = tmp_path / "t.c"
    path.write_text(HEAD + body + "\n", encoding="utf-8")
    got = cen.census(path, args=list(args)).sites
    return [s for s in got if cls is None or s.cls == cls]


def test_every_class_has_a_case():
    """Each class of the census has a case."""
    assert set(CASES) == set(cen.CLASSES)


@pytest.mark.parametrize("cls", sorted(CASES))
def test_class_found(tmp_path, cls):
    """C holding a defect of a class gives a site of it."""
    good, _, args = CASES[cls]
    assert sites(tmp_path, good, args, cls)


@pytest.mark.parametrize("cls", sorted(CASES))
def test_class_absent(tmp_path, cls):
    """The same C without the defect gives no site of it."""
    _, bad, args = CASES[cls]
    assert not sites(tmp_path, bad, args, cls)


def test_complexity_is_per_function(tmp_path):
    """Each function's cognitive complexity is clang-tidy's score."""
    path = tmp_path / "t.c"
    path.write_text(
        HEAD + "void f(int c) { if (c) { if (c > 1) { tick(); } } }\nvoid h(void) { tick(); }\n",
        encoding="utf-8",
    )
    rep = cen.census(path)
    assert rep.complexity == {"f": 3}
    assert rep.counts()["cognitive-complexity"] == 1
    assert [s.function for s in rep.sites if s.cls == "cognitive-complexity"] == ["f"]


@pytest.mark.parametrize(
    "expr, found",
    [
        ("(int8_t)(uint8_t)(e) > 0", True),
        ("(uint8_t)(e + 1) == 3", False),
        ("(int)u + 1", True),
        ("(unsigned)u + 1", False),
        ("(uint16_t)u << 8", False),
        ("(int16_t)u + 1", True),
        ("!(uint8_t)e", False),
        ("!(int16_t)u", True),
        ("e ? (int)u : 0", True),
        ("(uint8_t)e ? 1 : 0", False),
        ("(int16_t)u ? 1 : 0", True),
        ("p[(int16_t)u]", True),
        ("p[(uint8_t)e]", False),
        ("k((uint8_t)(e + 1))", True),
        ("k((uint16_t)e)", True),
        ("k2((uint8_t)e)", False),
        ("(_Bool)(uint8_t)e", False),
        ("(int)(double)e", False),
    ],
)
def test_subsumed_cast_by_types(tmp_path, expr, found):
    """A cast is subsumed when its operand's values or the conversion that follows it make
    it a no-op at every width of ``int``."""
    body = (
        f"int k(uint8_t);\nint k2(uint16_t);\nint f(int e, uint8_t u, int *p) {{ return {expr}; }}"
    )
    assert bool(sites(tmp_path, body, (), "subsumed-cast")) == found


@pytest.mark.parametrize(
    "body, found",
    [
        ("uint8_t f(int e) { uint8_t v = (uint8_t)(e + 1); return v; }", True),
        ("uint16_t f(int e) { uint16_t v = (uint8_t)(e + 1); return v; }", False),
        ("uint8_t f(int e) { return (uint8_t)(e + 1); }", True),
        ("void f(uint8_t *p, uint16_t w) { p[0] = (uint8_t)w; }", True),
        ("void f(uint8_t *p, uint16_t w) { p[0] += (uint8_t)w; }", False),
        ("void f(int e) { if ((int16_t)(int8_t)e) { tick(); } }", True),
        ("void f(int e) { if ((int8_t)e) { tick(); } }", False),
        ("void f(int e) { while ((int16_t)(int8_t)e) { tick(); } }", True),
        ("int f(int e) { return ((uint8_t)e, 1); }", False),
        ("void k(); void f(int e) { k((uint8_t)e); }", False),
    ],
)
def test_subsumed_cast_by_context(tmp_path, body, found):
    """An assignment, initialisation, return or argument converting to the cast's width or
    narrower subsumes it; a condition reads only its value."""
    assert bool(sites(tmp_path, body, (), "subsumed-cast")) == found


@pytest.mark.parametrize(
    "body, n",
    [
        ("void f(int c) { if (c) { tick(); g = 1; } else { tick(); g = 2; } }", 1),
        ("void f(int c) { if (c) { g = 1; tick(); } else { g = 2; tick(); } }", 1),
        ("void f(int c) { if (c) { g = 1; } else { g = 2; } }", 0),
        ("void f(int c) { if (c) { tick(); g = 1; } else if (c > 1) { tick(); g = 2; } }", 0),
        (
            "void f(int c) { if (c) { tick(); g = 1; } else if (c > 1) { tick(); g = 2; }"
            " else { tick(); g = 3; } }",
            1,
        ),
        ("void f(void) { if (g) { tick(); g = 0; } else { g = 0; } }", 1),
        ("void f(void) { if (g) { g = 0; tick(); } else { g = 0; g = 1; } }", 0),
        ("void f(int c) { if (c) { c = 0; tick(); } else { c = 0; g = 1; } }", 0),
        ("void f(int c) { int v; if (c) { v = 0; tick(); } else { v = 0; g = 1; } g = v; }", 1),
        ("void f(int c) { if (g) { tick(); g = 1; } else { tick(); g = 2; } }", 0),
        ("int h(void); void f(void) { if (h()) { g = 1; tick(); } else { g = 1; } }", 0),
        (
            "void f(int c) { switch (c) { case 1: g = 1; tick(); break;"
            " default: g = 2; tick(); } }",
            1,
        ),
        (
            "void f(int c) { switch (c) { case 1: case 2: tick(); g = 1; break;"
            " default: tick(); g = 2; break; } }",
            1,
        ),
        ("void f(int c) { switch (c) { case 1: g = 1; tick(); break; } }", 0),
        (
            "void f(int c) { switch (c) { case 1: g = 1; tick(); return;"
            " default: g = 2; tick(); } }",
            0,
        ),
        ("void f(int c) { switch (c) { case 1: g = 1; default: g = 2; } }", 0),
        ("void f(int c) { switch (c) { tick(); case 1: g = 1; break; default: g = 2; } }", 0),
        (
            "void f(int c) { if (c) { int t = g; g = t; } else { int t = c; g = t; } }",
            0,
        ),
    ],
)
def test_common_arm_statements(tmp_path, body, n):
    """A statement leading every arm is hoistable when it cannot change any condition; one
    ending every arm is sinkable when every arm reaches it; arms all alike are a clone."""
    assert len(sites(tmp_path, body, (), "common-arm-statements")) == n


@pytest.mark.parametrize(
    "body, found",
    [
        ("void f(void) { uint8_t v = 2; do { tick(); v--; } while (v); }", True),
        (
            "void f(void) { uint8_t v; v = 3; for (;;) { tick(); if (v == 0) break; v -= 1; } }",
            True,
        ),
        ("void f(void) { uint8_t v = 2; while (v && (v = v - 1, 1)) { tick(); } }", True),
        ("void f(void) { uint8_t v = 2; g = 1; while (v) { tick(); v = 1 + v; } }", True),
        ("void f(void) { uint8_t v = 2; for (; v; v++) { tick(); } }", True),
        ("void f(void) { uint8_t v; for (v = 2; v; v++) { tick(); } }", False),
        ("void f(void) { uint8_t v; for (v = 2; v;) { tick(); v++; } }", True),
        ("void f(void) { uint8_t v = 2; while (v) { if (g) { continue; } v--; } }", False),
        ("void f(void) { uint8_t v = 2; while (v) { if (g) { v--; } } }", False),
        ("void f(void) { uint8_t v = 2; while (v) { v--; v--; } }", False),
        ("void f(void) { uint8_t v = 2; while (v) { v = v * 2; } }", False),
        ("void f(void) { uint8_t v = 2; while (v) { v = g - v; } }", False),
        ("void f(void) { uint8_t v = 2; while (v) { v += g; } }", False),
        ("void f(void) { uint8_t v = g; while (v) { v--; } }", False),
        ("void f(void) { uint8_t v = 2; g = v; while (v) { v--; } }", False),
        ("void f(void) { uint8_t v = 2; uint8_t *p = &v; while (v) { v--; } (void)p; }", False),
        ("void f(void) { static uint8_t v = 2; while (v) { v--; } }", False),
        ("void f(void) { uint8_t v = 2; while (g) { v--; } }", False),
        ("void f(void) { uint8_t v = 2; while (g || (v = v - 1, 1)) { tick(); } }", False),
        ("void f(void) { uint8_t v = 2; l:; while (v) { v--; if (g) goto l; } }", False),
        ("void f(void) { uint8_t v = 2; l:; while (v) { v--; } if (g) goto l; }", False),
    ],
)
def test_uncounted_loop(tmp_path, body, found):
    """A variable set to a constant before a loop, stepped by a constant once per
    iteration and tested, is a counted loop that should be a ``for`` of the three."""
    assert bool(sites(tmp_path, body, (), "uncounted-loop")) == found


@pytest.mark.parametrize(
    "body, found",
    [
        ("int __attribute__((pure)) h(int); void f(int c) { if (h(c)) { tick(); } }", False),
        ("int __attribute__((const)) h(int); void f(int c) { if (h(c)) { tick(); } }", False),
        ("int h(int c) { return c + g; }\nvoid f(int c) { if (h(c)) { tick(); } }", False),
        ("int h(int c) { int t = c; t++; return t; }\nvoid f(int c) { if (h(c)) tick(); }", False),
        ("int h(int c) { g = c; return c; }\nvoid f(int c) { if (h(c)) { tick(); } }", True),
        ("int h(int c) { tick(); return c; }\nvoid f(int c) { if (h(c)) { tick(); } }", True),
        ("int h(int);\nint j(int c) { return h(c); }\nvoid f(int c) { if (j(c)) tick(); }", True),
        ("int h(int c) { return c ? h(c - 1) : 0; }\nvoid f(int c) { if (h(c)) tick(); }", False),
        ("int h(volatile int *p) { return *p; }\nvoid f(int *p) { if (h(p)) tick(); }", True),
        ("int h(int (*k)(void)) { return k(); }\nvoid f(void) { if (h(0)) tick(); }", True),
        ("void f(int c) { do { tick(); } while (c--); }", True),
        ("void f(int c) { for (c = 0; c < 3; c++) { tick(); } }", False),
        ("void f(int c) { for (; (c = c - 1);) { tick(); } }", True),
    ],
)
def test_effect_in_condition(tmp_path, body, found):
    """A store, comma or call that may have an effect in a condition is a site; a call to a
    function declared or found pure is none."""
    assert bool(sites(tmp_path, body, (), "effect-in-condition")) == found


def test_duplicate_sequences_are_maximal(tmp_path):
    """A repeated sequence is reported once, at its longest, with every copy."""
    body = (
        "void f(int c) { g = 1; tick(); g = 2; if (c) { g = 1; tick(); g = 2; }"
        " { g = 1; tick(); } }"
    )
    got = sites(tmp_path, body, (), "duplicate-code")
    assert sorted(s.detail.split(" at ")[0] for s in got) == ["2 statements", "3 statements"]


def test_duplicate_code_ignores_names_of_its_own_locals(tmp_path):
    """Blocks declaring and using their own variables alike are duplicates."""
    body = "void f(int c) { { int t = c; g = t; } { int t = c; g = t; } }"
    assert len(sites(tmp_path, body, (), "duplicate-code")) == 1


def test_duplicate_code_tells_literals_in_header_macros_apart(tmp_path):
    """Statements alike but for constants their header's macros spell are no duplicates."""
    (tmp_path / "t.h").write_text(
        "".join(f"#define R{k} (b + {k})\n" for k in range(4)), encoding="utf-8"
    )
    body = (
        '#include "t.h"\nuint8_t b[9];\n'
        "void f(void) { R0[0] = 0; R1[0] = 0; R2[0] = 0; R3[0] = 0; }"
    )
    assert not sites(tmp_path, body, (f"-I{tmp_path}",), "duplicate-code")
    same = body.replace("R2", "R0").replace("R3", "R1")
    assert len(sites(tmp_path, same, (f"-I{tmp_path}",), "duplicate-code")) == 1


def test_goto_names_its_label(tmp_path):
    """A goto's detail is its label."""
    (site,) = sites(tmp_path, "void f(void) { goto out; out:; }", (), "goto")
    assert (site.function, site.detail) == ("f", "out")


def test_rejected_c_is_an_error(tmp_path):
    """C the compiler rejects is an error, not an empty census."""
    with pytest.raises(cen.CensusError):
        sites(tmp_path, "void f(void) { undeclared(); }")


def test_tidy_errors_are_errors(tmp_path):
    """C clang-tidy alone rejects is an error."""
    path = tmp_path / "t.c"
    path.write_text("void f(void) {}\n", encoding="utf-8")
    with pytest.raises(cen.CensusError):
        cen.Unit(path).tidy(["-DX=", "-include", str(tmp_path / "missing.h")])


def test_function_at_file_scope(tmp_path):
    """A line outside every function belongs to none."""
    path = tmp_path / "t.c"
    path.write_text(HEAD + "void f(void) {\n tick();\n}\n", encoding="utf-8")
    unit = cen.Unit(path)
    assert (unit.function_at(1), unit.function_at(5)) == ("", "f")


def test_fuzz_corpus_counts():
    """The synthetic players' C has sites of the classes their translation leaves."""
    paths = sorted(FUZZ.glob("*.c"))
    reports = cen.census_all(paths, [FUZZ], quiet=True)
    counts = {p.name: r.counts() for p, r in reports.items()}
    assert all(c["cognitive-complexity"] for c in counts.values())
    assert counts["fuzz-vflag-0.c"]["common-arm-statements"] == 1
    assert counts["fuzz-rmw-1.c"]["uncounted-loop"] == 1
    assert not any(c["goto"] for c in counts.values())


def test_cli_text_and_json(tmp_path, capsys):
    """The command line prints each file's sites and counts, or them as JSON."""
    path = tmp_path / "t.c"
    path.write_text(HEAD + "void f(int c) { if (c) { goto out; } out:; }\n", encoding="utf-8")
    assert main(["census", str(path), "-j", "1"]) == 0
    text = capsys.readouterr().out
    assert "goto [f] out" in text and "goto 1" in text
    assert main(["census", str(path), "--json", "-I", str(tmp_path)]) == 0
    got = json.loads(capsys.readouterr().out)[str(path)]
    assert got["counts"]["goto"] == 1 and got["complexity"] == {"f": 2}
    assert {"cls": "goto", "function": "f", "line": 4, "detail": "out"} in got["sites"]


def test_adapter_keys(tmp_path):
    """The adapter counts sites per key, every class of a file held, and totals each file's
    cognitive complexity."""
    path = tmp_path / "t.c"
    path.write_text(HEAD + "void f(int c) { if (c) { goto out; } out:; }\n", encoding="utf-8")
    got = cen.adapter({"files": [str(tmp_path / "*.c")]})
    assert got["t:goto"] == 1 and got["t:dead-store"] == 0
    assert got["t:cognitive-complexity"] == 2
    assert "t:cognitive-complexity" in got and len(got) == len(cen.CLASSES)
    by_fn = cen.adapter({"files": [str(path)], "key": "{class}/{function}", "total": "{path}"})
    assert by_fn == {"goto/f": 1, str(path): 2}
    with pytest.raises(ValueError):
        cen.adapter({"files": [str(tmp_path / "none-*.c")]})


VC = shutil.which("virtuous-cycles", path=os.path.dirname(sys.executable))


@pytest.mark.skipif(VC is None, reason="needs virtuous-cycles")
def test_ratchet(tmp_path):
    """virtuous-cycles holds the fuzz corpus' census to its budgets through the adapter:
    ``tighten`` records them, ``check`` passes, and one more goto fails it."""
    corpus = tmp_path / "c"
    shutil.copytree(FUZZ, corpus)
    (tmp_path / "virtuous-cycles.toml").write_text(
        '[[metric]]\nname = "census"\nadapter = "wayward-census"\n'
        'budget = "budgets/census.json"\n[metric.options]\nfiles = ["c/*.c"]\n'
        'include = ["c"]\n',
        encoding="utf-8",
    )

    def vc(*args):
        cmd = [VC, "-q", "-c", str(tmp_path / "virtuous-cycles.toml"), *args]
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert vc("tighten").returncode == 0
    budget = json.loads((tmp_path / "budgets" / "census.json").read_text(encoding="utf-8"))
    assert budget["budgets"]["fuzz-rmw-1:uncounted-loop"] == 1
    assert budget["budgets"]["fuzz-loop-0:goto"] == 0
    assert vc("check").returncode == 0
    src = corpus / "fuzz-loop-0.c"
    text = src.read_text(encoding="utf-8")
    src.write_text(
        text.replace("void cgen_play(", "void cgen_jump(void) { goto x; x:; }\nvoid cgen_play(", 1),
        encoding="utf-8",
    )
    failed = vc("check")
    assert failed.returncode == 1 and "fuzz-loop-0:goto" in failed.stdout + failed.stderr
