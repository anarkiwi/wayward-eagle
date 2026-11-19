"""Opaque names, memory blocks, hardware at integer addresses, labels reached only by jumps,
and function size, on synthetic C."""

import pytest
from pycparser import c_ast

from wayward_eagle import c, legible
from wayward_eagle.c import flow

HEAD = "#include <stdint.h>\nint g;\nvoid tick(void);\n__attribute__((noreturn)) void trap(int);\n"


def unit(tmp_path, body, header=None):
    """The unit of C ``body`` after the common declarations, ``header`` as ``t.h``."""
    if header is not None:
        (tmp_path / "t.h").write_text(header, encoding="utf-8")
        body = '#include "t.h"\n' + body
    path = tmp_path / "t.c"
    path.write_text(HEAD + body + "\n", encoding="utf-8")
    return c.load(path, include=[tmp_path])


@pytest.mark.parametrize(
    "name, words",
    [
        ("v13", ["v", "13"]),
        ("f3_L4_x2", ["f", "3", "L", "4", "x", "2"]),
        ("cgen_play_x1", ["cgen", "play", "x", "1"]),
        ("CGEN_OBJ0_ORG", ["CGEN", "OBJ", "0", "ORG"]),
        ("K_AF", ["K", "AF"]),
        ("B0c2", ["B", "0", "c", "2"]),
        ("readHWReg", ["read", "HW", "Reg"]),
        ("__x", ["x"]),
    ],
)
def test_words(name, words):
    """Names split at underscores, letter-digit boundaries and camel case."""
    assert legible.words(name) == words


def test_opaque_names_have_no_meaningful_word():
    """A name is opaque when every word is a single letter, a number or a hex tag, one at
    least a number or tag; a meaningful word with a number beside it is not."""
    names = ["v13", "S_108", "K_AF", "R1", "c48", "f12_1", "x3", "B0c2", "S_1FF"]
    words = ["g_freq_lo_2", "freq_hi", "voice", "ptr", "cgen_play_x1", "CGEN_OBJ0_ORG", "R0_ORG"]
    words += ["a", "i", "x_y"]
    assert legible.opaque(names + words) == set(names)


def test_identifiers_seen_by_the_reader(tmp_path):
    """Declarations, references, labels and expanded object-like macros are named; a name
    only inside a macro's body or only declared in a header is not."""
    u = unit(
        tmp_path,
        "void f(int v1) { if (v1) goto L1; g = W + K_1F; L1: tick(); }",
        "extern int hidden;\n#define W (hidden + 1)\n#define K_1F 0x1F\n",
    )
    names = legible.identifiers(u)
    assert set(names) == {"f", "v1", "g", "W", "K_1F", "L1", "tick", "trap"}
    assert names["v1"][1] == "f" and names["g"][1] == ""
    sites, declared = legible.opaque_names(u)
    assert sorted(s.detail for s in sites) == ["K_1F", "L1", "v1"] and declared == 8


MEMORY = "uint8_t m[64];\nuint8_t t[4];\n"


@pytest.mark.parametrize(
    "body, n",
    [
        ("void f(int i) { m[0x10 + i] = 1; m[0x20 + i] = m[3]; }", 3),
        ("void f(int i, int j) { m[0x10 + i] = 1; m[0x10 + j] = 2; m[0x11 + j] = 3; }", 0),
        ("void f(int i) { m[i] = 1; m[i + 3] = 2; }", 2),
        ("void f(int i, int j) { m[i + j] = 1; m[i + j - 1] = 2; m[i] = m[j]; }", 0),
        ("void f(void) { m[0] = 1; m[0x30] = 2; }", 0),
        ("void f(int i) { uint8_t l[64]; l[i] = 1; l[0x20 + i] = 2; g = l[1]; }", 0),
        ("void f(int i) { W[i] = 1; m[0x10 + i] = 2; t[i] = 3; t[i + 1] = 4; }", 2),
        ("void f(int i) { (m + i)[2] = 1; (m + 0x20)[i] = 2; }", 2),
    ],
)
def test_raw_memory(tmp_path, body, n):
    """Every subscript of a file-scope array whose indexed subscripts touch two objects (no
    shared offset joins them, nor neighbouring ones under one index); constant subscripts
    alone decide nothing; local arrays never; a macro's window into an array is the array."""
    u = unit(tmp_path, MEMORY + body, "extern uint8_t m[64];\n#define W (m + 0x20)\n")
    got = legible.raw_memory(u)
    assert len(got) == n and all(s.function == "f" for s in got)


def test_raw_memory_names_array_and_offset(tmp_path):
    """A raw-memory site's detail is the array and the subscript's constant."""
    u = unit(tmp_path, MEMORY + "void f(int i) { m[0x10 + i] = 1; m[i - 2] = 2; }")
    assert [s.detail for s in legible.raw_memory(u)] == ["m at 0x10", "m at -0x2"]


OUT = "void out(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }\n"


@pytest.mark.parametrize(
    "body, details",
    [
        (
            "void f(uint8_t i) { out(0xD400 + i, 1); out(0xD418, 2); }",
            ["out 0xd400 + index", "out 0xd418"],
        ),
        ("void f(void) { *(volatile uint8_t *)0xD020 = 0; }", ["* 0xd020"]),
        ("void f(void) { ((volatile uint8_t *)0xD800)[g] = 0; }", ["* 0xd800"]),
        (
            "void poke(uint16_t p, uint8_t v) { out(p, v); }\nvoid f(void) { poke(0xD020, 0); }",
            ["poke 0xd020"],
        ),
        (
            "void v(uint8_t o) { out(0xD404 + o, 1); }\nvoid f(void) { v(7); }",
            ["out 0xd404 + index"],
        ),
        ("void f(uint16_t a) { out(a, 1); out(BASE, 2); out(0, 3); }", []),
        ("void f(void) { tick(); trap(0xD020); }", []),
    ],
)
def test_raw_hardware(tmp_path, body, details):
    """An integer converted to a pointer, or passed whole to a parameter that is (through
    wrappers), with a nonzero literal in its constant; a parameter added to a constant is an
    index; a constant a macro names, zero, and a call taking no address are no sites."""
    u = unit(tmp_path, OUT + body, "#define BASE 0xD000\n")
    assert [s.detail for s in legible.raw_hardware(u)] == details


def test_address_parameters(tmp_path):
    """Parameters converted to pointers whole, and those passed whole to them."""
    u = unit(
        tmp_path,
        OUT + "void poke(uint16_t p, uint8_t v) { out(p, v); }\n"
        "void v(uint8_t o) { out(0xD404 + o, 1); }",
    )
    assert legible.address_parameters(u) == {("out", 0), ("poke", 0)}


@pytest.mark.parametrize(
    "body, labels",
    [
        ("if (c) goto l; tick(); return; l: tick();", ["l"]),
        ("if (c) goto l; tick(); l: tick();", []),
        ("if (c) goto l; trap(1); l: tick();", ["l"]),
        ("if (c) goto l; if (g) return; else goto m; l: tick(); m: tick();", ["l"]),
        ("for (;;) { if (c) break; goto l; } return; l: tick();", ["l"]),
        ("return; l: tick();", []),
        ("while (c) { if (g) continue; l: tick(); if (g) goto l; }", []),
    ],
)
def test_labels_after_jump(tmp_path, body, labels):
    """A reachable label every predecessor of which is a goto: what falls into it returns,
    jumps, breaks or calls a noreturn function, on every path; an unreachable label is not."""
    u = unit(tmp_path, f"void f(int c) {{ {body} }}")
    assert [s.detail for s in legible.labels_after_jump(u, u.functions[0])] == labels


def test_goto_vertices(tmp_path):
    """With ``gotos`` a named goto is a vertex entering its label; without, the label is
    entered directly."""
    u = unit(tmp_path, "void f(int c) { if (c) goto l; tick(); l: tick(); }")
    body = u.functions[0].body
    plain, own = flow.build(body), flow.build(body, gotos=True)
    assert len(own.nodes) == len(plain.nodes) + 1
    (x,) = [i for i, n in enumerate(own.nodes) if isinstance(n, c_ast.Goto)]
    assert isinstance(own.origin[own.succ[x][0]], c_ast.Label)


def test_size(tmp_path):
    """Each function's lines and statements: a declaration statement is one however many it
    declares; blocks, labels and empty statements none; a compound statement's own head
    one."""
    u = unit(
        tmp_path,
        "void f(int c) {\n int a, b;\n a = c; b = a;\n if (c) { ; } else g = b;\n"
        " switch (c) { case 1: tick(); break; default:; }\n l: while (c) c--;\n}\n"
        "void h(void) { }",
    )
    lines, stmts = legible.size(u)
    assert lines == {"f": 7, "h": 1}
    assert stmts == {"f": 10, "h": 0}
