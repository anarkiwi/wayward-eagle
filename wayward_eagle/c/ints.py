"""C's integer arithmetic over ``c_ast`` expressions of constants and variables of known
types, in a data model fixed by the width of ``int``: literal types, the integer promotions,
the usual arithmetic conversions, wrapping, and undefined results (signed overflow, division
by zero, bad shifts) as None."""

from __future__ import annotations

from typing import NamedTuple

from pycparser import c_ast

#: The widths of ``int`` of the data models a result must hold in: a 16-bit target's, a host's.
MODELS = (16, 32)


class Int(NamedTuple):
    """An integer type: its width in bits and whether it is signed (``_Bool`` is 1 bit)."""

    bits: int
    signed: bool

    def wrap(self, x):
        """``x`` converted to this type, modulo its width (``_Bool``: whether it is nonzero)."""
        if self.bits == 1:
            return int(x != 0)
        x &= (1 << self.bits) - 1
        return x - (1 << self.bits) if self.signed and x >> (self.bits - 1) else x

    def holds(self, x):
        """Whether ``x`` is a value of this type."""
        return self.wrap(x) == x


#: Fixed-width typedef names.
FIXED = {f"{s}int{b}_t": Int(b, s == "") for b in (8, 16, 32, 64) for s in ("", "u")}


def named(names, bits):
    """The :class:`Int` that type specifier ``names`` (``["unsigned", "char"]``,
    ``["uint8_t"]``) denote with an ``int`` of ``bits`` bits, None for no integer type."""
    if len(names) == 1 and names[0] in FIXED:
        return FIXED[names[0]]
    if names == ["_Bool"]:
        return Int(1, False)
    words = [w for w in names if w not in ("signed", "unsigned", "int")]
    size = {(): bits, ("char",): 8, ("short",): 16, ("long",): 32 if bits == 16 else 64}
    size[("long", "long")] = 64
    got = size.get(tuple(words))
    return None if got is None else Int(got, "unsigned" not in names)


def type_of(tree, bits):
    """The :class:`Int` of ``c_ast`` type tree ``tree`` (a ``Typename`` or ``TypeDecl``)."""
    if isinstance(tree, c_ast.Typename):
        tree = tree.type
    if not isinstance(tree, c_ast.TypeDecl) or not isinstance(tree.type, c_ast.IdentifierType):
        return None
    return named(tree.type.names, bits)


def promoted(t, bits):
    """``t`` after the integer promotions."""
    if t.bits < bits or (t.bits == bits and t.signed):
        return Int(bits, True)
    return t


def common(a, b, bits):
    """The usual arithmetic conversions' type of operands of types ``a`` and ``b``."""
    a, b = promoted(a, bits), promoted(b, bits)
    if a == b:
        return a
    if a.signed == b.signed:
        return max(a, b)
    s, u = (a, b) if a.signed else (b, a)
    if u.bits >= s.bits:
        return u
    return s if s.bits > u.bits else Int(s.bits, False)


def literal(c, bits):
    """``(value, Int)`` of integer constant ``c`` (a ``c_ast.Constant``), None for another."""
    text = c.value.lower()
    if c.type == "char":
        body = text[1:-1]
        return (ord(body), Int(bits, True)) if len(body) == 1 and body != "\\" else None
    digits = text.rstrip("ul")
    suffix = text[len(digits) :]
    try:
        number = int(digits, 16 if digits.startswith("0x") else 8 if digits[:1] == "0" else 10)
    except ValueError:
        return None
    decimal = not digits.startswith("0") or digits == "0"
    signs = (False,) if "u" in suffix else (True,) if decimal else (True, False)
    for w in (bits, 32 if bits == 16 else 64, 64)[suffix.count("l") :]:
        for signed in signs:
            if Int(w, signed).holds(number):
                return number, Int(w, signed)
    return None


ARITH = {
    "+": lambda x, y: x + y,
    "-": lambda x, y: x - y,
    "*": lambda x, y: x * y,
    "&": lambda x, y: x & y,
    "|": lambda x, y: x | y,
    "^": lambda x, y: x ^ y,
}
COMPARE = {
    "<": lambda x, y: x < y,
    ">": lambda x, y: x > y,
    "<=": lambda x, y: x <= y,
    ">=": lambda x, y: x >= y,
    "==": lambda x, y: x == y,
    "!=": lambda x, y: x != y,
}


def value(e, env, bits):  # pylint: disable=too-many-return-statements,too-many-branches
    """``(value, Int)`` of expression ``e`` with variables ``env`` (``{name: (value, Int)}``)
    and an ``int`` of ``bits`` bits, None when it reads anything else or is undefined."""
    if isinstance(e, c_ast.Constant):
        return literal(e, bits)
    if isinstance(e, c_ast.ID):
        return env.get(e.name)
    if isinstance(e, c_ast.Cast):
        t, x = type_of(e.to_type, bits), value(e.expr, env, bits)
        return None if t is None or x is None else (t.wrap(x[0]), t)
    if isinstance(e, c_ast.UnaryOp):
        x = value(e.expr, env, bits)
        if x is None or e.op not in ("-", "+", "~", "!"):
            return None
        if e.op == "!":
            return int(x[0] == 0), Int(bits, True)
        t = promoted(x[1], bits)
        got = {"-": -x[0], "+": x[0], "~": ~x[0]}[e.op]
        return _result(got, t)
    if isinstance(e, c_ast.TernaryOp):
        c = value(e.cond, env, bits)
        a, b = value(e.iftrue, env, bits), value(e.iffalse, env, bits)
        if c is None or a is None or b is None:
            return None
        t = common(a[1], b[1], bits)
        return t.wrap((a if c[0] else b)[0]), t
    if isinstance(e, c_ast.BinaryOp):
        return _binary(e, env, bits)
    return None


def _result(x, t):
    """``x`` in type ``t``: wrapped when unsigned, None when a signed result overflows."""
    if t.signed and not t.holds(x):
        return None
    return t.wrap(x), t


def _binary(e, env, bits):  # pylint: disable=too-many-return-statements
    a = value(e.left, env, bits)
    if a is None:
        return None
    if e.op in ("&&", "||"):
        if (a[0] != 0) == (e.op == "||"):
            return int(e.op == "||"), Int(bits, True)
        b = value(e.right, env, bits)
        return None if b is None else (int(b[0] != 0), Int(bits, True))
    b = value(e.right, env, bits)
    if b is None:
        return None
    if e.op in ("<<", ">>"):
        t = promoted(a[1], bits)
        if not 0 <= b[0] < t.bits or (e.op == "<<" and a[0] < 0):
            return None
        return _result(a[0] << b[0] if e.op == "<<" else a[0] >> b[0], t)
    t = common(a[1], b[1], bits)
    x, y = t.wrap(a[0]), t.wrap(b[0])
    if e.op in COMPARE:
        return int(COMPARE[e.op](x, y)), Int(bits, True)
    if e.op in ARITH:
        return _result(ARITH[e.op](x, y), t)
    if e.op in ("/", "%") and y != 0:
        q = abs(x) // abs(y) * (1 if (x < 0) == (y < 0) else -1)
        return _result(q if e.op == "/" else x - q * y, t)
    return None


def constant(x):
    """A ``c_ast`` literal of integer ``x`` (``int``), hexadecimal from 10 up, negated below
    zero."""
    if x < 0:
        return c_ast.UnaryOp("-", constant(-x))
    return c_ast.Constant("int", str(x) if x < 10 else f"0x{x:X}")
