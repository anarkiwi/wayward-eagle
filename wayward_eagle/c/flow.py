"""Control flow and dataflow over ``c_ast``: a function body's control-flow graph, a
bit-vector worklist solver, and over a unit's function the analyses of its own variables
(reaching definitions, available copies, liveness) and the copies they find."""

from __future__ import annotations

import functools
import operator
from collections import deque
from typing import NamedTuple

from pycparser import c_ast

from . import query as q
from .front import Opaque

#: Declarations of variables a function owns, when automatic and never addressed.
OWNED = ("VarDecl", "ParmVarDecl")
#: Operators whose right operand runs on some evaluations only.
SHORT = ("&&", "||")
NOTHING = frozenset()


class CFG:
    """A control-flow graph: vertex ``i`` evaluates ``nodes[i]`` (an expression, a
    declaration, a condition, a ``return``, a computed ``goto``) or is a join (None) of
    statement ``origin[i]``; ``succ[i]`` its successors, ``entry`` and ``exit`` its ends."""

    def __init__(self):
        self.nodes, self.origin, self.succ = [], [], []
        self.entry = self.add(None)
        self.exit = self.add(None)

    def add(self, node, succ=(), origin=None):
        """A new vertex evaluating ``node`` (a join of ``origin`` when None)."""
        self.nodes.append(node)
        self.origin.append(origin)
        self.succ.append(list(succ))
        return len(self.nodes) - 1

    @functools.cached_property
    def order(self):
        """The vertices reachable from ``entry`` in reverse postorder."""
        seen, post, todo = {self.entry}, [], [(self.entry, iter(self.succ[self.entry]))]
        while todo:
            x, it = todo[-1]
            nxt = next((s for s in it if s not in seen), None)
            if nxt is None:
                post.append(x)
                todo.pop()
            else:
                seen.add(nxt)
                todo.append((nxt, iter(self.succ[nxt])))
        return post[::-1]

    @functools.cached_property
    def reachable(self):
        """The vertices reachable from ``entry``."""
        return frozenset(self.order)

    @functools.cached_property
    def pred(self):
        """Each vertex's reachable predecessors."""
        out = [[] for _ in self.nodes]
        for x in self.order:
            for s in self.succ[x]:
                out[s].append(x)
        return out

    def joins(self):
        """The reachable vertices control enters from two or more reachable ones."""
        return [x for x in self.order if len(self.pred[x]) > 1]


class _Builder:
    """Builds a :class:`CFG` statement by statement, each from its successor back."""

    def __init__(self, noreturn):
        self.g, self.noreturn = CFG(), noreturn
        self.labels, self.computed, self.switches = {}, [], []

    def label(self, name, origin=None):
        """The join vertex of label ``name``."""
        if name not in self.labels:
            self.labels[name] = self.g.add(None)
        if origin is not None:
            self.g.origin[self.labels[name]] = origin
        return self.labels[name]

    def sequence(self, items, nxt, brk, cont):
        """The entry of statements ``items`` run in order, then ``nxt``."""
        for st in reversed(items):
            nxt = self.stmt(st, nxt, brk, cont)
        return nxt

    def expression(self, e, nxt):
        """The entry of expression ``e`` evaluated for its effects, top-level commas apart."""
        if isinstance(e, c_ast.ExprList):
            return self.sequence(e.exprs, nxt, None, None)
        call = e.expr if isinstance(e, c_ast.Cast) else e
        dead = isinstance(call, c_ast.FuncCall) and self.noreturn(call)
        return self.g.add(e, [] if dead else [nxt])

    def stmt(self, n, nxt, brk, cont):  # pylint: disable=too-many-return-statements
        """The entry vertex of statement ``n``, which goes on to ``nxt``; ``break`` goes to
        ``brk`` and ``continue`` to ``cont``."""
        g = self.g
        if n is None or isinstance(n, (c_ast.EmptyStatement, c_ast.Typedef, c_ast.Pragma)):
            return nxt
        if isinstance(n, c_ast.Compound):
            return self.sequence(q.statements(n), nxt, brk, cont)
        if isinstance(n, c_ast.DeclList):
            return self.sequence(n.decls, nxt, brk, cont)
        if isinstance(n, c_ast.Decl):
            return g.add(n, [nxt]) if n.name else nxt
        if isinstance(n, c_ast.If):
            join = g.add(None, [nxt], n)
            return g.add(n.cond, [self.stmt(a, join, brk, cont) for a in (n.iftrue, n.iffalse)])
        if isinstance(n, q.LOOPS):
            return self.loop(n, nxt)
        if isinstance(n, (c_ast.Switch, c_ast.Case, c_ast.Default)):
            return self.switch(n, nxt, brk, cont)
        if isinstance(n, (c_ast.Label, c_ast.Goto)):
            return self.jump(n, nxt, brk, cont)
        if isinstance(n, (c_ast.Break, c_ast.Continue)):
            return brk if isinstance(n, c_ast.Break) else cont
        if isinstance(n, c_ast.Return):
            return g.add(n, [g.exit])
        return self.expression(n, nxt)

    def switch(self, n, nxt, brk, cont):
        """The entry vertex of ``switch`` statement ``n``, or of one of its case labels."""
        g = self.g
        if isinstance(n, c_ast.Switch):
            after = g.add(None, [nxt], n)
            self.switches.append([])
            self.stmt(n.stmt, after, after, cont)
            cases = self.switches.pop()[::-1]
            default = any(isinstance(c, c_ast.Default) for c, _ in cases)
            return g.add(n.cond, [v for _, v in cases] + [after] * (not default))
        join = g.add(None, [self.sequence(n.stmts, nxt, brk, cont)], n)
        self.switches[-1].append((n, join))
        return join

    def jump(self, n, nxt, brk, cont):
        """The entry vertex of label ``n`` (its statement after it) or of goto ``n``."""
        g = self.g
        if isinstance(n, c_ast.Label):
            join = self.label(n.name, n)
            g.succ[join] = [self.stmt(n.stmt, nxt, brk, cont)]
            return join
        if isinstance(n.name, str):
            return self.label(n.name)
        self.computed.append(g.add(n))
        return self.computed[-1]

    def loop(self, n, nxt):
        """The entry vertex of loop ``n``: a join at its head, which the code before it and
        every back edge enter."""
        g = self.g
        head = g.add(None, (), n)
        if isinstance(n, c_ast.DoWhile):
            test = g.add(n.cond, [head, nxt])
            g.succ[head] = [self.stmt(n.stmt, test, nxt, test)]
            return head
        init, step = (n.init, n.next) if isinstance(n, c_ast.For) else (None, None)
        step = self.stmt(step, head, None, None)
        body = self.stmt(n.stmt, step, nxt, step)
        g.succ[head] = [g.add(n.cond, [body, nxt]) if n.cond is not None else body]
        return self.stmt(init, head, None, None)


def build(body, noreturn=lambda call: False):
    """The :class:`CFG` of function body ``body``; a statement that is a call ``noreturn``
    holds of has no successor. A computed ``goto`` may go to any label."""
    b = _Builder(noreturn)
    g = b.g
    g.succ[g.entry] = [b.stmt(body, g.exit, None, None)]
    for x in b.computed:
        g.succ[x] = list(b.labels.values())
    return g


def solve(cfg, gen, kill, universe=None, forward=True):  # pylint: disable=too-many-locals
    """``(into, out)``: each vertex's facts (bit sets) where it begins and ends, of the problem
    a vertex of which brings ``gen`` and stops ``kill``; may (union), or must (intersection)
    given ``universe``; ``forward`` from ``entry``, else backward from ``exit``, which hold
    nothing, as unreachable vertices do."""
    src, dst = (cfg.pred, cfg.succ) if forward else (cfg.succ, cfg.pred)
    start = cfg.entry if forward else cfg.exit
    meet = operator.or_ if universe is None else operator.and_
    init = universe or 0
    before, after = [0] * len(cfg.nodes), [init] * len(cfg.nodes)
    order = cfg.order if forward else cfg.order[::-1]
    todo, queued, reach = deque(order), set(order), cfg.reachable
    while todo:
        x = todo.popleft()
        queued.discard(x)
        ins = [after[p] for p in src[x] if p in reach]
        before[x] = 0 if x == start else functools.reduce(meet, ins, init if ins else 0)
        got = gen[x] | (before[x] & ~kill[x])
        if got != after[x]:
            after[x] = got
            for s in dst[x]:
                if s in reach and s not in queued:
                    queued.add(s)
                    todo.append(s)
    for x in set(range(len(cfg.nodes))) - reach:
        before[x] = after[x] = 0
    return (before, after) if forward else (after, before)


def bits(x):
    """The indices of the set bits of ``x``, lowest first."""
    while x:
        low = x & -x
        yield low.bit_length() - 1
        x ^= low


class Def(NamedTuple):
    """A definition of an owned variable: its vertex, the variable's symbol id, the node (None
    for a parameter on entry), whether it surely stores, and for a copy what it copies
    (``("var", id)`` or ``("const", key)``), else None."""

    vertex: int
    var: int
    node: object
    must: bool
    copy: tuple | None


class Facts(NamedTuple):
    """A vertex's owned variables read, those a read may follow a store of (``late``), symbol
    ids it may store to, whether it may write memory no name reaches (``clobber``), and
    whether it reads all before its one store, its top node (``ordered``)."""

    reads: frozenset
    late: frozenset
    stores: frozenset
    clobber: bool
    ordered: bool


class Flow:  # pylint: disable=too-many-instance-attributes
    """The dataflow of function ``fd`` of ``unit`` over its owned variables: automatic, neither
    array nor volatile, never addressed by ``&`` or inline assembly. ``effect(call)``: whether
    a call may write memory (default every call); ``copyable(symbol, rhs)``: whether
    ``symbol = rhs`` copies ``rhs`` unchanged (default a variable of the same type)."""

    def __init__(self, unit, fd, effect=None, copyable=None):
        self.unit, self.fd = unit, fd
        self.effect = effect or (lambda call: True)
        self.copyable = copyable or self._same_type
        self.own = self._owned(unit.sema[fd].ref)
        self.cfg = build(fd.body, lambda call: getattr(unit.symbol(call), "noreturn", False))
        self.facts = [self._facts(n) for n in self.cfg.nodes]
        self.defs = [
            Def(self.cfg.entry, unit.sema[p].ref, None, True, None)
            for p in _params(fd)
            if p in unit.sema and unit.sema[p].ref in self.own
        ]
        for x, n in enumerate(self.cfg.nodes):
            self.defs += self._defs(x, n)
        self.mask = {}
        for i, d in enumerate(self.defs):
            self.mask[d.var] = self.mask.get(d.var, 0) | 1 << i

    def _owned(self, fn):
        """Symbol ids of ``fd``'s owned variables."""
        unit, banned = self.unit, set()
        for n in q.walk(self.fd):
            if isinstance(n, c_ast.UnaryOp) and n.op == "&":
                banned.update(self._root(n.expr) or ())
            elif isinstance(n, Opaque):
                banned.update(unit.sema[x].ref for x in q.walk(n) if x in unit.sema)
        return frozenset(
            sym.id
            for sym in unit.symbols.values()
            if sym.kind in OWNED
            and sym.function == fn
            and sym.storage not in ("static", "extern")
            and sym.id not in banned
            and "[" not in sym.type.canon
            and "volatile" not in unit.qualifiers(sym.type)
        )

    def _root(self, lv):
        """``{id}`` of the variable whose object lvalue ``lv`` lies in (through an array's
        subscripts and ``.`` members), None when it may lie anywhere (through a pointer)."""
        while not isinstance(lv, c_ast.ID):
            if isinstance(lv, c_ast.ArrayRef) and "[" in self._type(lv.name):
                lv = lv.name
            elif isinstance(lv, c_ast.StructRef) and lv.type == ".":
                lv = lv.name
            elif isinstance(lv, c_ast.Cast):
                lv = lv.expr
            else:
                return None
        ref = self._ref(lv)
        return {ref} if ref else None

    def _type(self, n):
        info = self.unit.sema.get(n)
        return info.type.canon if info is not None and info.type is not None else ""

    def _ref(self, n):
        info = self.unit.sema.get(n)
        return info.ref if info is not None else None

    def _same_type(self, sym, rhs):
        """Whether ``rhs`` is a variable of ``sym``'s type, qualifiers aside."""
        other = self.unit.symbol(rhs) if isinstance(rhs, c_ast.ID) else None
        return other is not None and bare(other.type.canon) == bare(sym.type.canon)

    def _stores(self, n, maybe=False, out=None):
        """``[(id or None, node, must)]`` of each store in ``n``: the variable stored to (None
        through a pointer), whether every evaluation of ``n`` stores all of it."""
        out = [] if out is None else out
        if isinstance(n, c_ast.Decl) and n.name:
            out.append((self._ref(n), n, not maybe))
        lhs = q.stored(n)
        if lhs is not None:
            for r in self._root(lhs) or (None,):
                out.append((r, n, not maybe and isinstance(lhs, c_ast.ID)))
        if isinstance(n, c_ast.BinaryOp) and n.op in SHORT:
            parts = ((n.left, maybe), (n.right, True))
        elif isinstance(n, c_ast.TernaryOp):
            parts = ((n.cond, maybe), (n.iftrue, True), (n.iffalse, True))
        else:
            parts = ((k, maybe) for k in q.kids(n) if not isinstance(k, q.TYPES))
        for k, m in parts:
            self._stores(k, m, out)
        return out

    def _reads(self, n):
        """Owned variables node ``n`` reads (an assignment's target is not read)."""
        skip = {
            id(x.lvalue)
            for x in q.walk(n)
            if isinstance(x, c_ast.Assignment) and x.op == "=" and isinstance(x.lvalue, c_ast.ID)
        }
        return frozenset(
            r
            for x in q.walk(n, q.TYPES)
            if isinstance(x, c_ast.ID) and id(x) not in skip and (r := self._ref(x)) in self.own
        )

    @staticmethod
    def _evaluated(n):
        """The node a vertex evaluates: a ``return``'s value, None for a join."""
        return n.expr if isinstance(n, c_ast.Return) else n

    def _facts(self, n):
        n = self._evaluated(n)
        if n is None:
            return Facts(NOTHING, NOTHING, NOTHING, False, False)
        stores, reads = self._stores(n), self._reads(n)
        names = frozenset(r for r, _, _ in stores if r is not None)
        calls = any(self.effect(c) for c in q.calls(n))
        clobber = isinstance(n, Opaque) or calls or any(r is None for r, _, _ in stores)
        top = len(stores) == 1 and stores[0][1] is n
        return Facts(reads, NOTHING if top else reads & names, names, clobber, top and not calls)

    def _defs(self, x, n):
        """The definitions of owned variables vertex ``x`` (evaluating ``n``) makes."""
        n = self._evaluated(n)
        if n is None:
            return []
        stores = self._stores(n)
        return [
            Def(x, r, node, must, self._copy(node) if len(stores) == 1 and node is n else None)
            for r, node, must in stores
            if r in self.own
        ]

    def _copy(self, n):
        """The key of what plain assignment or initialisation ``n`` copies, or None."""
        if isinstance(n, c_ast.Decl):
            lhs, rhs = n, n.init
        elif isinstance(n, c_ast.Assignment) and n.op == "=":
            lhs, rhs = n.lvalue, n.rvalue
        else:
            return None
        sym = self.unit.symbol(lhs)
        if rhs is None or isinstance(rhs, c_ast.InitList) or not self.copyable(sym, rhs):
            return None
        other = self.unit.symbol(rhs) if isinstance(rhs, c_ast.ID) else None
        if other is None or other.kind == "EnumConstantDecl":
            return ("const", q.key(rhs, self.unit))
        if other.id == sym.id or "volatile" in self.unit.qualifiers(other.type):
            return None
        return ("var", other.id)

    # Analyses.

    def reaching(self):
        """Each vertex's reaching definitions (bit sets over :attr:`defs`) where it begins."""
        n = len(self.cfg.nodes)
        gen, kill = [0] * n, [0] * n
        for i, d in enumerate(self.defs):
            gen[d.vertex] |= 1 << i
            if d.must:
                kill[d.vertex] |= self.mask[d.var]
        return solve(self.cfg, gen, [k & ~g for k, g in zip(kill, gen)])[0]

    def pairs(self):
        """``{(variable id, source key): bit}`` of every copy :attr:`defs` makes."""
        out = {}
        for d in self.defs:
            if d.copy is not None:
                out.setdefault((d.var, d.copy), len(out))
        return out

    def available(self, pairs=None):
        """``(into, kill)``: each vertex's available copies (bits of :meth:`pairs`) where it
        begins, a copy available when every path copies and changes neither side after (a
        store to either, any write to memory for a source not owned); what each stops."""
        pairs = self.pairs() if pairs is None else pairs
        involve, memory = {}, 0
        for (v, src), b in pairs.items():
            for s in (v, src[1]) if src[0] == "var" else (v,):
                involve[s] = involve.get(s, 0) | 1 << b
            if src[0] == "var" and src[1] not in self.own:
                memory |= 1 << b
        gen = [0] * len(self.cfg.nodes)
        for d in self.defs:
            if d.copy is not None:
                gen[d.vertex] |= 1 << pairs[d.var, d.copy]
        kill = [
            functools.reduce(operator.or_, (involve.get(s, 0) for s in f.stores), 0)
            | (memory if f.clobber else 0)
            for f in self.facts
        ]
        return solve(self.cfg, gen, kill, (1 << len(pairs)) - 1)[0], kill

    def liveness(self):
        """Each vertex's live owned variables where it begins, as symbol id sets."""
        ids = sorted(self.own)
        bit = {v: 1 << i for i, v in enumerate(ids)}
        use = [sum(bit[v] for v in f.reads) for f in self.facts]
        kill = [0] * len(self.cfg.nodes)
        for d in self.defs:
            if d.must and d.node is not None:
                kill[d.vertex] |= bit[d.var] & ~use[d.vertex]
        into = solve(self.cfg, use, kill, forward=False)[0]
        return [frozenset(ids[i] for i in bits(x)) for x in into]

    # What they find.

    def copies(self):
        """``[(def, [vertex])]`` of each copy whose variable its source could replace at every
        use it reaches (and it reaches one): no other definition reaches the use, the copy
        is available there, and the vertex reads the variable before it changes the copy."""
        pairs = self.pairs()
        rd = self.reaching()
        avail, kill = self.available(pairs)
        uses, bad = {}, set()
        for x in self.cfg.order:
            f = self.facts[x]
            for v in f.reads:
                reach = rd[x] & self.mask.get(v, 0)
                for i in bits(reach):
                    d = self.defs[i]
                    pair = 1 << pairs[v, d.copy] if d.copy is not None else 0
                    held = avail[x] & pair and (f.ordered or not kill[x] & pair)
                    if held and reach == 1 << i and v not in f.late:
                        uses.setdefault(i, []).append(x)
                    else:
                        bad.add(i)
        return [(self.defs[i], xs) for i, xs in sorted(uses.items()) if i not in bad]

    def edge_copies(self):
        """``[(join, variable id, [def])]`` of each join and variable two or more distinct
        copies reach on its incoming edges, each the last store to it on every path of its
        edge, the variable not read since: the copies an out-of-SSA translation leaves."""
        n = len(self.cfg.nodes)
        gen, kill = [0] * n, [0] * n
        for i, d in enumerate(self.defs):
            if d.copy is not None and d.must:
                gen[d.vertex] |= 1 << i
        for x, f in enumerate(self.facts):
            for v in f.reads | f.stores:
                kill[x] |= self.mask.get(v, 0)
        kill = [k & ~g for k, g in zip(kill, gen)]
        out = solve(self.cfg, gen, kill, (1 << len(self.defs)) - 1)[1]
        found = []
        for j in self.cfg.joins():
            by_var = {}
            for p in self.cfg.pred[j]:
                for i in bits(out[p]):
                    by_var.setdefault(self.defs[i].var, set()).add(i)
            found += [
                (j, v, [self.defs[i] for i in sorted(ds)])
                for v, ds in by_var.items()
                if len(ds) > 1
            ]
        return found


def _params(fd):
    """The parameter declarations of function definition ``fd``."""
    args = fd.decl.type.args
    return [p for p in (args.params if args else ()) if isinstance(p, c_ast.Decl)]


def bare(canon):
    """A canonical type spelling without ``const`` and ``volatile``."""
    for qual in ("const ", "volatile "):
        canon = canon.replace(qual, "")
    return canon
