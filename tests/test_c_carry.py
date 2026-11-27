"""Add-with-carry chains as exact sums: the rewrites' shapes, their refusals, and the
reduced functions printing what the originals did over a sweep of byte values."""

import pytest

from conftest import run
from wayward_eagle.c import front
from wayward_eagle.c import reduce as r

HEAD = (
    "typedef unsigned char uint8_t; typedef unsigned short uint16_t;\n"
    "uint8_t R[64]; uint8_t out[8]; int n;\n"
    "void put(uint8_t v);\n"
)
SWEEP = """
#include <stdio.h>
void put(uint8_t v) { printf("%u ", v); }
int main(void) {
  static const int vals[] = {0, 1, 2, 0x7F, 0x80, 0x81, 0xFE, 0xFF, 0x33, 0xC0};
  for (int i = 0; i < 64; i++) R[i] = (uint8_t)(i * 37 + 11);
  for (int a = 0; a < 10; a++)
    for (int b = 0; b < 10; b++)
      for (int c = 0; c < 2; c++) {
        f(vals[a], vals[b], c);
        printf("|\\n");
      }
  return 0;
}
"""


def reduced(tmp_path, body, sig="void f(uint8_t a, uint8_t b, uint8_t c)"):
    """The text of ``f`` (``sig`` around ``body``, after :data:`HEAD`) reduced, asserting it
    prints what the original does over :data:`SWEEP`'s values."""
    src = f"{HEAD}{sig} {{ {body} }}\n"
    path = tmp_path / "orig.c"
    path.write_text(src, encoding="utf-8")
    got = r.reduce_unit(front.load(path), functions={"f"}, carries=True)
    assert run(tmp_path / "new.c", got + SWEEP) == run(tmp_path / "old.c", src + SWEEP)
    return " ".join(got.split(sig)[-1].split())


@pytest.mark.parametrize(
    "body,want",
    [
        (
            "uint8_t t = a + b; uint8_t x = t + (c != 0); uint8_t k = t < a || x < t;"
            " put(x); put(k);",
            "{ uint16_t x = a + b + (c != 0); put(x); put(x > 0xFF); }",
        ),
        (
            "uint8_t lo = a + b; uint8_t k = lo < a;"
            " put((uint16_t)((((uint16_t)c << 8) | lo) + (((uint16_t)R[1] << 8) | k)) >> 8);",
            None,
        ),
        (
            "uint8_t t = a + (R + 1)[c & 7]; uint8_t x = t + (b > 7);"
            " if ((signed char)x < 0) { put(1); } put(t < a || x < t);",
            "{ uint16_t x = a + (R + 1)[c & 7] + (b > 7); if ((signed char)x < 0) { put(1); }"
            " put(x > 0xFF); }",
        ),
        (
            "put((uint16_t)((((uint16_t)R[0] << 8) | a) + (((uint16_t)R[1] << 8) | b)) >> 8);",
            "{ (void)c; put(R[0] + R[1] + (a + b > 0xFF)); }",
        ),
        (
            "out[0] = (uint16_t)((((uint16_t)R[0] << 8) | a) + (((uint16_t)R[1] << 8) | b)) >> 8;",
            "{ (void)c; out[0] = R[0] + R[1] + (a + b > 0xFF); }",
        ),
        (
            "if ((uint16_t)((((uint16_t)R[0] << 8) | a) + (((uint16_t)R[1] << 8) | b)) >> 8)"
            " { put(1); }",
            "{ (void)c; if ((uint8_t)(R[0] + R[1] + (a + b > 0xFF))) { put(1); } }",
        ),
    ],
)
def test_carry_chains_become_sums(tmp_path, body, want):
    """A byte sum whose carry is recomputed is held whole, chains merge, and a carry merged
    into a 16-bit shift reads as the byte sum, printing what it did."""
    got = reduced(tmp_path, body)
    if want is not None:
        assert got == want
    else:
        assert "<< 8" not in got and "> 0xFF" in got


@pytest.mark.parametrize(
    "body",
    [
        "uint8_t x = a + b; a = 0; put(x < a);",  # the operand changed before the test
        "uint8_t x = a + b; if (c) { x = 3; } put(x < a);",  # another store to it
        "uint8_t x = a + n; put(x < a);",  # an operand not a byte
        "uint8_t x = a - b; put(x < a);",  # a difference
        "uint8_t x = a + b; put(x < c);",  # a comparison with another value
        "uint8_t x = a + b; put(x);",  # no carry
        "uint8_t x = a + b; put(x); put(x < a);",  # a lone carry: no simpler as a sum
        "uint8_t x = a + b; put(R[x & 7] + (x < b));",  # nor where it adds a cast
    ],
)
def test_what_is_no_carry_stays(tmp_path, body):
    """A sum whose comparison is not its carry, or with no carry read, stays a byte."""
    assert "uint16_t" not in reduced(tmp_path, body)


def test_a_parameter_is_not_widened(tmp_path):
    """A parameter's type is the function's: its sum stays a byte."""
    got = reduced(tmp_path, "a = a + b; put(a); put(a < b);")
    assert "uint16_t" not in got


def test_shift_in_arithmetic_stays(tmp_path):
    """A merged shift whose type matters to the arithmetic around it stays."""
    body = "put(1 + ((uint16_t)((((uint16_t)R[0] << 8) | a) + (((uint16_t)R[1] << 8) | b)) >> 8));"
    assert "<< 8" in reduced(tmp_path, body)


def test_carries_can_be_off(tmp_path):
    """Without ``carries`` the chains stay bytes; they are sums by default."""
    path = tmp_path / "f.c"
    path.write_text(
        HEAD + "void f(uint8_t a, uint8_t b, uint8_t c) { uint8_t t = a + b;"
        " uint8_t x = t + (c != 0); put(x); put(t < a || x < t); }\n",
        encoding="utf-8",
    )
    assert "uint16_t x" not in r.reduce_unit(front.load(path), functions={"f"}, carries=False)
    assert "uint16_t x" in r.reduce_unit(front.load(path), functions={"f"})
