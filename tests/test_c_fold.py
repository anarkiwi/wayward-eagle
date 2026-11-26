"""Single-use temporaries folded into their use: where the value read and every effect's
order are kept, and the cost does not rise."""

import pytest
from pycparser import c_ast

from conftest import LOCALS, behaves
from wayward_eagle.c import fold, ints, stmt
from wayward_eagle.c import reduce as r

DECLS = (
    "uint8_t R[8]; uint16_t W; volatile uint8_t V;\n"
    "uint8_t h(uint8_t); void put(uint16_t a, uint8_t b); int u(void);\n"
)


def folded(cc, body, sig="void f(void)", decls=DECLS, size=fold.SIZE):
    """``body``'s statements (after :data:`DECLS` and :data:`conftest.LOCALS`, and
    ``uint8_t`` locals ``b1``..``b3``) folded, as one line of C."""
    unit = cc.unit(f"{decls}{sig} {{ {LOCALS} uint8_t b1, b2, b3; uint16_t s1; {body} }}")
    fd = unit.functions[-1]
    types = dict(r._params(fd))  # pylint: disable=protected-access
    items = r._normal(stmt.block(fd.body), types)  # pylint: disable=protected-access
    ret = None if r._returns_void(fd) else fd.decl.type.type  # pylint: disable=protected-access
    got = fold.Folder(types, ret, r.declarators(unit), size=size)(items)
    return cc.text(got)


@pytest.mark.parametrize(
    "body,sig,want",
    [
        ("b1 = h(b2); return b1;", "uint8_t f(void)", "{ return h(b2); }"),
        (
            "s1 = R[0] | (uint16_t)R[1] << 8; put(s1, 0);",
            None,
            "{ put(R[0] | ((uint16_t)R[1] << 8), 0); }",
        ),
        ("b1 = W >> 8; put(0, b1);", None, "{ put(0, W >> 8); }"),
        ("b1 = b2 > b3; R[0] = R[1] - (R[2] + b1);", None, "{ R[0] = R[1] - (R[2] + (b2 > b3)); }"),
        ("b1 = R[0]; b2 = R[1]; R[2] = b1 + b2;", None, "{ R[2] = R[0] + R[1]; }"),
        ("b1 = b2 + 1; R[0] = b1;", None, "{ R[0] = b2 + 1; }"),
        ("b1 = b2 & 0x0F; R[b1] = 0;", None, "{ R[b2 & 0x0F] = 0; }"),
        ("b1 = b2 < 3; if (b1) { x(); }", None, "{ if (b2 < 3) { x(); } else { } }"),
        ("b1 = b2 - b3; R[0] = 1; put(0, b1);", None, "{ R[0] = 1; put(0, b2 - b3); }"),
    ],
)
def test_a_single_use_folds(cc, body, sig, want):
    """A temporary read once takes its value's place where that reads the same value."""
    assert folded(cc, body, sig or "void f(void)") == want


@pytest.mark.parametrize(
    "body",
    [
        "b1 = R[0]; R[0] = 1; put(b1, 0);",  # a store to memory between
        "b1 = R[0]; x(); put(b1, 0);",  # a call between
        "b1 = b2; b2 = 1; put(b1, b2); R[0] = b2;",  # a store to what it reads between
        "b1 = R[0]; put(b1, b1);",  # read twice
        "b1 = R[0]; if (p) { b1 = 1; } put(b1, 0);",  # another definition reaches the use
        "b1 = R[0]; while (p) { put(b1, 0); p--; }",  # the use in a loop the definition is not
        "b1 = h(1); R[0] = 2; put(b1, 0);",  # an effect between it and the use
        "b1 = h(1); put(R[0], b1);",  # an effect reordered with a read in the use
        "b1 = h(1); if (p) { put(0, b1); }",  # an effect made conditional
        "b1 = h(1); R[0] = p && b1;",  # an effect made conditional by a logical operator
        "b1 = b2 + 1; R[b1] = 0;",  # its value wraps where its type matters
        "b1 = b2 + 1; if (b1) { x(); }",  # its value wraps in a condition
        "b1 = V; put(b1, 0);",  # volatile
        "b1 = *(volatile uint8_t *)0xD000; put(b1, 0);",  # volatile
        "b1 = R[0]; R[1] = sizeof(b1);",  # under sizeof
        "b1 = R[0]; R[b1] = b1++;",  # stored in the use
        "b1 = b1 + 1; put(b1, 0);",  # reads itself
        "b1 = (b2 = 1); put(b1, 0);",  # stores
        "b1 = p ? 1 : 2; if (q) { put(b1, 0); }",  # deeper nesting costs more
        "b1 = (R[0], R[1]); put(b1, 0);",  # a comma
        "b1 = u(); put(b1, 0);",  # an int into a uint8_t: the conversion matters
        "b1 = b2 - b3; put(b1, 0);",  # a uint8_t into a uint16_t: the conversion matters
    ],
)
def test_a_fold_that_changes_what_runs_is_refused(cc, body):
    """A fold that may read another value, reorder or drop an effect, or cost more, stays."""
    assert "b1 =" in folded(cc, body)


def test_a_fold_past_the_size_stays(cc):
    """A use whose expression would hold more than ``size`` nodes keeps the temporary."""
    body = "s1 = R[0] | (uint16_t)R[1] << 8; put(s1, 0);"
    assert "s1 =" in folded(cc, body, size=6)
    assert "s1 =" not in folded(cc, body)


def test_folds_chain(cc):
    """A temporary folded into another's definition folds on with it."""
    got = folded(cc, "b1 = R[0]; b2 = b1 >> 1; b3 = b2 & 3; put(0, b3);")
    assert got == "{ put(0, (R[0] >> 1) & 3); }"


def test_unknown_types_fold_only_where_converted_to_the_same(cc):
    """Without the file's declarations, a value of unknown type folds only where the use
    converts it to the temporary's own type."""
    body = "uint8_t b1, b2, b3; b1 = R[0]; b2 = R[1]; b3 = b1 - b2; return b3;"
    unit = cc.unit(f"{DECLS}uint8_t f(void) {{ {body} }}")
    fd = unit.functions[-1]
    types = {}
    items = r._normal(stmt.block(fd.body), types)  # pylint: disable=protected-access
    got = fold.folded(items, types, fd.decl.type.type)
    assert cc.text(got) == "{ b1 = R[0]; b2 = R[1]; return b1 - b2; }"


def test_bounds():
    """Expressions' ranges follow C's conversions in both data models."""
    u8 = c_ast.TypeDecl("a", [], None, c_ast.IdentifierType(["uint8_t"]))
    i16 = c_ast.TypeDecl("s", [], None, c_ast.IdentifierType(["int16_t"]))
    types = fold.Types({"a": u8, "s": i16})

    def bound(text, bits=16):
        got = types.bound(_parse(text), bits)
        return None if got is None else (got.lo, got.hi)

    assert bound("a + 1") == (1, 256)
    assert bound("a >> 4") == (0, 15)
    assert bound("a << 1") == (0, 510)
    assert bound("a & 7") == (0, 7)
    assert bound("a | 0x10") == (0, 255)
    assert bound("a / 2") == (0, 127)
    assert bound("a % 10") == (0, 9)
    assert bound("a * 2") == (0, 510)
    assert bound("a - 1") == (-1, 254)
    assert bound("a < 3") == (0, 1)
    assert bound("!a") == (0, 1)
    assert bound("-a") == (-255, 0)
    assert bound("~a") == (-32768, 32767)
    assert bound("+a") == (0, 255)
    assert bound("(uint8_t)(a + 1)") == (0, 255)
    assert bound("(uint16_t)a") == (0, 255)
    assert bound("a ? a : 300") == (0, 300)
    assert bound("s & 1") == (-32768, 32767)
    assert bound("a >> s") == (-32768, 32767)
    assert bound("z") is None
    assert bound("z + 1") is None
    assert bound("a ? z : 1") is None
    assert bound("&a") is None
    assert bound("a, a") is None
    assert bound("(uint16_t)a * 0x200", 16) == (0, 0xFFFF)
    assert bound("(uint16_t)a * 0x200", 32) == (0, 255 * 0x200)


def _parse(text):
    from pycparser import CParser  # pylint: disable=import-outside-toplevel

    src = f"typedef int uint8_t; typedef int uint16_t; int z; void f(void) {{ {text}; }}"
    return CParser().parse(src).ext[-1].body.block_items[0]


def test_facts_and_clashes():
    """What an expression reads, stores and changes, and which pairs may not swap."""
    e = _parse("R[a] = h(b) + c")
    got = fold.facts(e, {"a", "b", "c"})
    assert got.reads == {"a", "b", "c"} and got.memory and got.effect and not got.stores
    loc = fold.facts(_parse("a = b + 1"), {"a", "b"})
    assert loc.stores == {"a"} and not loc.memory and not loc.effect
    assert fold.clash(loc, fold.facts(_parse("a"), {"a"}))
    assert not fold.clash(fold.facts(_parse("b"), {"b"}), fold.facts(_parse("R[0]"), set()))
    assert fold.clash(fold.facts(_parse("R[0]"), set()), fold.facts(_parse("h(1)"), set()))
    assert fold.clash(fold.facts(_parse("h(1)"), set()), fold.facts(_parse("R[0]"), set()))
    assert fold.facts(_parse("p->f"), set()).memory
    assert fold.facts(_parse("*p"), {"p"}).memory
    assert not fold.facts(_parse("R[0] = a"), {"a"}, arrays={"R"}).memory
    assert fold.facts(_parse("R[0] = a"), {"a"}).memory
    assert fold.facts(_parse("R[0] += a"), {"a"}, arrays={"R"}).memory


def test_types_of_names():
    """Names' integer types: locals, file-scope objects and arrays (not volatile), calls'
    returns and prototypes."""
    t = fold.Types({}, {"g": _decl("uint8_t g;"), "A": _decl("uint8_t A[4];")})
    t.outer["h"] = _decl("uint8_t h(uint16_t, ...);")
    t.outer["v"] = _decl("volatile uint8_t v;")
    t.outer["k"] = _decl("void k(void);")
    assert t.variable("g", 16) == ints.Int(8, False)
    assert t.variable("v", 16) is None
    assert t.variable("A", 16) is None
    call = _parse("h(1, 2)")
    assert t.returned(call, 16) == ints.Int(8, False)
    assert ints.type_of(t.parameter(call, 0), 16) == ints.Int(16, False)
    assert t.parameter(call, 1) is None
    assert t.parameter(_parse("k()"), 0) is None
    assert t.parameter(_parse("m(1)"), 0) is None


def _decl(text):
    from pycparser import CParser  # pylint: disable=import-outside-toplevel

    return CParser().parse("typedef int uint8_t; typedef int uint16_t;" + text).ext[-1].type


def test_declarators_of_a_unit(cc):
    """The file-scope objects and functions of a unit, typedefs aside."""
    got = r.declarators(cc.unit(DECLS + "static int f(void) { return 0; }"))
    assert {"R", "W", "V", "h", "put", "u", "f", "p"} <= set(got)
    assert "uint8_t" not in got
    assert isinstance(got["R"], c_ast.ArrayDecl) and isinstance(got["f"], c_ast.FuncDecl)


@pytest.mark.parametrize(
    "src,gone",
    [
        (
            "uint8_t A[4] = {1, 2, 3, 250};\n"
            "uint8_t h(uint8_t a) { return a + 1; }\n"
            "void f(void) { uint8_t b, c, d; b = A[p]; c = A[q] > b; d = h(b);"
            " A[0] = d + c; w(A[0]); c = A[k & 3] + 6; w(c); }",
            ["d = "],
        ),
        (
            "uint8_t A[4] = {1, 2, 3, 250};\n"
            "void f(void) { uint8_t b; uint16_t s; b = A[p]; A[p] = 0;"
            " s = A[q] | (uint16_t)A[k & 3] << 8; w(s >> 4); w(b);"
            " b = A[q] + 7; if (b) { w(1); } }",
            ["s = "],
        ),
        (
            "int A[4] = {1, 2, 3, 4};\n"
            "void f(void) { int a, b, c; a = g(); b = g(); c = a - b; w(c);"
            " a = A[p]; while (q) { q--; w(a); } }",
            ["c = "],
        ),
    ],
)
def test_folded_functions_behave(tmp_path, src, gone):
    """Whole functions with temporaries folded and kept print what they did."""
    got = behaves(tmp_path, src).split("void f(void)")[-1]
    assert not [g for g in gone if g in got]


def test_a_statement_at_two_positions_folds_at_one(cc):
    """A statement object standing at two positions (a tail's call, shared) is rewritten
    only where the fold is."""
    unit = cc.unit(
        DECLS + "void f(void) { uint8_t b1; if (p) { b1 = R[0]; put(b1, 0); return; }"
        " b1 = R[1]; put(b1, 0); }"
    )
    fd, types = unit.functions[-1], {}
    items = r._normal(stmt.block(fd.body), types)  # pylint: disable=protected-access
    arm = stmt.block(items[0].iftrue)
    items[-1] = arm[1]
    got = fold.folded(items, types, None, r.declarators(unit))
    assert cc.text(got) == "{ if (p) { put(R[0], 0); return; } else { } put(R[1], 0); }"
