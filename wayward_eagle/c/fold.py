"""Single-use temporaries folded into their use (dewolf's expression propagation): a local
set by ``v = e;`` and read once is replaced there by ``e`` where that reads what ``v`` did,
in the same order with every effect, and costs no more (docs/rewrites.md, *Finishing*)."""

from __future__ import annotations

import copy
from typing import NamedTuple

from pycparser import c_ast

from . import query as q
from .ints import MODELS, Int, common, literal, promoted, type_of
from .listflow import Graph, reached
from .stmt import braced, children, heads, rebuild, reheaded

#: The most nodes (types aside) the use's expression may hold once ``e`` stands in it.
SIZE = 24


class Facts(NamedTuple):
    """What an expression does: the locals it reads and stores, whether it reads memory
    (anything but a local), and whether it may change memory (a store to it, or a call)."""

    reads: frozenset
    stores: frozenset
    memory: bool
    effect: bool


def facts(
    e, local, skip=frozenset(), without=None, arrays=frozenset()
):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """:class:`Facts` of expression ``e`` over locals ``local`` (the object a plain ``=``
    stores to is not read, though what locates it is, nor is an array of ``arrays`` named):
    the subtree ``without`` left out, and the store or call of each node whose id ``skip``
    holds (the ancestors of ``without``, acting after it) not counted."""
    reads, stores, memory, effect = set(), set(), False, False
    callees = {id(n.name) for n in q.walk(e) if isinstance(n, c_ast.FuncCall)}
    fields = {id(n.field) for n in q.walk(e) if isinstance(n, c_ast.StructRef)}
    targets = {id(n.lvalue) for n in q.walk(e) if isinstance(n, c_ast.Assignment) and n.op == "="}
    todo = [e]
    while todo:
        n = todo.pop()
        if n is None or n is without or isinstance(n, q.TYPES):
            continue
        todo += q.kids(n)
        if id(n) in targets or id(n) in callees | fields:
            pass
        elif isinstance(n, c_ast.ID):
            if n.name in local:
                reads.add(n.name)
            elif n.name not in arrays:
                memory = True
        elif isinstance(n, (c_ast.ArrayRef, c_ast.StructRef)) or (
            isinstance(n, c_ast.UnaryOp) and n.op == "*"
        ):
            memory = True
        elif isinstance(n, c_ast.FuncCall) and id(n) not in skip:
            memory = effect = True
        lhs = q.stored(n)
        if lhs is not None and id(n) not in skip:
            if isinstance(lhs, c_ast.ID) and lhs.name in local:
                stores.add(lhs.name)
            else:
                effect = True
    return Facts(frozenset(reads), frozenset(stores), memory, effect)


def clash(a, b):
    """Whether the evaluations of :class:`Facts` ``a`` and ``b`` may not be reordered."""
    if a.stores & (b.reads | b.stores) or b.stores & a.reads:
        return True
    return (a.effect and (b.memory or b.effect)) or (b.effect and a.memory)


class Bound(NamedTuple):
    """The type an expression has and the least and greatest values it may take."""

    type: Int
    lo: int
    hi: int


def _full(t):
    if t.bits == 1:
        return Bound(t, 0, 1)
    if t.signed:
        return Bound(t, -(1 << (t.bits - 1)), (1 << (t.bits - 1)) - 1)
    return Bound(t, 0, (1 << t.bits) - 1)


def _in(b, t):
    """``b`` converted to ``t``: kept when ``t`` holds its range, else ``t``'s whole range."""
    full = _full(t)
    if full.lo <= b.lo and b.hi <= full.hi:
        return Bound(t, b.lo, b.hi)
    return full


class Types:
    """The integer types of names: ``local`` (``{name: type tree}``) and ``outer``
    (``{name: declarator}`` of file-scope objects and functions, a ``c_ast.Decl``'s
    ``type``)."""

    def __init__(self, local, outer=None):
        self.local, self.outer = local, outer or {}

    def variable(self, name, bits):
        """The :class:`Int` of variable ``name``, None when unknown or not an integer."""
        tree = self.local.get(name)
        if tree is None:
            tree = self.outer.get(name)
            if not isinstance(tree, c_ast.TypeDecl) or "volatile" in tree.quals:
                return None
        return type_of(tree, bits)

    def element(self, e, bits):
        """The :class:`Int` of ``e``, a subscript of an array declared at file scope."""
        base = e.name
        tree = self.outer.get(base.name) if isinstance(base, c_ast.ID) else None
        if not isinstance(tree, c_ast.ArrayDecl) or "volatile" in tree.type.quals:
            return None
        return type_of(tree.type, bits)

    def function(self, name):
        """The ``FuncDecl`` of function ``name``, or None."""
        tree = self.outer.get(name)
        return tree if isinstance(tree, c_ast.FuncDecl) else None

    def returned(self, call, bits):
        """The :class:`Int` call ``call`` returns, None when unknown."""
        fn = self.function(q.callee(call))
        return None if fn is None else type_of(fn.type, bits)

    def parameter(self, call, k):
        """The type tree of argument ``k`` of call ``call``'s prototype, or None."""
        fn = self.function(q.callee(call))
        params = list(fn.args.params) if fn is not None and fn.args is not None else []
        if k >= len(params) or not isinstance(params[k], (c_ast.Decl, c_ast.Typename)):
            return None
        tree = params[k].type
        return None if q.is_void(tree) else tree

    def bound(self, e, bits):  # pylint: disable=too-many-return-statements
        """The :class:`Bound` of expression ``e`` with an ``int`` of ``bits`` bits, None when
        its type is unknown."""
        if isinstance(e, c_ast.Constant):
            got = literal(e, bits)
            return None if got is None else Bound(got[1], got[0], got[0])
        if isinstance(e, c_ast.ID):
            t = self.variable(e.name, bits)
            return None if t is None else _full(t)
        if isinstance(e, c_ast.ArrayRef):
            t = self.element(e, bits)
            return None if t is None else _full(t)
        if isinstance(e, c_ast.FuncCall):
            t = self.returned(e, bits)
            return None if t is None else _full(t)
        if isinstance(e, c_ast.Cast):
            t, x = type_of(e.to_type, bits), self.bound(e.expr, bits)
            return None if t is None or x is None else _in(x, t)
        if isinstance(e, c_ast.UnaryOp):
            return self._unary(e, bits)
        if isinstance(e, c_ast.TernaryOp):
            a, b = self.bound(e.iftrue, bits), self.bound(e.iffalse, bits)
            if a is None or b is None or self.bound(e.cond, bits) is None:
                return None
            t = common(a.type, b.type, bits)
            return _in(Bound(t, min(a.lo, b.lo), max(a.hi, b.hi)), t)
        if isinstance(e, c_ast.BinaryOp):
            return self._binary(e, bits)
        return None

    def _unary(self, e, bits):
        x = self.bound(e.expr, bits)
        if x is None or e.op not in ("-", "+", "~", "!"):
            return None
        if e.op == "!":
            return Bound(Int(bits, True), 0, 1)
        t = promoted(x.type, bits)
        if e.op == "+":
            return _in(x, t)
        if e.op == "-" and t.signed:
            return _in(Bound(t, -x.hi, -x.lo), t)
        return _full(t)

    def _binary(self, e, bits):  # pylint: disable=too-many-return-statements
        a, b = self.bound(e.left, bits), self.bound(e.right, bits)
        if a is None or b is None:
            return None
        if e.op in ("&&", "||", "<", ">", "<=", ">=", "==", "!="):
            return Bound(Int(bits, True), 0, 1)
        if e.op in ("<<", ">>"):
            t = promoted(a.type, bits)
            a = _in(a, t)
            if b.lo != b.hi or not 0 <= b.lo < t.bits or a.lo < 0:
                return _full(t)
            k = b.lo
            if e.op == ">>":
                return Bound(t, a.lo >> k, a.hi >> k)
            return _in(Bound(t, a.lo << k, a.hi << k), t)
        t = common(a.type, b.type, bits)
        a, b = _in(a, t), _in(b, t)
        return _in(self._arith(e.op, a, b, t), t)

    @staticmethod
    def _arith(op, a, b, t):  # pylint: disable=too-many-return-statements
        """The range of ``a op b`` in mathematical integers, ``t``'s whole when unknown."""
        if op == "+":
            return Bound(t, a.lo + b.lo, a.hi + b.hi)
        if op == "-":
            return Bound(t, a.lo - b.hi, a.hi - b.lo)
        if op == "*":
            got = [x * y for x in (a.lo, a.hi) for y in (b.lo, b.hi)]
            return Bound(t, min(got), max(got))
        if min(a.lo, b.lo) < 0:
            return _full(t)
        if op == "&":
            return Bound(t, 0, min(a.hi, b.hi))
        if op in ("|", "^"):
            return Bound(t, 0, (1 << max(a.hi, b.hi).bit_length()) - 1)
        if op in ("/", "%") and b.lo > 0:
            return Bound(t, a.lo // b.hi, a.hi // b.lo) if op == "/" else Bound(t, 0, b.hi - 1)
        return _full(t)


class Site(NamedTuple):
    """Where a fold happens: the definition ``v = e`` (its position, ``(id(list), index)``,
    and node), the use (its statement, position, node, the expression of the statement holding
    the read, and the read). A statement may stand at several positions."""

    var: str
    value: object
    define: tuple
    node: int
    use: object
    spot: tuple
    at: int
    expr: object
    read: object


def _positions(items):
    for i, st in enumerate(items):
        yield items, i, st
        for sub in children(st):
            yield from _positions(sub)


class Folder:  # pylint: disable=too-few-public-methods
    """The folds of one statement list: ``types`` its locals' type trees, ``ret`` the type
    tree the function returns (None for nothing), ``outer`` as :class:`Types` takes it."""

    def __init__(self, types, ret=None, outer=None, stops=None, size=SIZE):
        self.local, self.ret, self.stops, self.size = set(types), ret, stops, size
        self.types = Types(types, outer)
        self.arrays = frozenset(
            k for k, t in (outer or {}).items() if isinstance(t, c_ast.ArrayDecl)
        )

    def __call__(self, items):
        """``items`` with each fold that applies made, until none does."""
        while True:
            got = self._once(items)
            if got is None:
                return items
            items = got

    def _once(self, items):
        graph = Graph(items, self.stops)
        before = q.cognitive(braced(items))
        for lst, i, st in _positions(items):
            site = self._site(graph, items, (id(lst), i), st)
            if site is None:
                continue
            new = q.replaced(
                site.expr, lambda n, s=site: copy.deepcopy(s.value) if n is s.read else None
            )
            got = _edited(items, site, new)
            if _nodes(new) <= self.size and q.cognitive(braced(got)) <= before:
                return got
        return None

    def _site(self, graph, items, spot, st):
        """The :class:`Site` folding definition ``st`` at position ``spot``, None when it does
        not fold."""
        if not (
            isinstance(st, c_ast.Assignment)
            and st.op == "="
            and isinstance(st.lvalue, c_ast.ID)
            and st.lvalue.name in self.local
        ):
            return None
        v, d = st.lvalue.name, graph.heads.get(spot, [None])[0]
        mine = facts(st.rvalue, self.local, arrays=self.arrays)
        if d is None or mine.stores or v in mine.reads or q.commas(st.rvalue):
            return None
        readers = _reach_reads(graph, d, graph.mask((v,)))
        found = (
            _use_of(items, graph, readers[0])
            if len(readers) == 1 and not _volatile(st.rvalue)
            else None
        )
        if found is None:
            return None
        reads = _occurrences(found[2], v)
        if len(reads) != 1 or v in _stores_in(found[2], v):
            return None
        site = Site(v, st.rvalue, spot, d, found[0], found[1], readers[0], found[2], reads[0])
        return site if self._legal(graph, site, mine) else None

    def _legal(self, graph, site, mine):
        """Whether ``site``'s fold keeps every value read and every effect's order: along
        the paths from the definition to the use, and within the use."""
        return self._clear(graph, site, mine) and self._inside(site, mine)

    def _clear(self, graph, site, mine):
        """Whether the definition alone reaches the use, the use runs once for each time it
        runs, and nothing on a path between them clashes with its value (one with an effect:
        nothing is between them)."""
        d, u, bit = site.node, site.at, graph.mask((site.var,))
        others = [s for n, m in enumerate(graph.store) if m & bit and n != d for s in graph.succ[n]]
        if u in reached(graph.succ, [graph.entry, *others], {d}):
            return False
        if u in reached(graph.succ, graph.succ[u], {d}):
            return False
        preds = graph.predecessors()
        if mine.effect:
            return graph.succ[d] == [u] and preds[u] == [d]
        between = reached(graph.succ, graph.succ[d], {d, u}) & reached(preds, preds[u], {d, u})
        for n in between - {d, u}:
            e = graph.expr[n]
            if e is not None and (
                clash(mine, facts(e, self.local, arrays=self.arrays))
                or graph.store[n] & bit
                or (mine.memory and _volatile(e))
            ):
                return False
        return True

    def _inside(self, site, mine):
        """Whether the rest of the use's expression lets the value move in: no clash with
        what is not sequenced after the read, no ``sizeof``, an effect not made conditional,
        and the same value read (:meth:`_same`)."""
        path = _ancestors(site.expr, site.read)
        if any(isinstance(a, c_ast.UnaryOp) and a.op == "sizeof" for a in path):
            return False
        commas = {id(x) for x in q.commas(site.expr)}
        if mine.effect and any(
            isinstance(a, c_ast.TernaryOp)
            or id(a) in commas
            or (isinstance(a, c_ast.BinaryOp) and a.op in ("&&", "||"))
            for a in path
        ):
            return False
        rest = facts(site.expr, self.local, frozenset(map(id, path)), site.read, self.arrays)
        return not clash(mine, rest) and self._same(site, path)

    def _same(self, site, path):
        """Whether reading ``e`` where the use reads ``v`` gives what that read gave, in both
        data models: the context converts to ``v``'s own type, or ``v``'s type holds every
        value ``e`` takes and either the context needs only that value or ``e``'s type
        promotes as ``v``'s."""
        tree = self.types.local[site.var]
        parent = path[-1] if path else None
        target = self._target(site, parent)
        if target is not None and _alike(target, tree):
            return True
        for bits in MODELS:
            t, b = type_of(tree, bits), self.types.bound(site.value, bits)
            if t is None or b is None or _in(b, t) != Bound(t, b.lo, b.hi):
                return False
            if (
                target is None
                and not _valued(parent, site.read)
                and promoted(b.type, bits) != promoted(t, bits)
            ):
                return False
        return True

    def _target(self, site, parent):
        """The type tree the read's context converts it to, or None."""
        read = site.read
        if isinstance(parent, c_ast.Assignment) and parent.op == "=" and parent.rvalue is read:
            lv = parent.lvalue
            if isinstance(lv, c_ast.ID):
                return self.types.local.get(lv.name) or _object(self.types.outer.get(lv.name))
            if isinstance(lv, c_ast.ArrayRef) and isinstance(lv.name, c_ast.ID):
                tree = self.types.outer.get(lv.name.name)
                return tree.type if isinstance(tree, c_ast.ArrayDecl) else None
            return None
        if isinstance(parent, c_ast.ExprList):
            for call in q.walk(site.expr):
                if isinstance(call, c_ast.FuncCall) and call.args is parent:
                    k = next(i for i, x in enumerate(parent.exprs) if x is read)
                    return self.types.parameter(call, k)
        if isinstance(parent, c_ast.Return):
            return self.ret
        return None


def _alike(a, b):
    """Whether type trees ``a`` and ``b`` are one integer type in both data models."""
    return all(
        type_of(a, bits) is not None and type_of(a, bits) == type_of(b, bits) for bits in MODELS
    )


def _object(tree):
    return tree if isinstance(tree, c_ast.TypeDecl) else None


def _valued(parent, read):
    """Whether ``read``'s context needs only its value: the condition of a statement, the
    operand of ``!`` or of ``&&``/``||``, an expression statement's own value."""
    if parent is None:
        return True
    if isinstance(parent, c_ast.UnaryOp) and parent.op == "!":
        return True
    if isinstance(parent, c_ast.BinaryOp) and parent.op in ("&&", "||"):
        return True
    return isinstance(parent, c_ast.TernaryOp) and parent.cond is read


def _reach_reads(graph, d, bit):
    """The nodes reading the variable of ``bit`` that the store at node ``d`` reaches."""
    edges = [[] if graph.kill[n] & bit else s for n, s in enumerate(graph.succ)]
    return sorted(n for n in reached(edges, graph.succ[d], {d}) if graph.use[n] & bit)


def _use_of(items, graph, n):
    """``(statement, position, expression)`` of the statement of ``items`` evaluating node
    ``n``."""
    for lst, i, st in _positions(items):
        nodes = graph.heads.get((id(lst), i), [])
        if n in nodes:
            return st, (id(lst), i), heads(st)[nodes.index(n)]
    return None


def _occurrences(e, v):
    """The ``ID`` nodes of ``e`` reading variable ``v``."""
    skip = {
        id(x.lvalue)
        for x in q.walk(e)
        if isinstance(x, c_ast.Assignment) and x.op == "=" and isinstance(x.lvalue, c_ast.ID)
    }
    skip |= {id(x.name) for x in q.walk(e) if isinstance(x, c_ast.FuncCall)}
    skip |= {id(x.field) for x in q.walk(e) if isinstance(x, c_ast.StructRef)}
    return [
        x
        for x in q.walk(e, q.TYPES)
        if isinstance(x, c_ast.ID) and x.name == v and id(x) not in skip
    ]


def _stores_in(e, v):
    """``{v}`` when ``e`` stores ``v`` other than as the whole of ``v = ..``."""
    for x in q.walk(e):
        lhs = q.stored(x)
        if (
            isinstance(lhs, c_ast.ID)
            and lhs.name == v
            and not (x is e and isinstance(x, c_ast.Assignment) and x.op == "=")
        ):
            return {v}
    return set()


def _ancestors(e, target):
    """The nodes from ``e`` down to ``target``'s parent."""
    path = []

    def down(n):
        if n is target:
            return True
        path.append(n)
        if any(down(k) for k in q.kids(n)):
            return True
        path.pop()
        return False

    return path if down(e) else []


def _volatile(e):
    return any(
        isinstance(n, (c_ast.TypeDecl, c_ast.PtrDecl)) and "volatile" in (n.quals or ())
        for n in q.walk(e)
    )


def _nodes(e):
    return sum(1 for _ in q.walk(e, q.TYPES) if not isinstance(_, q.TYPES))


def _edited(items, site, new):
    """``items`` without ``site``'s definition and its use evaluating ``new``."""
    out = []
    for i, st in enumerate(items):
        if (id(items), i) == site.define:
            continue
        if (id(items), i) == site.spot:
            st = reheaded(st, [new if x is site.expr else x for x in heads(st)])
        if children(st):
            st = rebuild(st, [_edited(sub, site, new) for sub in children(st)])
        out.append(st)
    return out


def folded(items, types, ret=None, outer=None, stops=None):  # pylint: disable=too-many-arguments
    """``items`` with each single-use temporary folded (:class:`Folder`)."""
    return Folder(types, ret, outer, stops)(items)
