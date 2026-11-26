"""Add-with-carry chains as sums in a wider type: a byte sum whose carry is recomputed held
exactly in ``uint16_t`` (the byte ``(uint8_t)x``, the carry ``x > 0xFF``), a carry into a
second add merged into one sum, a carry LLVM merges into a 16-bit shift read as a byte sum."""

from __future__ import annotations

import copy

from pycparser import c_ast

from . import query as q
from .fold import Folder, Site, Types, alike, ancestors, converted, facts, occurrences
from .fold import settled, valued
from .ints import MODELS, type_of
from .listflow import Graph, reached
from .stmt import assigned, braced, children, heads, rehead, remap, spliced, walk

OVER = "0xFF"


def _named(name):
    return c_ast.TypeDecl(None, [], None, c_ast.IdentifierType([name]))


def _byte_cast(e):
    return c_ast.Cast(c_ast.Typename(None, [], None, _named("uint8_t")), e)


def _over(e):
    """``e > 0xFF``: the carry out of the sum ``e`` (a name or an expression)."""
    e = c_ast.ID(e) if isinstance(e, str) else e
    return c_ast.BinaryOp(">", e, c_ast.Constant("int", OVER))


def _is_over(e, name):
    return (
        isinstance(e, c_ast.BinaryOp)
        and e.op == ">"
        and isinstance(e.left, c_ast.ID)
        and e.left.name == name
        and isinstance(e.right, c_ast.Constant)
        and e.right.value == OVER
    )


def _shift(e, op="<<"):
    """``X`` of ``(uint16_t)X << 8`` (or ``>>`` for ``op``), else None."""
    if (
        isinstance(e, c_ast.BinaryOp)
        and e.op == op
        and _literal(e.right) == 8
        and isinstance(e.left, c_ast.Cast)
        and q.spelled(e.left.to_type.type, "uint16_t")
    ):
        return e.left.expr
    return None


def _byte_type(tree):
    """Whether type tree ``tree`` (or None) is a byte's, in both data models."""
    return tree is not None and alike(tree, _named("uint8_t"))


def _literal(e):
    if isinstance(e, c_ast.Constant) and "int" in e.type:
        try:
            return int(e.value.rstrip("uUlL"), 0)
        except ValueError:
            return None
    return None


def _positions(items):
    for i, st in enumerate(items):
        yield (id(items), i), st
        for sub in children(st):
            yield from _positions(sub)


class Carrier:  # pylint: disable=too-few-public-methods
    """The carry rewrites of a statement list over ``types`` (``{name: type tree}``, updated
    for the variables it widens), widening only ``eligible``; ``ret``, ``outer`` and ``stops``
    as :class:`~wayward_eagle.c.fold.Folder` takes them."""

    def __init__(
        self, types, eligible, ret=None, outer=None, stops=None
    ):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self.types, self.eligible, self.stops, self.ret = types, set(eligible), stops, ret
        self.typed, self.folder = Types(types, outer), Folder(types, ret, outer, stops)
        self.widened = set()

    def __call__(self, items):
        """``items`` with each rewrite that applies made, until none does."""
        return settled(self._once, items)

    def _once(self, items):
        before = q.cognitive(braced(items))
        graph = Graph(items, self.stops)
        for rule in (self._widen, self._merge, self._shifted):
            for got in rule(graph, items):
                if q.cognitive(braced(got)) <= before:
                    return got
        return None

    def _byte(self, e, lo=0, hi=255):
        """Whether ``e`` takes only values from ``lo`` to ``hi``, in both data models."""
        for bits in MODELS:
            b = self.typed.bound(e, bits)
            if b is None or b.lo < lo or b.hi > hi:
                return False
        return True

    def _definition(self, graph, items, v):
        """``(node, position, statement)`` of the one store to ``v``, a statement ``v = e``,
        when no read of ``v`` comes before it, else None."""
        bit = graph.mask((v,))
        stores = [n for n, m in enumerate(graph.store) if m & bit]
        if len(stores) != 1 or graph.live()[graph.entry] & bit:
            return None
        for spot, st in _positions(items):
            if graph.heads.get(spot) == stores and assigned(st) == v:
                return stores[0], spot, st
        return None

    @staticmethod
    def _reads(graph, v):
        """``[(node, expression, read)]`` of each read of ``v``."""
        bit = graph.mask((v,))
        return [
            (n, graph.expr[n], r)
            for n in range(len(graph.expr))
            if graph.use[n] & bit
            for r in occurrences(graph.expr[n], v)
        ]

    def _kept(self, graph, d, n, e):
        """Whether ``e`` (a local or a constant) holds at node ``n`` what it held at node
        ``d``: no store to it on a path between them, nor in ``n``."""
        if isinstance(e, c_ast.Cast):
            e = e.expr
        if isinstance(e, c_ast.Constant):
            return True
        if not isinstance(e, c_ast.ID) or e.name not in self.types:
            return False
        bit, preds = graph.mask((e.name,)), graph.predecessors()
        between = reached(graph.succ, graph.succ[d], {d, n}) & reached(preds, preds[n], {d, n})
        if any(graph.store[m] & bit for m in between - {d}):
            return False
        return e.name not in q.writes(graph.expr[n])

    def _carry_of(
        self, graph, at, expr, read, operands
    ):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        """The comparison ``read < a`` (or ``a > read``) holding ``read`` in ``expr`` at
        node ``n`` (``at`` is ``(d, n)``), ``a`` an operand of the sum stored at node ``d``
        and unchanged since."""
        d, n = at
        path = ancestors(expr, read)
        parent = path[-1] if path else None
        if not isinstance(parent, c_ast.BinaryOp):
            return None
        sides = {"<": (parent.left, parent.right), ">": (parent.right, parent.left)}
        got = sides.get(parent.op)
        if got is None or got[0] is not read:
            return None
        if any(q.equal(got[1], a) and self._kept(graph, d, n, a) for a in operands):
            return parent
        return None

    def _context(self, expr, read):
        """``(parent, target)``: ``read``'s parent in ``expr`` and the type tree it converts
        ``read`` to, or None."""
        path = ancestors(expr, read)
        parent = path[-1] if path else None
        return parent, converted(self.typed, self.ret, expr, read, parent)

    def _converted_byte(self, expr, read):
        """Whether ``read``'s context converts it to a byte, or casts it to 8 bits or fewer."""
        parent, target = self._context(expr, read)
        if isinstance(parent, c_ast.Cast):
            got = [type_of(parent.to_type, bits) for bits in MODELS]
            return all(t is not None and t.bits <= 8 for t in got)
        return _byte_type(target)

    def _widen(self, graph, items):
        """Each byte sum ``x = a + b`` a carry of which is recomputed, widened."""
        for v in sorted(self.eligible - self.widened):
            got = (
                self._definition(graph, items, v)
                if q.spelled(self.types.get(v), "uint8_t")
                else None
            )
            e = got[2].rvalue if got else None
            if not (isinstance(e, c_ast.BinaryOp) and e.op == "+"):
                continue
            if not (self._byte(e.left) and self._byte(e.right)):
                continue
            new, carries = {}, 0
            for n, expr, read in self._reads(graph, v):
                carry = self._carry_of(graph, (got[0], n), expr, read, (e.left, e.right))
                if carry is not None:
                    new[id(carry)], carries = _over(v), carries + 1
                elif not self._converted_byte(expr, read):
                    new[id(read)] = _byte_cast(c_ast.ID(v))
            if carries:
                self.widened.add(v)
                self.types[v] = _named("uint16_t")
                yield _rewritten(items, graph, new)

    def _merge(self, graph, items):
        """Each carry ``t > 0xFF || x > 0xFF`` of a sum ``x = (uint8_t)t + c`` (``c`` 0 or 1)
        of a widened sum ``t`` read nowhere else: ``x`` the whole sum, its carry ``x > 0xFF``,
        ``t``'s sum folded into it where that keeps every value and effect."""
        for x in sorted(self.widened):
            got = self._definition(graph, items, x)
            pair = self._summed(got[2].rvalue) if got else None
            joined = self._joined(graph, x, *pair[:2]) if pair else None
            if joined is None:
                continue
            new = {id(o): _over(x) for o in joined}
            new[id(got[2].rvalue)] = c_ast.BinaryOp("+", c_ast.ID(pair[0]), pair[2])
            items = _rewritten(items, graph, new)
            yield self._folded(items, pair[0], x) or items

    def _summed(self, e):
        """``(t, cast, c)`` of ``(uint8_t)t + c`` (either way round), ``t`` widened and ``c``
        0 or 1, else None."""
        if not (isinstance(e, c_ast.BinaryOp) and e.op == "+"):
            return None
        for cast, c in ((e.left, e.right), (e.right, e.left)):
            if (
                isinstance(cast, c_ast.Cast)
                and isinstance(cast.expr, c_ast.ID)
                and cast.expr.name in self.widened
                and q.spelled(cast.to_type.type, "uint8_t")
                and self._byte(c, 0, 1)
            ):
                return cast.expr.name, cast, c
        return None

    def _joined(self, graph, x, t, cast):
        """The ``t > 0xFF || x > 0xFF`` of ``x``'s carries, when every carry of ``x`` is one
        and ``t`` is read only in them and in ``cast``, else None."""
        ors, overs = [], 0
        for _, expr, read in self._reads(graph, x):
            path = ancestors(expr, read)
            if path and _is_over(path[-1], x):
                overs += 1
                if len(path) > 1 and _or_of(path[-2], t, x):
                    ors.append(path[-2])
        rest = [r for _, _, r in self._reads(graph, t) if r is not cast.expr]
        inside = {id(r) for o in ors for r in occurrences(o, t)}
        if not ors or len(ors) != overs or len(rest) != len(ors):
            return None
        return ors if all(id(r) in inside for r in rest) else None

    def _folded(self, items, t, x):
        """``items`` with ``t``'s sum folded into ``x = t + c`` where that keeps every value
        and effect, else None: the sum is exact, so its type does not matter."""
        graph = Graph(items, self.stops)
        dt, dx = self._definition(graph, items, t), self._definition(graph, items, x)
        if dt is None or dx is None or not self._byte(dt[2].rvalue, 0, 0x1FE):
            return None
        value, use = dt[2].rvalue, dx[2]
        read = occurrences(use.rvalue, t)[0]
        site = Site(t, value, dt[1], dt[0], use, dx[1], dx[0], use, read)
        mine = facts(value, set(self.types), arrays=self.folder.arrays)
        if mine.stores or not self.folder.legal(graph, site, mine, same=False):
            return None
        rv = q.replaced(use.rvalue, lambda n: copy.deepcopy(value) if n is read else None)
        new = {dt[1]: [], dx[1]: [c_ast.Assignment("=", use.lvalue, rv, use.coord)]}
        self.widened.discard(t)
        return spliced(items, lambda lst, i, st: new.get((id(lst), i), [st]))

    def _shifted(self, graph, items):
        """Each ``(uint16_t)(((uint16_t)H << 8 | L) + ((uint16_t)T << 8 | C)) >> 8`` of
        bytes, in a context converting it or needing only its value, as
        ``(uint8_t)(H + T + (L + C > 0xFF))``, uncast where the context converts to a byte."""
        for n, expr in enumerate(graph.expr):
            for node in q.walk(expr, q.TYPES) if expr is not None else ():
                parts = self._halves(node)
                if parts is None or facts(node, set(self.types)).effect:
                    continue
                parent, target = self._context(expr, node)
                if target is None and not valued(parent, node):
                    continue
                (h1, l1), (h2, l2) = parts
                total = c_ast.BinaryOp(
                    "+", c_ast.BinaryOp("+", h1, h2), _over(c_ast.BinaryOp("+", l1, l2))
                )
                if not _byte_type(target):
                    total = _byte_cast(total)
                yield _rewritten(items, graph, {id(node): total}, only=n)

    def _halves(self, e):
        """``((H1, L1), (H2, L2))`` of ``(uint16_t)(P1 + P2) >> 8``, each ``P`` a
        ``(uint16_t)H << 8 | L`` of bytes, else None."""
        total = _shift(e, ">>")
        if not (isinstance(total, c_ast.BinaryOp) and total.op == "+"):
            return None
        got = [self._pair(p) for p in (total.left, total.right)]
        return None if None in got else tuple(got)

    def _pair(self, e):
        """``(H, L)`` of ``(uint16_t)H << 8 | L`` (either way round) of bytes, else None."""
        if not (isinstance(e, c_ast.BinaryOp) and e.op == "|"):
            return None
        for hi, lo in ((e.left, e.right), (e.right, e.left)):
            byte = _shift(hi)
            if byte is not None and self._byte(byte) and self._byte(lo):
                return byte, lo
        return None


def _or_of(e, t, x):
    """Whether ``e`` is ``t > 0xFF || x > 0xFF`` (either way round)."""
    if not (isinstance(e, c_ast.BinaryOp) and e.op == "||"):
        return False
    return (_is_over(e.left, t) and _is_over(e.right, x)) or (
        _is_over(e.left, x) and _is_over(e.right, t)
    )


def _rewritten(items, graph, new, only=None):
    """``items`` with each node whose id ``new`` holds replaced in the expressions of the
    graph's nodes (of node ``only`` alone when given)."""

    def swap(x):
        return copy.deepcopy(new[id(x)]) if id(x) in new else None

    def fix(lst, i, st):
        nodes = graph.heads.get((id(lst), i), [])
        exprs = [
            q.replaced(e, swap) if only is None or k == only else e
            for e, k in zip(heads(st), nodes)
        ]
        return rehead(st, exprs)

    return remap(items, fix)


def _halved(items, v):
    """Whether a carry ``v > 0xFF`` in ``items`` is one operand of an ``||``: a chain only
    partly summed."""
    for st in walk(items):
        for e in heads(st):
            for n in q.walk(e, q.TYPES):
                if isinstance(n, c_ast.BinaryOp) and n.op == "||":
                    if _is_over(n.left, v) or _is_over(n.right, v):
                        return True
    return False


def _score(items):
    """What a rewrite must lower: cognitive complexity, then statements, then nodes."""
    nodes = sum(1 for st in walk(items) for e in heads(st) for _ in q.walk(e, q.TYPES))
    return q.cognitive(braced(items)), sum(1 for _ in walk(items)), nodes


def _run(items, types, eligible, typed, stops):
    """``(items, types, widened)`` of :class:`Carrier` over ``eligible``, a variable whose
    chain is left partly summed left a byte."""
    eligible = set(eligible)
    while True:
        mine = dict(types)
        carrier = Carrier(mine, eligible, *typed, stops)
        got = carrier(items)
        partial = {v for v in carrier.widened if _halved(got, v)}
        if not partial:
            return got, mine, carrier.widened
        eligible -= partial


def carried(items, types, eligible, typed=(None, None), stops=None):
    """``items`` with each add-with-carry chain rewritten (:class:`Carrier`; ``typed`` the
    type tree the function returns and ``outer``) where that lowers :func:`_score`, and the
    variables widened; each widening kept only where leaving it out scores worse."""
    eligible = set(eligible)
    best = _run(items, types, eligible, typed, stops)
    for v in sorted(best[2]):
        if v in best[2]:
            other = _run(items, types, eligible - {v}, typed, stops)
            if _score(other[0]) <= _score(best[0]):
                best, eligible = other, eligible - {v}
    if _score(best[0]) >= _score(items):
        return items, set()
    types.update(best[1])
    return best[0], best[2]
