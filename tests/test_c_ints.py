"""C's integer arithmetic with a 16-bit and a 32-bit ``int``."""

import pytest
from pycparser import c_ast, c_parser

from wayward_eagle.c import ints

U8, S8, U16 = ints.Int(8, False), ints.Int(8, True), ints.Int(16, False)
PARSER = c_parser.CParser()


def expr(src):
    """The ``c_ast`` expression of C ``src``, with the fixed-width typedefs declared."""
    unit = PARSER.parse(
        "typedef unsigned char uint8_t; typedef signed char int8_t; typedef unsigned short"
        f" uint16_t; long f(void) {{ return {src}; }}"
    )
    return unit.ext[-1].body.block_items[0].expr


@pytest.mark.parametrize(
    "src, env, narrow, wide",
    [
        ("(int8_t)v > 0", {"v": (255, U8)}, 0, 0),
        ("v - 1", {"v": (0, U8)}, -1, -1),
        ("(uint8_t)(v - 1)", {"v": (0, U8)}, 255, 255),
        ("v + 1", {"v": (65535, U16)}, 0, 65536),
        ("0xFFFF", {}, 65535, 65535),
        ("-1 < 0u", {}, 0, 0),
        ("-1 < 0", {}, 1, 1),
        ("32767 + 1", {}, None, 32768),
        ("v << 8", {"v": (255, U8)}, None, 65280),
        ("1 << 16", {}, None, 65536),
        ("v >> 1", {"v": (-4, S8)}, -2, -2),
        ("-7 / 2", {}, -3, -3),
        ("-7 % 2", {}, -1, -1),
        ("7 / 0", {}, None, None),
        ("p ? 1u : -1", {"p": (0, U8)}, 65535, 4294967295),
        ("v && w", {"v": (0, U8)}, 0, 0),
        ("v || w", {"v": (1, U8)}, 1, 1),
        ("v && 2", {"v": (1, U8)}, 1, 1),
        ("!v", {"v": (0, U8)}, 1, 1),
        ("~v", {"v": (0, U8)}, -1, -1),
        ("-v", {"v": (1, U8)}, -1, -1),
        ("(_Bool)v", {"v": (4, U8)}, 1, 1),
        ("(unsigned char)300", {}, 44, 44),
        ("'a'", {}, 97, 97),
        ("40000", {}, 40000, 40000),
        ("10UL", {}, 10, 10),
        ("017", {}, 15, 15),
        ("f()", {}, None, None),
        ("(float)1", {}, None, None),
        ("v++", {"v": (1, U8)}, None, None),
        ("w", {}, None, None),
    ],
)
def test_values_follow_c_in_each_data_model(src, env, narrow, wide):
    """Each value is C's with a 16-bit and a 32-bit ``int``, None when undefined or
    reading anything but the given variables."""
    got = [ints.value(expr(src), env, bits) for bits in ints.MODELS]
    assert [g if g is None else g[0] for g in got] == [narrow, wide]


def test_literal_and_operand_types():
    """A literal takes the first type holding it (an unsuffixed hexadecimal one unsigned
    too); promotions and the usual arithmetic conversions follow the width of ``int``."""
    assert ints.literal(c_ast.Constant("int", "0xFFFF"), 16) == (65535, ints.Int(16, False))
    assert ints.literal(c_ast.Constant("int", "40000"), 16) == (40000, ints.Int(32, True))
    assert ints.literal(c_ast.Constant("int", "1u"), 32) == (1, ints.Int(32, False))
    assert ints.literal(c_ast.Constant("int", "08"), 16) is None
    assert ints.literal(c_ast.Constant("char", "'\\n'"), 16) is None
    assert ints.promoted(U16, 16) == U16 and ints.promoted(U16, 32) == ints.Int(32, True)
    assert ints.common(ints.Int(32, True), U16, 16) == ints.Int(32, True)
    assert ints.common(ints.Int(16, True), ints.Int(32, False), 32) == ints.Int(32, False)
    assert ints.common(ints.Int(64, True), ints.Int(32, False), 16) == ints.Int(64, True)


def test_type_names():
    """Type specifiers name their integer type in the model, None for others."""
    assert ints.named(["unsigned", "char"], 16) == U8
    assert ints.named(["short", "int"], 32) == ints.Int(16, True)
    assert ints.named(["long"], 16) == ints.Int(32, True)
    assert ints.named(["long", "long"], 16) == ints.Int(64, True)
    assert ints.named(["unsigned"], 16) == U16
    assert ints.named(["uint8_t"], 32) == U8
    assert ints.named(["double"], 16) is None
    ptr = c_ast.PtrDecl([], c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["int"])))
    assert ints.type_of(ptr, 16) is None


def test_constants_are_spelled_as_written_here():
    """Literals below 10 decimal, from 10 hexadecimal, negative ones negated."""
    assert ints.constant(9).value == "9" and ints.constant(255).value == "0xFF"
    neg = ints.constant(-1)
    assert isinstance(neg, c_ast.UnaryOp) and neg.op == "-" and neg.expr.value == "1"
