"""Counters a ``for`` loop steps in lockstep with its own, dead after it, as affine functions
of one counter stepped by one, the loop then counting that alone under the most readable
condition of it (docs/rewrites.md)."""

from __future__ import annotations

from typing import NamedTuple

from pycparser import c_ast

from . import query as q
from .ints import MODELS, constant, type_of, value
from .listflow import Graph
from .refine import Count, _comma, _init, _loops, _widens, step_of
from .stmt import block, braced, exprs_replaced, negate, swap, writes


class _Counter(NamedTuple):
    """Counter ``v`` of a loop: its ``count``, its init's index (None: the loop's own), and
    its values on each run of the body then one step more (``values[-1]``)."""

    count: Count
    at: int | None
    values: list[int]


def lockstepped(items, types, stops=None):
    """``items`` with every ``for`` loop whose counters fold into one folded
    (:func:`_folded`); ``types`` the variables' type trees, ``stops`` noreturn calls."""
    failed = set()
    while True:
        graph = Graph(items, stops)
        for lst, i, st in _loops(items):
            if id(st) not in failed:
                got = _folded(graph, lst, i, types)
                if got is not None:
                    items = swap(items, lst, got)
                    break
                failed.add(id(st))
        else:
            return items


def _folded(graph, lst, i, types):
    """List ``lst`` with its loop ``i`` counting one counter stepped by one and the others,
    dead after it, that counter's affine functions in their reads; or None."""
    st, counters = lst[i], _counters(lst, i, types)
    if counters is None:
        return None
    after = graph.live_in(graph.after[(id(lst), i)])
    plans = [_plan(counters, u, st.init.lvalue.name, after, types) for u in sorted(counters)]
    plans = [p for p in plans if p is not None and p[1]]
    if not plans:
        return None
    _, fits, u, cond = max(plans, key=lambda p: p[0])
    keep = [counters[w].count.step for w in counters if w not in fits]
    body = exprs_replaced(block(st.stmt), lambda n: fits.get(n.name) if _read(n) else None)
    init = st.init if counters[u].at is None else lst[counters[u].at]
    loop = c_ast.For(init, cond, _comma(keep), braced(body), st.coord)
    gone = {counters[w].at for w in [*fits, u]} - {None}
    return [x for j, x in enumerate(lst[:i]) if j not in gone] + [loop] + lst[i + 1 :]


def _own(st):
    """The variable ``st`` counts when a ``for`` of several steps that sets it alone and
    tests it alone, else None."""
    if not isinstance(st, c_ast.For) or not isinstance(st.next, c_ast.ExprList):
        return None
    init = st.init
    if not isinstance(init, c_ast.Assignment) or init.op != "=" or st.cond is None:
        return None
    v = q.variable(init.lvalue)
    return (
        v.name if v is not None and step_of(init, v.name) and q.reads(st.cond) == {v.name} else None
    )


def _counters(lst, i, types):
    """``{v: _Counter}`` of the loop ``i`` of ``lst`` (:func:`_own`): each step storing one
    variable from itself, not stored in the body, its init a constant before the loop (or
    the loop's own); or None."""
    st, own = lst[i], set(types)
    v = _own(st)
    if v is None:
        return None
    body, out = block(st.stmt), {}
    for s in st.next.exprs:
        w = q.variable(q.stored(s))
        if w is None or w.name in out or not step_of(s, w.name) or q.reads(s) - {w.name}:
            return None
        at = None if w.name == v else _init(lst[:i], w.name, own)
        if at is None and w.name != v:
            return None
        start = st.init if at is None else lst[at]
        out[w.name] = (Count(w.name, start.rvalue, s, negate(st.cond)), at)
    if v not in out or set(out) & (writes(body) | q.addressed(braced(body))):
        return None
    return _runs(out, v, types)


def _runs(found, v, types):
    """``found`` (``{w: (Count, init index)}``) as :class:`_Counter`, the runs those of
    ``v``'s :meth:`Count.sequence`, every counter of a type promoted to ``int`` and its
    values the same in each model; or None."""
    out, runs = {}, None
    for w, (count, at) in found.items():
        ts = [type_of(types.get(w), b) for b in MODELS]
        if not all(map(_widens, ts, MODELS)):
            return None
        if runs is None:
            runs = _length(found[v][0], [type_of(types.get(v), b) for b in MODELS])
        if runs is None:
            return None
        seqs = [_values(count, t, b, runs) for t, b in zip(ts, MODELS)]
        if None in seqs or seqs[0] != seqs[1]:
            return None
        out[w] = _Counter(count, at, seqs[0])
    return out


def _length(count, ts):
    """How many runs the body of ``count``'s loop, its exit tested first, takes in every
    model, or None."""
    got = [count.sequence(t, b) if t is not None else None for t, b in zip(ts, MODELS)]
    if None in got or got[0] != got[1]:
        return None
    return len(got[0][0]) - 1


def _values(count, t, bits, runs):
    """The ``runs + 1`` values of ``count``'s variable (type ``t``) from its init, or None."""
    start = value(count.init, {}, bits)
    if start is None:
        return None
    out = [t.wrap(start[0])]
    for _ in range(runs):
        x = count.stepped(out[-1], t, bits)
        if x is None:
            return None
        out.append(x)
    return out


def _plan(counters, u, v, after, types):
    """``(rank, {w: expression of u}, u, condition)`` of the loop counting ``u``, stepped by
    one, its condition :meth:`Count.head` of ``u`` alone; every other counter dead after the
    loop and affine in ``u`` folded into its reads; or None."""
    me = counters[u]
    seq, end = me.values[:-1], me.values[-1]
    t = type_of(types.get(u), MODELS[0])
    if len(set(seq)) != len(seq) or (
        len(seq) > 1 and (seq[1] - seq[0]) % (1 << t.bits) not in (1, (1 << t.bits) - 1)
    ):
        return None
    fits = {}
    for w, c in counters.items():
        if w != u and w not in after:
            e = _affine(seq, c.values[:-1], u, types.get(w), type_of(types.get(w), MODELS[0]).bits)
            if e is not None:
                fits[w] = e
    if u != v and v not in fits:
        return None
    count = Count(u, me.count.init, me.count.step, c_ast.ID(u))
    cond = count.head(seq, end, [type_of(types.get(u), b) for b in MODELS])
    if cond is None:
        return None
    return (len(fits), cond.op != "!=", u == v), fits, u, cond


def _affine(us, ws, u, decl, bits):
    """The expression of ``u`` (taking ``us``) taking ``ws``, ``a + b * u`` cast to ``decl``
    unless its values need no wrapping, ``b`` from the first two; or None."""
    m = 1 << bits
    b = 0 if len(us) < 2 else (ws[1] - ws[0]) * (us[1] - us[0]) % m
    b = b - m if b > m // 2 else b
    base = (ws[0] - b * us[0]) % m
    for a in (base, base - m):
        if all(a + b * x == y for x, y in zip(us, ws)):
            return _sum(a, b, u)
    if all((base + b * x) % m == y for x, y in zip(us, ws)):
        return c_ast.Cast(c_ast.Typename(None, [], None, decl), _sum(base, b, u))
    return None


def _sum(a, b, u):
    """``a + b * u``, written without zero terms or unit factors."""
    term = c_ast.ID(u) if abs(b) == 1 else c_ast.BinaryOp("*", constant(abs(b)), c_ast.ID(u))
    if b == 0:
        return constant(a)
    if a == 0:
        return term if b > 0 else c_ast.UnaryOp("-", term)
    if b > 0:
        return (
            c_ast.BinaryOp("+", term, constant(a))
            if a > 0
            else c_ast.BinaryOp("-", term, constant(-a))
        )
    return c_ast.BinaryOp("-", constant(a), term)


def _read(n):
    """Whether ``n`` is a variable read (an ``ID``)."""
    return isinstance(n, c_ast.ID)
