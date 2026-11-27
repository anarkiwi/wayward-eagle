"""Constant arguments of pieces: substituted, specialised per value or inlined, refused where
a call's value is unknown, and the rewritten functions behaving as the originals."""

import pytest

from conftest import DRIVER, HEAD, run
from wayward_eagle.c import consts, printer
from wayward_eagle.c.flow import parameters


def pieces(cc, src):
    """The function definitions of ``src`` (pieces first, the function cut last)
    specialised, and their text on one line."""
    funcs = consts.specialised(cc.unit(src).functions)
    return funcs, " ".join(printer.render(funcs).split())


def same(tmp_path, src):
    """``src`` with its pieces specialised, asserting it prints what ``src`` does over the
    driver's inputs."""
    from wayward_eagle.c import front  # pylint: disable=import-outside-toplevel

    path = tmp_path / "orig.c"
    text = HEAD + src + "\n" + DRIVER.replace("RUN", "f()")
    path.write_text(text, encoding="utf-8")
    unit = front.load(path).functions
    funcs = consts.specialised([g for g in unit if g.decl.name in ("h", "f")])
    got = HEAD + printer.render(funcs) + "\n" + DRIVER.replace("RUN", "f()")
    assert run(tmp_path / "spec.c", got) == run(path, text)
    return " ".join(printer.render(funcs).split())


ONE = (
    "static void h(int a, uint8_t b) { w(a + b); w(a - b); }"
    " void f(void) { h(p, 2); if (c0) { h(q, 2); } }"
)
FLAT = (
    "static void h(uint8_t b) { int s = p - (q + (b ^ 1)); w(s); w(s + 1); x(); }"
    " void f(void) { uint8_t t = p; uint8_t v = t > 1; if (t == 2) { h(v); return; }"
    " if (!v) { h(v); return; } h(0); }"
)
TWO = (
    "static void h(uint8_t b) { if (b && c1) { w(p); } else { w(q); } x(); }"
    " void f(void) { uint8_t t = p; uint8_t v = t > 1; if (t == 2) { h(v); return; }"
    " if (!v) { h(v); return; } h(0); }"
)
PINNED = (
    "static void h(uint8_t b) { w(b); } void f(void) { uint8_t t = p; uint8_t v = t == 2;"
    " if (t == 2) { h(v); return; } h(0); }"
)
INLINED = "static void h(uint8_t b) { w(b + 0); } void f(void) { if (c0) { h(1); return; } h(0); }"


def test_one_constant_substituted(cc):
    """A parameter every call passes one constant for is that constant, and goes."""
    funcs, text = pieces(cc, ONE)
    assert "static void h(int a) { w(a + 2); w(a - 2); }" in text
    assert "h(p);" in text and "h(q);" in text
    assert len(funcs) == 2


def test_path_constants_specialised(cc):
    """Constants known from the conditions on the path to each call (through a local's one
    initializer) specialise the piece per value, each simplified, where that lowers the
    cognitive complexity; a copy lowering none leaves the piece."""
    _, text = pieces(cc, TWO)
    assert "b && c1" not in text and "{ w(q); x(); }" in text
    assert "h_1();" in text and "h_0();" in text
    _, text = pieces(cc, FLAT)
    assert "h_1" not in text and "h(v);" in text


def test_small_pieces_inlined(cc):
    """A piece small enough is put at its calls where that costs less than copies."""
    funcs, text = pieces(cc, INLINED)
    assert [f.decl.name for f in funcs] == ["f"]
    assert "w(1);" in text and "w(0);" in text
    _, text = pieces(cc, PINNED)
    assert "w(1);" in text and "w(0);" in text and "h(" not in text


@pytest.mark.parametrize(
    "src",
    [
        # The argument read from memory.
        "static void h(int b) { w(b); w(b + 1); } void f(void) { h(p); h(1); }",
        # The parameter stored.
        "static void h(int b) { b = b + 1; w(b); } void f(void) { h(1); h(1); }",
        # Three values.
        "static void h(int b) { w(b); w(b + 1); x(); }"
        " void f(void) { if (c0) { h(0); return; } if (c1) { h(1); return; } h(2); }",
        # The local tested stored twice.
        "static void h(int b) { w(b); w(b + 1); x(); }"
        " void f(void) { int v = p; v = q; if (v == 1) { h(v); return; } h(1); }",
        # The piece's address taken.
        "static void h(int b) { w(b); w(b + 1); } void f(void) { void (*u)(int) = h; h(1); }",
    ],
)
def test_unknown_values_stay(cc, src):
    """A parameter some call's value is not known for stays."""
    funcs, _ = pieces(cc, src)
    assert parameters(funcs[0])


@pytest.mark.parametrize("src", [ONE, TWO, INLINED, PINNED])
def test_specialised_functions_behave(tmp_path, src):
    """Pieces specialised print what the originals do."""
    same(tmp_path, src)


@pytest.mark.parametrize(
    "expr, want",
    [
        ("((uint16_t)p + (1 ^ 1)) * 2", "(uint16_t)p * 2"),
        ("(uint16_t)p + 0", "(uint16_t)p + 0"),
        ("(g(), q + 0)", "(g(), q + 0)"),
        ("(p + q) + 0", "p + q"),
        ("0 + (p - q)", "p - q"),
        ("-(2 - 3)", "1"),
        ("(uint8_t)1 + 0", "1"),
        ("(0 && p) + q", "0 + q"),
        ("(1 || p) * q", "1 * q"),
        ("(1 && p) + q", "(1 && p) + q"),
        ("!(1 && p)", "!p"),
    ],
)
def test_simplified(cc, expr, want):
    """Constants of type ``int`` folded and zero identities dropped where ``x`` is promoted
    either way."""
    items = cc.items(f"w({expr});")
    got = consts.simplified(items[0])
    assert cc.text([got]) == cc.text(cc.items(f"w({want});"))


@pytest.mark.parametrize(
    "body, want",
    [
        ("if (1 && p) { x(); } else { y(); }", "if (p) { x(); } else { y(); }"),
        ("if (0 && p) { x(); } else { y(); } z();", "y(); z();"),
        ("if (2 - 2) { x(); } z();", "z();"),
    ],
)
def test_constant_conditions(cc, body, want):
    """A condition decided by a constant left operand loses it; an ``if`` of a constant
    condition is the arm it takes."""
    got = consts.simplified(cc.fn(body))
    assert cc.text(got.body.block_items) == cc.text(cc.fn(want).body.block_items)
