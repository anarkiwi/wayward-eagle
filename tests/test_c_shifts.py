"""Arithmetic right shifts of a byte under a test of its sign as ``asr8``."""

import pytest
from pycparser import c_ast

from conftest import HEAD, behaves, run
from wayward_eagle.c import HELPERS, helpers, shifts

U8 = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["uint8_t"]))
U16 = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["uint16_t"]))
NEG = "(v1 >> 3) ^ 0xE0"


def body(text):
    """The reduced C of function ``f`` in ``text`` (from ``f``'s definition on)."""
    return " ".join(text[text.index("\nvoid f(") :].split())


def src(test, neg=NEG, pos="v1 >> 3", decl="uint8_t"):
    """A function setting ``q`` from byte ``v1`` under ``test``, ``neg`` its negative arm's."""
    return (
        f"void f(void) {{ {decl} v1 = p * 0x47 + q * 0x29 + k * 0x11;"
        f" if ({test}) {{ w(1); q = {neg}; }} else {{ w(2); q = {pos}; }} }}"
    )


@pytest.mark.parametrize(
    "test",
    ["(int8_t)v1 < 0", "(int8_t)v1 <= -1", "v1 & 0x80", "v1 >= 0x80", "v1 > 0x7F"],
)
def test_sign_tests_give_asr8(tmp_path, test):
    """Each test true exactly when the byte is negative, its arms then both ``asr8``."""
    got = body(behaves(tmp_path, src(test)))
    assert got.count("asr8(v1, 3)") == 2


@pytest.mark.parametrize("n", range(1, 8))
@pytest.mark.parametrize("op", ["^", "|"])
def test_each_shift_behaves(tmp_path, n, op):
    """Every shift count, its top bits set by ``^`` or ``|``, under a test of a clear top
    bit (the negative arm the ``else``)."""
    m = (0xFF << (8 - n)) & 0xFF
    text = src("(int8_t)v1 > -1", f"v1 >> {n}", f"(v1 >> {n}) {op} 0x{m:X}")
    assert body(behaves(tmp_path, text)).count(f"asr8(v1, {n})") == 2


@pytest.mark.parametrize(
    "text",
    [
        src("(int8_t)v1 < 0", "(v1 >> 3) ^ 0xF0"),
        src("(int8_t)v1 < 1"),
        src("(int8_t)v1 < p"),
        src("(int8_t)v1 < 0", "(v1 >> 3) ^ 0xE0, v1 = 1"),
        src("(int8_t)v1 < 0", "*&v1 >> 3 ^ 0xE0"),
        src("(int8_t)v1 < 0", "(v1 >> 0) ^ 0"),
        src("(int8_t)v1 < 0", "(v1 >> p) ^ 0xE0"),
        src("(int8_t)v1 < 0", "v1 >> 3", "(v1 >> 3) ^ 0xE0"),
    ],
)
def test_shifts_that_stay(tmp_path, text):
    """No ``asr8`` when the mask is not the shift's top bits, the test is no sign test, the
    byte is stored or addressed, the count is not 1 to 7, or the masked shift is in the
    arm where the byte is not negative."""
    assert "asr8" not in body(behaves(tmp_path, text))


def test_wider_variables_stay(cc):
    """A ``uint16_t`` under a test of its low byte's sign stays."""
    items = cc.items("if ((int8_t)v1 < 0) { q = (v1 >> 3) ^ 0xE0; }")
    assert cc.same(
        shifts.shifted(items, {"v1": U16}), "if ((int8_t)v1 < 0) { q = (v1 >> 3) ^ 0xE0; }"
    )


def test_nested_arms_rewrite(cc):
    """A sign test nested in a loop and in another ``if`` arm rewrites."""
    items = cc.items("for (;;) { if (p) { if ((int8_t)v1 < 0) { q = (v1 >> 1) | 0x80; } } }")
    want = "{ for (;;) { if (p) { if ((int8_t)v1 < 0) { q = asr8(v1, 1); } else { } } else { } } }"
    assert cc.text(shifts.shifted(items, {"v1": U8})) == want


def test_helper_is_an_arithmetic_shift(tmp_path):
    """``asr8(x, n)`` is ``x`` shifted right ``n`` with its sign bit copied in, every byte
    and count from 0 to 7."""
    prog = (
        HEAD
        + HELPERS["asr8"]
        + "#include <stdio.h>\nint main(void) { for (int x = 0; x < 256; x++)"
        " for (int n = 0; n < 8; n++) { int s = x >= 0x80 ? x - 0x100 : x;"
        ' if (asr8(x, n) != (uint8_t)(s >> n)) printf("%d %d\\n", x, n); } return 0; }\n'
    )
    assert run(tmp_path / "asr8.c", prog) == ""


def test_helpers_named_in_calls(cc):
    """:func:`helpers` names the helpers a function's statements call."""
    items = cc.items("if ((int8_t)v1 < 0) { q = (v1 >> 1) | 0x80; } w(p);")
    assert helpers(shifts.shifted(items, {"v1": U8})) == ["asr8"]
    assert not helpers(items)
