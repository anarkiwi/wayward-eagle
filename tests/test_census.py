"""Census of readability defects: each class found where it is and not where it is not."""

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from wayward_eagle import census as cen
from wayward_eagle import adapter as ad
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
    "status-call": (
        "int h(int c) { if (c) { g = c; return 1; } return 0; }\n"
        "void f(int c) { if (h(c)) { return; } tick(); }",
        "int h(int c) { g = c; return c; }\nvoid f(int c) { if (h(c)) { return; } tick(); }",
        (),
    ),
    "copy-variable": (
        "int f(int a) { int v = a; tick(); return v + 1; }",
        "int f(int a) { int v = a; a = 2; return v + a; }",
        (),
    ),
    "edge-copy": (
        "int f(int a, int c) { int v; if (c) { v = a; } else { v = 2; } return v; }",
        "int f(int a, int c) { int v; if (c) { v = a + 1; } else { v = 2; } return v; }",
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
    "label-after-jump": (
        "void f(int c) { if (c) { goto out; } tick(); return; out: tick(); }",
        "void f(int c) { if (c) { goto out; } tick(); out: tick(); }",
        (),
    ),
    "opaque-name": (
        "void f(int v1, int v2) { g = v1 + v2; }",
        "void f(int lo, int hi) { g = lo + hi; }",
        (),
    ),
    "raw-memory": (
        "uint8_t m[64];\nvoid f(int i) { m[0x10 + i] = 1; m[0x20 + i] = 2; }",
        "uint8_t m[64];\nvoid f(int i, int j) { m[i] = 1; m[i + 1] = 2; m[j] = 3; m[5] = 4; }",
        (),
    ),
    "raw-hardware": (
        "void out(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }\n"
        "void f(uint8_t i) { out(0xD400 + i, 1); }",
        "void out(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }\n"
        "void f(uint16_t a) { out(a, 1); }",
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


STATUS = "int h(int c) { if (c) { g = c; return 1; } return 0; }\n"


@pytest.mark.parametrize(
    "body, status",
    [
        ("void f(int c) { if (!h(c)) { return; } tick(); }", True),
        ("void f(int c) { if ((char)h(c) == 1) { tick(); } }", True),
        ("void f(int c) { if (0 != h(c)) { tick(); } }", True),
        ("void f(int c) { while (h(c)) { tick(); } }", True),
        ("void f(int c) { if (h(c) + 1) { tick(); } }", False),
        ("void f(int c) { if (h(c) == g) { tick(); } }", False),
        ("void f(int c) { if (h(c = 2)) { tick(); } }", False),
        ("void f(int c) { int a[2] = {0}; if (h(c)) { tick(); } g = a[c]; }", False),
        ("void f(int c) { int x = 0; int *p = &x; if (h(c)) { tick(); } g = *p; }", False),
        ("int k(int);\nvoid f(int c) { if (k(c)) { tick(); } }", False),
        ("int k(int c) { g = c; return g; }\nvoid f(int c) { if (k(c)) { tick(); } }", False),
        ("int k(int c) { g = c; return 0; }\nvoid f(int c) { if (k(c) && h(c)) tick(); }", False),
    ],
)
def test_status_call(tmp_path, body, status):
    """A condition that is only a call (negated, cast or compared with a constant) of a
    function here returning exit codes, its arguments and the caller's objects out of its
    reach, is a status call and no effect in a condition."""
    got = {s.cls for s in sites(tmp_path, STATUS + body)}
    assert ("status-call" in got, "effect-in-condition" in got) == (status, not status)


@pytest.mark.parametrize(
    "body, detail",
    [
        ("int f(int a) { int v = a; tick(); return v + 1; }", "v copies a"),
        ("int f(void) { int v = g; return v + 1; }", "v copies g"),
        ("int f(void) { int v = g; tick(); return v; }", None),
        (
            "int h(int c) { return c + 1; }\nint f(void) { int v = g; return h(v) + v; }",
            "v copies g",
        ),
        ("int f(int a, int c) { int v = a; if (c) { v = 3; } return v; }", None),
        ("int f(void) { uint8_t v = 2; tick(); return v; }", "v copies 2"),
        ("int f(long a) { int v = a; return v; }", None),
        ("int f(void) { int v; v = g; return v; }", "v copies g"),
        (
            "int f(int a) { int s = 0; int v = a; for (int i = 0; i < 3; i++) { s += v; }"
            " return s; }",
            "v copies a",
        ),
        ("volatile int vg;\nint f(void) { int v = vg; return v; }", None),
        ("int f(int a) { int v = a; int *p = &v; return *p; }", None),
        ("int f(int a, int b) { int v = a; return (a = b, v); }", None),
        ("int f(int a) { int v = a; a = v + 1; return a; }", "v copies a"),
        ("int f(int a, int c) { int v; if (c) { v = a; } return v; }", None),
        ("int f(int a) { int v = a; l: tick(); if (g) { goto l; } return v; }", "v copies a"),
        ("int f(int a) { int v = a; l: a++; if (g) { goto l; } return v; }", None),
        ("void f(int a) { int v = a; }", None),
        (
            "int f(int a, int c) { int v = a; switch (c) { case 1: a = 0; break;"
            " default: break; } return v; }",
            None,
        ),
        ("int f(int a, int *p) { int v = a; *p = 1; return v; }", "v copies a"),
        ("int f(int *p) { int v = g; *p = 1; return v; }", None),
        ("uint8_t b[2];\nint f(void) { int v = g; b[1] = 1; return v; }", "v copies g"),
        ("void f(int a) { int v = a; while (v) { a = 0; } }", None),
    ],
)
def test_copy_variable(tmp_path, body, detail):
    """A copy whose variable its source could replace at every use it reaches, every use
    reached by it alone and with neither side changed since, is an extraneous variable."""
    got = [s.detail for s in sites(tmp_path, body, (), "copy-variable")]
    assert got == ([detail] if detail else [])


@pytest.mark.parametrize(
    "body, n",
    [
        ("int f(int a, int c) { int v; if (c) { v = a; } else { v = 2; } return v; }", 1),
        ("void f(int c, int x) { uint8_t v; if (c) { v = 2; } else { g = x; v = 2; } g = v; }", 0),
        ("void f(int c, int x) { uint8_t v; if (c) { v = 2; tick(); } else { v = 2; } g = v; }", 0),
        (
            "void f(int c, int x) { uint8_t v; if (c) { g = 1; v = 2; g = 3; }"
            " else { g = x; v = 2; tick(); } g = v; }",
            1,
        ),
        ("int f(int a, int c) { int v = a; if (c) { g = 1; } return v; }", 0),
        ("int f(int a, int c) { int v; if (c) { v = a; g = v; } else { v = 2; } return v; }", 0),
        ("int f(int a, int c) { int v = 1; if (c) { v = a; } return v; }", 1),
        ("int f(int a) { int v = 0; while (g) { tick(); v = a; } return v; }", 1),
        ("int f(void) { int v = 0; while (v < 3) { v = v + 1; } return v; }", 0),
        ("int f(int a) { int v = 0; l: tick(); if (g) { v = a; goto l; } return v; }", 1),
        (
            "int f(int a, int c) { int v; switch (c) { case 1: v = a; break;"
            " default: v = 3; } return v; }",
            1,
        ),
        (
            "int f(int a, int c) { int v, w; if (c) { v = a; w = 1; } else { v = 2; w = a; }"
            " return v + w; }",
            2,
        ),
    ],
)
def test_edge_copy(tmp_path, body, n):
    """Distinct copies of one variable that are the last stores to it on two or more edges
    into a join, unread since, are one site per join and variable; the same statement
    leading or ending every arm is common-arm-statements' alone."""
    assert len(sites(tmp_path, body, (), "edge-copy")) == n


def test_edge_copy_names_its_lines(tmp_path):
    """An edge copy's detail is the variable and the lines of its copies."""
    body = "int f(int a, int c) {\n int v;\n if (c)\n v = a;\n else\n v = 2;\n return v;\n}"
    (site,) = sites(tmp_path, body, (), "edge-copy")
    assert (site.function, site.line, site.detail) == ("f", 6, "v: lines 7, 9")


def test_locals_per_function(tmp_path):
    """Each function's automatic variables are counted, parameters and statics aside."""
    path = tmp_path / "t.c"
    path.write_text(
        HEAD + "void f(int c) { int a, b; static int s; { int t = c; g = t + a + b + s; } }\n"
        "void h(void) { tick(); }\n",
        encoding="utf-8",
    )
    assert cen.census(path).locals == {"f": 3, "h": 0}


def test_duplicate_sequences_are_maximal(tmp_path):
    """A repeated sequence is reported once, at its longest, with every copy."""
    body = (
        "void f(int c) { g = 1; tick(); g = 2; if (c) { g = 1; tick(); g = 2; }"
        " { g = 1; tick(); } }"
    )
    got = sites(tmp_path, body, (), "duplicate-code")
    assert sorted(s.detail.split(" at ")[0] for s in got) == ["2 statements", "3 statements"]


def test_duplicate_code_leaves_tail_calls(tmp_path):
    """A call and a ``return;`` repeated is the call of shared code, no duplicate; a call
    and another statement repeated is."""
    body = "void f(int c) { if (c) { tick(); return; } if (g) { tick(); return; } tick(); }"
    assert not sites(tmp_path, body, (), "duplicate-code")
    body = "void f(int c) { if (c) { tick(); g = 1; } if (g) { tick(); g = 1; } }"
    assert len(sites(tmp_path, body, (), "duplicate-code")) == 1


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
    assert got["locals"] == {"f": 0} and got["lines"] == {"f": 1} and got["statements"] == {"f": 2}
    assert got["names"] == {"opaque": 0, "declared": 5}
    assert got["reasons"] == {"f": dict.fromkeys(cen.REASONS, 0)}
    assert {"cls": "goto", "function": "f", "line": 4, "detail": "out"} in got["sites"]


def test_adapter_keys(tmp_path):
    """The adapter counts sites per key, every class of a file held, and totals each file's
    cognitive complexity."""
    path = tmp_path / "t.c"
    path.write_text(
        HEAD + "void f(int c) { int x = c; if (x) { goto out; } out:; }\n", encoding="utf-8"
    )
    got = ad.adapter({"files": [str(tmp_path / "*.c")]})
    assert got["t:goto"] == 1 and got["t:dead-store"] == 0
    assert got["t:cognitive-complexity"] == 2 and got["t:locals"] == 1
    assert got["t:lines"] == 1 and got["t:statements"] == 3
    assert got["t:locals:single-use"] == 1 and got["t:locals:clobbered"] == 0
    assert len(got) == len(cen.CLASSES) + 3 + len(cen.REASONS)
    by_fn = ad.adapter(
        {
            "files": [str(path)],
            "key": "{class}/{function}",
            "total": "{path}",
            "locals": "",
            "reasons": "",
            "lines": "",
            "statements": "",
        }
    )
    assert by_fn == {"goto/f": 1, "copy-variable/f": 1, str(path): 2}
    with pytest.raises(ValueError):
        ad.adapter({"files": [str(tmp_path / "none-*.c")]})


def test_adapter_combine(tmp_path):
    """``combine`` replaces its classes' keys by one of their weighted site counts, per file
    and per function, every other key unchanged."""
    path = tmp_path / "t.c"
    path.write_text(
        HEAD + "uint8_t m[64];\nvoid f(int v1) { m[0x10 + v1] = 1; m[0x20 + v1] = 2; }\n",
        encoding="utf-8",
    )
    weights = {"opacity": {"opaque-name": 1, "raw-memory": 2}}
    plain = ad.adapter({"files": [str(path)]})
    got = ad.adapter({"files": [str(path)], "combine": weights})
    assert plain["t:opaque-name"] == 1 and plain["t:raw-memory"] == 2
    assert got.pop("t:opacity") == 5
    assert got == {k: v for k, v in plain.items() if k not in ("t:opaque-name", "t:raw-memory")}
    by_fn = ad.adapter({"files": [str(path)], "key": "{class}/{function}", "combine": weights})
    assert by_fn["opacity/f"] == 5 and not {"opaque-name/f", "raw-memory/f"} & set(by_fn)


def test_adapter_combine_measures(tmp_path):
    """``combine`` weights the per-file measures as it does classes: their keys replaced by
    one of the weighted sum, and a name of no class or measure refused."""
    path = tmp_path / "t.c"
    path.write_text(
        HEAD + "uint8_t m[64];\nvoid f(int c) { int a, b; { int t = c; m[t] = a + b; } }\n",
        encoding="utf-8",
    )
    plain = ad.adapter({"files": [str(path)]})
    weights = {
        "size": {"lines": 1, "statements": 2},
        "defs": {f"locals:{r}": 1 for r in cen.REASONS},
    }
    got = ad.adapter({"files": [str(path)], "combine": weights})
    assert got.pop("t:size") == plain["t:lines"] + 2 * plain["t:statements"]
    reasons = [f"t:locals:{r}" for r in cen.REASONS]
    assert got.pop("t:defs") == sum(plain[k] for k in reasons) > 0
    gone = {"t:lines", "t:statements", *reasons}
    assert got == {k: v for k, v in plain.items() if k not in gone}
    with pytest.raises(ValueError, match="no class or measure"):
        ad.adapter({"files": [str(path)], "combine": {"x": {"lnes": 1}}})


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


DEVICE = (
    "uint8_t a[2], b[2];\n"
    "void out(uint16_t p, uint8_t v) { *(volatile uint8_t *)p = v; }\n"
    "uint8_t in(uint16_t p) { return *(volatile uint8_t *)p; }\n"
    "__attribute__((noreturn)) void die(void);\n"
    "__attribute__((const)) int k(void);\n"
    "__attribute__((pure)) int r(void);\n"
    "void put(int v) { g = v; }\n"
    "void check(int v) { if (v) { die(); } }\n"
)

#: Function (after HEAD and DEVICE): the reason of each definition, by its source.
REASONS = [
    ("int f(void) { int t = g + 1; return t; }", {"int t = g + 1": "single-use"}),
    ("void f(void) { int t = g; tick(); g = t; }", {"int t = g": "clobbered"}),
    ("void f(void) { int t = g; k(); r(); g = t; }", {"int t = g": "single-use"}),
    ("void f(void) { int t = a[0]; out(1, 2); b[1] = t; }", {"int t = a[0]": "single-use"}),
    ("void f(void) { int t = in(1); out(2, 3); g = t; }", {"int t = in(1)": "clobbered"}),
    ("void f(void) { int t = a[0]; b[0] = 1; g = t; }", {"int t = a[0]": "single-use"}),
    ("void f(void) { int t = a[0]; a[1] = 1; g = t; }", {"int t = a[0]": "single-use"}),
    ("void f(void) { int t = a[0]; a[0] = 1; g = t; }", {"int t = a[0]": "clobbered"}),
    ("void f(int c) { int t = a[0]; a[c] = 1; g = t; }", {"int t = a[0]": "clobbered"}),
    ("void f(int c) { int t = a[c]; a[1] = 1; g = t; }", {"int t = a[c]": "clobbered"}),
    ("void f(uint8_t *p) { int t = a[1]; *p = 1; g = t; }", {"int t = a[1]": "clobbered"}),
    ("void f(void) { int t = *(a + 1); a[1] = 1; g = t; }", {"int t = *(a + 1)": "clobbered"}),
    ("void f(int *p) { int t = a[0]; *p = 1; g = t; }", {"int t = a[0]": "clobbered"}),
    (
        "static const int K[2] = {1, 2};\nvoid f(int c, int *p) { int t = K[c]; *p = 0; g = t; }",
        {"int t = K[c]": "single-use"},
    ),
    ("int f(void) { int t = g; return t * t; }", {"int t = g": "multi-use"}),
    ("int f(int c) { int t = c + 1; c = 0; return t + c; }", {"int t = c + 1": "clobbered"}),
    (
        "int f(int c) { int t = c; t = t + 1; return t; }",
        {"int t = c": "single-use", "t = t + 1": "clobbered"},
    ),
    (
        "int f(int c) { int t; if (c) { t = 1; } else { t = 2; } return t; }",
        {"t = 1": "merged", "t = 2": "merged"},
    ),
    ("void f(void) { int t = g; t = 2; g = t; }", {"int t = g": "unused", "t = 2": "single-use"}),
    (
        "void f(int c) { int t = c - 3; if (t > 5) { die(); } g = c; }",
        {"int t = c - 3": "check-only"},
    ),
    (
        "void f(int c) { int t = c - 3; if (t > 5) { die(); } g = t; }",
        {"int t = c - 3": "multi-use"},
    ),
    (
        "void f(void) { for (;;) { int t = in(1); if (t) { g = 1; } else { while (1) {} } } }",
        {"int t = in(1)": "single-use"},
    ),
    ("void f(void) { int t = g; g = t + 1; }", {"int t = g": "single-use"}),
    ("void f(void) { int t = g; b[0] = (g = 1) + t; }", {"int t = g": "clobbered"}),
    ("void f(void) { int t = g; put(t); }", {"int t = g": "single-use"}),
    ("void f(void) { int t = g; check(1); b[0] = t; }", {"int t = g": "single-use"}),
    ("void f(void) { int t = in(1); check(0); g = t; }", {"int t = in(1)": "clobbered"}),
    ("void f(int n) { int t = g; while (n--) { g = 0; } b[0] = t; }", {"int t = g": "clobbered"}),
    ("void f(int *p) { int t = sizeof(*p); *p = 1; g = t; }", {"int t = sizeof(*p)": "single-use"}),
    ('void f(void) { int t = g; __asm__(""); b[0] = t; }', {"int t = g": "clobbered"}),
]


@pytest.mark.parametrize("body, want", REASONS)
def test_reasons_of_definitions(tmp_path, body, want):
    """Each definition of a local variable has the first reason that holds of it."""
    path = tmp_path / "t.c"
    path.write_text(HEAD + DEVICE + body + "\n", encoding="utf-8")
    unit = cen.Unit(path)
    got = {unit.c.source(d.node): r for d, r in unit.flow(unit.functions[-1]).temporaries()}
    assert {k: v for k, v in got.items() if k in want} == want
    counts = cen.census(path).reasons
    assert sum(counts["f"].values()) == len(want) and set(counts["f"]) == set(cen.REASONS)


def test_call_access(tmp_path):
    """A call reads and writes its definition's locations (its own automatic variables
    aside), a device for volatile accesses and for a function never returning, nothing for
    ``const``, memory reads for ``pure``, everything for any other."""
    path = tmp_path / "t.c"
    path.write_text(
        HEAD
        + DEVICE
        + "void f(int (*h)(void)) { put(1); out(1, 2); check(0); k(); r(); tick(); h(); }\n",
        encoding="utf-8",
    )
    unit = cen.Unit(path)
    got = [unit.call_access(x) for x in cen.q.calls(unit.functions[-1])]
    g = next(i for i, s in unit.c.symbols.items() if s.name == "g")
    dev, every = frozenset({"device"}), frozenset({"device", "memory"})
    assert got == [
        (frozenset(), frozenset({g})),
        (dev, dev),
        (dev, dev),
        (frozenset(), frozenset()),
        (frozenset({"memory"}), frozenset()),
        (every, every),
        (every, every),
    ]
