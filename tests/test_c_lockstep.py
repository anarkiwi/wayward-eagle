"""Counters stepped in lockstep folded into one counted loop."""

import pytest
from pycparser import c_ast

from conftest import LOCAL, behaves, reduced
from wayward_eagle.c import lockstep

U8 = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["uint8_t"]))
U16 = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["uint16_t"]))
LOOP = "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31, v2 = v2 - 1) { w(v1); w(v2); }"


def body(text):
    """The reduced C of function ``f`` in ``text`` (from ``f``'s definition on)."""
    return " ".join(text[text.index("\nvoid f(") :].split())


def folded(cc, src, types=None):
    """``src``'s statements with their lockstep counters folded."""
    return lockstep.lockstepped(cc.items(src), types or {v: U8 for v in LOCAL})


@pytest.mark.parametrize(
    "src,want",
    [
        (LOOP, "for (v2 = 2; v2 < 3; v2 = v2 - 1) { w(0x31 * v2); w(v2); }"),
        (
            "v2 = 2; for (v1 = 2; v1 != 0xFF; v1 = v1 - 1, v2 = v2 - 1) { w(v2); }",
            "for (v1 = 2; v1 < 3; v1 = v1 - 1) { w(v1); }",
        ),
        (
            "v2 = 0x10; for (v1 = 0; v1 != 3; v1 = v1 + 1, v2 = v2 - 0x31) { w(v2); }",
            "for (v1 = 0; v1 < 3; v1 = v1 + 1) { w((uint8_t)(0x10 - 0x31 * v1)); }",
        ),
        (
            "v2 = 9; for (v1 = 0; v1 < 4; v1 = v1 + 1, v2 = v2 + 2) { w(v2); }",
            "for (v1 = 0; v1 < 4; v1 = v1 + 1) { w(2 * v1 + 9); }",
        ),
        (
            "v2 = 3; for (v1 = 5; v1 != 7; v1 = v1 + 1, v2 = v2 - 1) { w(v2); }",
            "for (v1 = 5; v1 < 7; v1 = v1 + 1) { w(8 - v1); }",
        ),
    ],
)
def test_counters_fold_into_one(cc, src, want):
    """Each counter dead after the loop is an affine function of one stepped by one."""
    assert cc.same(folded(cc, src), want)


@pytest.mark.parametrize(
    "src",
    [
        LOOP + " w(v1); w(v2);",
        "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31, v2 = v2 - 2) { w(v2); }",
        "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31, v2 = v2 - 1) { v2 = 1; }",
        "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31, v2 = v2 - 1) { p = *&v2; }",
        "v2 = p; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31, v2 = v2 - 1) { w(v2); }",
        "v2 = 2; for (v1 = 0x62; v1 != p; v1 = v1 - 0x31, v2 = v2 - 1) { w(v2); }",
        "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31, v2 = v2 - v1) { w(v2); }",
        "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31) { w(v2); }",
        "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x31, v1 = v1 - 1) { w(v2); }",
        "v2 = 2; for (v1 = 0x62; v1 != 0xCF; v1 = v1 - 0x30, v2 = v2 - 1) { w(v2); }",
        "v2 = 2; w(v2); for (v1 = 1; v1 != 3; v1 = v1 + 1, v2 = v2 - 1) { w(v2); }",
        "v2 = 2; for (v1 = 1; v1 != 4; v1 = v1 + 1, v2 = v2 * 3) { w(v2); }",
    ],
)
def test_loops_that_do_not_fold_stay(cc, src):
    """No fold when the other counter is read after the loop, steps by neither one nor in
    step, is stored or addressed in the body, starts unknown, the test reads another
    variable, there is one step or one variable stepped twice, the loop never ends, or no
    affine function gives the other."""
    assert cc.same(folded(cc, src), src)


def test_counters_promoted_past_int_stay(cc):
    """A counter of a type not promoted to ``int`` in each model stays."""
    src = "v2 = 2; for (v1 = 0; v1 != 3; v1 = v1 + 1, v2 = v2 - 1) { w(v2); }"
    assert cc.same(folded(cc, src, {v: U16 for v in LOCAL}), src)


@pytest.mark.parametrize(
    "start,stride,last,index",
    [
        (0x62, 0x31, 0x00, 2),
        (0x10, 0x31, 0xAE, 2),
        (0xFE, 0xFF, 0x01, 3),
        (0x03, 0x81, 0x82, 1),
        (0x00, 0x40, 0x80, 2),
    ],
)
def test_a_wrapping_counter_behaves(tmp_path, start, stride, last, index):
    """A counter wrapping mod 256 beside one counting down to zero runs the same values
    in the same order, the loop counting the index alone, an ``int`` down to 0."""
    src = (
        f"void f(void) {{ uint8_t v = 0x{start:X}; uint8_t u = {index}; for (;;) {{ w(v); w(u);"
        f" if (v == 0x{last:X}) break; v = v - 0x{stride:X}; u = u - 1; }} }}"
    )
    got = body(behaves(tmp_path, src))
    assert f"for (int u = {index}; u >= 0; u = u - 1)" in got


LOOP8 = "for (;;) { w(v); w(u); if (v == 0) break; v = v - 0x31; u = u - 1; }"


@pytest.mark.parametrize(
    "src,sig,want",
    [
        (f"uint8_t v = 0x62; u = 2; {LOOP8}", "void f(uint8_t u)", "u < 3"),
        (f"uint8_t v = 0x62, u = 2; {LOOP8} w(u);", "void f(void)", "for (;;)"),
        (f"uint8_t v = 0x62, u = 2; w(u); u = 2; {LOOP8}", "void f(void)", "u < 3"),
        (
            f"uint8_t v = 0x62, u = 2; {LOOP8.replace('{ w(v);', '{ w(sizeof u); w(v);')}",
            "void f(void)",
            "u < 3",
        ),
        (f"uint8_t v = 0x62, u = 2; {LOOP8}", "void f(void)", "for (int u = 2; u >= 0;"),
    ],
)
def test_counters_that_keep_their_type(cc, src, sig, want):
    """A parameter, a counter read before its last init or under ``sizeof`` keeps its type;
    one read after the loop keeps the loop; else it widens."""
    assert want in reduced(cc, src, sig)
