"""Arithmetic right shifts of a byte under a test of its sign, as ``asr8(x, n)``: the
negative arm's ``(x >> n) ^ M`` (or ``| M``), ``M`` the top ``n`` bits, and the other arm's
``x >> n`` (docs/rewrites.md)."""

from __future__ import annotations

from pycparser import c_ast

from . import query as q
from .ints import MODELS, Int, type_of, value
from .stmt import block, braced, children, ends_in_jump, exprs_replaced, rebuild, writes

#: The helper's name and its C definition, the 6502's ``EOR #$FF; LSR; EOR #$FF``.
HELPERS = {
    "asr8": "static inline uint8_t asr8(uint8_t x, uint8_t n) {\n"
    "  return x & 0x80 ? (uint8_t)~((uint8_t)~x >> n) : x >> n;\n}\n",
}

BYTE = Int(8, False)


def shifted(items, types):
    """``items`` with each byte variable's arithmetic right shifts under a test of its sign
    as ``asr8`` calls; ``types`` the variables' type trees."""
    taken = q.addressed(braced(items))

    def go(items):
        out = list(items)
        for i, st in enumerate(out):
            if isinstance(st, c_ast.If):
                rest = (
                    None
                    if block(st.iffalse) or not ends_in_jump(block(st.iftrue))
                    else out[i + 1 :]
                )
                out[i], rest = _arms(st, rest, types, taken)
                out[i + 1 :] = out[i + 1 :] if rest is None else rest
        return [visit(st) for st in out]

    def visit(st):
        if isinstance(st, c_ast.If):
            els = None if st.iffalse is None else braced(go(block(st.iffalse)))
            return c_ast.If(st.cond, braced(go(block(st.iftrue))), els, st.coord)
        return rebuild(st, [go(sub) for sub in children(st)]) if children(st) else st

    return go(items)


def _arms(st, rest, types, taken):
    """``(st, rest)`` with the shifts of the byte ``if`` statement ``st`` tests the sign of,
    stored in neither arm (the ``else``, or ``rest`` after it when not None) and never
    addressed, as ``asr8`` calls."""
    got = _sign(st.cond, types)
    if got is None or got[0] in taken:
        return st, rest
    v, negative = got
    arms = [block(st.iftrue), block(st.iffalse) if rest is None else rest]
    neg, pos = (arms[0], arms[1]) if negative else (arms[1], arms[0])
    if v in writes(neg) | writes(pos):
        return st, rest
    found = set()
    neg = exprs_replaced(neg, lambda n: _filled(n, v, found))
    if not found:
        return st, rest
    pos = exprs_replaced(pos, lambda n: _plain(n, v, found))
    then, els = (neg, pos) if negative else (pos, neg)
    if rest is not None:
        return c_ast.If(st.cond, braced(then), st.iffalse, st.coord), els
    return c_ast.If(st.cond, braced(then), braced(els), st.coord), None


def _sign(cond, types):
    """``(v, whether cond holds when v is negative)`` when ``cond`` reads byte variable ``v``
    alone and holds exactly when its top bit is set, or exactly when it is clear."""
    read = sorted(q.reads(cond))
    if len(read) != 1 or q.calls(cond) or q.writes(cond):
        return None
    v = read[0]
    if any(type_of(types.get(v), b) != BYTE for b in MODELS):
        return None
    runs = [[value(cond, {v: (x, BYTE)}, b) for x in range(256)] for b in MODELS]
    if any(None in r for r in runs):
        return None
    got = {tuple(bool(x[0]) for x in r) for r in runs}
    top = tuple(x >= 0x80 for x in range(256))
    if got == {top}:
        return v, True
    if got == {tuple(not t for t in top)}:
        return v, False
    return None


def _shift(n, v):
    """``k`` when ``n`` is ``v >> k`` for ``k`` from 1 to 7, else None."""
    if not (isinstance(n, c_ast.BinaryOp) and n.op == ">>" and q.variable(n.left)):
        return None
    k = value(n.right, {}, MODELS[0])
    if n.left.name != v or k is None or not 1 <= k[0] <= 7:
        return None
    return k[0]


def _filled(n, v, found):
    """``asr8(v, k)`` for ``(v >> k) ^ M`` or ``| M`` (``M`` the top ``k`` bits of a byte),
    ``k`` added to ``found``; else None."""
    if not (isinstance(n, c_ast.BinaryOp) and n.op in ("^", "|")):
        return None
    for a, b in ((n.left, n.right), (n.right, n.left)):
        k, m = _shift(a, v), value(b, {}, MODELS[0])
        if k is not None and m is not None and m[0] == (0xFF << (8 - k)) & 0xFF:
            found.add(k)
            return _call(v, k)
    return None


def _plain(n, v, found):
    """``asr8(v, k)`` for ``v >> k`` with ``k`` in ``found``, else None."""
    k = _shift(n, v)
    return _call(v, k) if k in found else None


def _call(v, k):
    """``asr8(v, k)``."""
    args = c_ast.ExprList([c_ast.ID(v), c_ast.Constant("int", str(k))])
    return c_ast.FuncCall(c_ast.ID("asr8"), args)


def helpers(nodes):
    """The names of :data:`HELPERS` that ``nodes`` (``c_ast`` nodes) call, in order."""
    used = {q.callee(c) for n in nodes for c in q.calls(n)}
    return [name for name in HELPERS if name in used]
