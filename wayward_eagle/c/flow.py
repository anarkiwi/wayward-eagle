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
from . import ints

#: Declarations of variables a function owns, when automatic and never addressed.
OWNED = ("VarDecl", "ParmVarDecl")
#: Operators whose right operand runs on some evaluations only.
SHORT = ("&&", "||")
NOTHING = frozenset()
#: Locations beyond named variables: memory through a pointer, devices (volatile accesses).
MEMORY, DEVICE = "memory", "device"
DEVICES = frozenset((DEVICE,))
#: Operators whose operand is not evaluated.
UNEVALUATED = ("sizeof", "_Alignof")
EVERYTHING = frozenset((MEMORY, DEVICE))
#: Why a definition's variable exists, in the order a definition takes the first that holds.
REASONS = ("unused", "check-only", "clobbered", "merged", "multi-use", "single-use")


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

    def __init__(self, noreturn, gotos=False):
        self.g, self.noreturn, self.gotos = CFG(), noreturn, gotos
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
            return g.add(n, [self.label(n.name)]) if self.gotos else self.label(n.name)
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


def build(body, noreturn=lambda call: False, gotos=False):
    """The :class:`CFG` of function body ``body``; a statement that is a call ``noreturn``
    holds of has no successor. A computed ``goto`` may go to any label; with ``gotos``, a
    named ``goto`` is a vertex of its own too, so a label's predecessors tell its jumps from
    the statement falling into it."""
    b = _Builder(noreturn, gotos)
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


def _type(unit, n):
    info = unit.sema.get(n)
    return info.type if info is not None else None


def root(unit, lv):
    """``{id}`` of the variable whose object lvalue ``lv`` lies in (through an array's
    subscripts and ``.`` members), None when it may lie anywhere (through a pointer)."""
    while not isinstance(lv, c_ast.ID):
        if isinstance(lv, c_ast.ArrayRef) and "[" in getattr(_type(unit, lv.name), "canon", ""):
            lv = lv.name
        elif isinstance(lv, c_ast.StructRef) and lv.type == ".":
            lv = lv.name
        elif isinstance(lv, c_ast.Cast):
            lv = lv.expr
        else:
            return None
    info = unit.sema.get(lv)
    return {info.ref} if info is not None and info.ref else None


def place(unit, lv):
    """The locations an access to lvalue ``lv`` touches: :data:`DEVICE` when it is volatile,
    none in an object declared ``const``, else the element ``(id, index)`` of an array
    variable a constant subscripts, the variable it lies in, or :data:`MEMORY`."""
    typ, at = _type(unit, lv), root(unit, lv)
    quals = unit.qualifiers(typ) if typ is not None else ()
    if "volatile" in quals:
        return DEVICES
    if at is None:
        return frozenset((MEMORY,))
    if "const" in quals:
        return NOTHING
    if isinstance(lv, c_ast.ArrayRef) and isinstance(lv.name, c_ast.ID):
        got = {v and v[0] for v in (ints.value(lv.subscript, {}, b) for b in ints.MODELS)}
        if len(got) == 1 and None not in got:
            return frozenset(((*at, got.pop()),))
    return frozenset(at)


def _accessed(unit, n):
    """Whether evaluating ``n`` reads the object it designates: a variable not an array, a
    subscript, a member, a dereference."""
    if isinstance(n, c_ast.ID):
        sym = unit.symbol(n)
        return sym is not None and sym.kind in OWNED and "[" not in sym.type.canon
    return isinstance(n, (c_ast.ArrayRef, c_ast.StructRef)) or (
        isinstance(n, c_ast.UnaryOp) and n.op == "*"
    )


def footprint(unit, n, calls, skip=NOTHING):
    """``(reads, writes)``: the locations evaluating ``n`` may read and write (variables'
    symbol ids, :data:`MEMORY`, :data:`DEVICE`, which a volatile access both reads and
    writes); ``calls(call)`` a call's own; the nodes ``skip`` holds (by ``id``) none of
    their own."""
    nodes, todo = [], [n]
    while todo:
        x = todo.pop()
        nodes.append(x)
        if not (isinstance(x, c_ast.UnaryOp) and x.op in UNEVALUATED):
            todo += [k for k in q.kids(x) if not isinstance(k, q.TYPES)]
    unread = {id(x.lvalue) for x in nodes if isinstance(x, c_ast.Assignment) and x.op == "="}
    unread |= {id(x.expr) for x in nodes if isinstance(x, c_ast.UnaryOp) and x.op == "&"}
    reads, writes = set(), set()
    for x in nodes:
        if id(x) in skip:
            continue
        lhs = q.stored(x)
        if isinstance(x, c_ast.FuncCall):
            r, w = calls(x)
        elif isinstance(x, Opaque):
            r = w = EVERYTHING
        elif lhs is not None:
            w = place(unit, lhs)
            r = w & DEVICES if isinstance(x, c_ast.Assignment) and x.op == "=" else w
        elif isinstance(x, c_ast.Decl) and x.name:
            info = unit.sema.get(x)
            r, w = NOTHING, frozenset((info.ref,)) if info is not None and info.ref else NOTHING
        elif id(x) not in unread and _accessed(unit, x):
            r = place(unit, x)
            w = r & DEVICES
        else:
            continue
        reads |= r
        writes |= w
    return frozenset(reads), frozenset(writes)


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
    ``symbol = rhs`` copies ``rhs`` unchanged (default a variable of the same type);
    ``calls(call)``: the locations a call may read and write, as :func:`footprint`'s
    (default everything)."""

    def __init__(self, unit, fd, effect=None, copyable=None, calls=None):
        self.unit, self.fd = unit, fd
        self.effect = effect or (lambda call: True)
        self.calls = calls or (lambda call: (EVERYTHING, EVERYTHING))
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
                banned.update(root(self.unit, n.expr) or ())
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
            for r in root(self.unit, lhs) or (None,):
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
        return frozenset(self._occurrences(n))

    def _occurrences(self, n):
        """``{id: [ID node]}`` of each owned variable node ``n`` reads, where it reads it."""
        if n is None:
            return {}
        skip = {
            id(x.lvalue)
            for x in q.walk(n)
            if isinstance(x, c_ast.Assignment) and x.op == "=" and isinstance(x.lvalue, c_ast.ID)
        }
        out = {}
        for x in q.walk(n, q.TYPES):
            if isinstance(x, c_ast.ID) and id(x) not in skip and (r := self._ref(x)) in self.own:
                out.setdefault(r, []).append(x)
        return out

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

    def temporaries(self):
        """``[(def, reason)]`` of each assignment or initialisation of an owned variable, by
        the first of :data:`REASONS` that holds (docs/census.md): no use reached;
        every use the condition of an ``if`` a branch of which ends in a call never returning;
        something between it and a use may change what its value reads; another
        definition reaches a use too; more than one use; else one use."""
        rd = self.reaching()
        users, merged = [[] for _ in self.defs], set()
        for x in self.cfg.order:
            for v, ids in self._occurrences(self._evaluated(self.cfg.nodes[x])).items():
                reach = rd[x] & self.mask.get(v, 0)
                for i in bits(reach):
                    users[i].append((x, ids))
                    if reach != 1 << i:
                        merged.add(i)
        bit = _Locations(self.own)
        value = [bit.mask(*self._value(d)) if _assigns(d) else (0, 0) for d in self.defs]
        dirty, checks = self._dirty(rd, value, bit), self._checks()

        def reason(i):
            got = users[i]
            if not got:
                return "unused"
            if all(x in checks for x, _ in got):
                return "check-only"
            if any(
                dirty[x] >> i & 1 or any(self._clash(x, u, value[i], bit) for u in ids)
                for x, ids in got
            ):
                return "clobbered"
            if i in merged:
                return "merged"
            return "multi-use" if sum(len(ids) for _, ids in got) > 1 else "single-use"

        return [(d, reason(i)) for i, d in enumerate(self.defs) if _assigns(d)]

    def _value(self, d):
        """``(reads, writes)`` of definition ``d``'s value, its own store aside."""
        n = d.node
        if isinstance(n, c_ast.Decl):
            n = n.init
        elif isinstance(n, c_ast.Assignment) and n.op == "=":
            n = n.rvalue
        r, w = footprint(self.unit, n, self.calls)
        return r, w - {d.var}

    def _dirty(self, rd, value, bit):
        """Each vertex's definitions (bits over :attr:`defs`) reaching it along a path from
        which something may change what their values read."""
        n, cfg = len(self.cfg.nodes), self.cfg
        gen, kill = [0] * n, [0] * n
        for i, d in enumerate(self.defs):
            if d.must:
                kill[d.vertex] |= self.mask[d.var]
            if value[i][0] >> bit.index(d.var) & 1:
                gen[d.vertex] |= 1 << i
        for x in cfg.order:
            node = self._evaluated(cfg.nodes[x])
            if node is None:
                continue
            r, w = bit.mask(*footprint(self.unit, node, self.calls))
            for i in bits(rd[x] & ~kill[x]):
                if bit.conflict(value[i], (r, w)):
                    gen[x] |= 1 << i
        return solve(cfg, gen, [k & ~g for k, g in zip(kill, gen)])[0]

    def _clash(self, x, use, value, bit):
        """Whether vertex ``x`` may change what ``value`` reads before it reads ``use`` (an
        ``ID``), or reads what it writes: ``use``'s ancestors act after it."""
        node = self._evaluated(self.cfg.nodes[x])
        path = _ancestors(node, use)
        return bit.conflict(value, bit.mask(*footprint(self.unit, node, self.calls, path)))

    def _checks(self):
        """The condition vertices of ``if`` statements a branch of which surely ends in a
        call that never returns: every path from it reaches a vertex without successors."""
        cfg = self.cfg
        conds = {id(n.cond) for n in q.walk(self.fd.body) if isinstance(n, c_ast.If)}
        dead, more = set(), True
        while more:
            more = False
            for x in reversed(cfg.order):
                if x not in dead and x != cfg.exit and all(s in dead for s in cfg.succ[x]):
                    dead.add(x)
                    more = True
        return {
            x
            for x in cfg.order
            if id(cfg.nodes[x]) in conds and any(s in dead for s in cfg.succ[x])
        }


class _Locations:
    """Bits for locations, each overlapping those of :attr:`cover`: :data:`MEMORY` every
    variable not owned and element, an array variable its elements."""

    def __init__(self, own):
        self.own, self.bit, self.cover = own, {MEMORY: 0}, [0]

    def index(self, loc):
        """The bit of location ``loc``."""
        if loc not in self.bit:
            b = self.bit[loc] = len(self.bit)
            self.cover.append(0)
            if loc not in self.own and loc != DEVICE:
                self.cover[0] |= 1 << b
                self.cover[b] |= 1
            if isinstance(loc, tuple):
                whole = self.index(loc[0])
                self.cover[whole] |= 1 << b
                self.cover[b] |= 1 << whole
        return self.bit[loc]

    def mask(self, *sets):
        """Each set of locations as a mask."""
        return tuple(sum(1 << self.index(loc) for loc in locs) for locs in sets)

    def overlap(self, a, b):
        """Whether location masks ``a`` and ``b`` may name one location."""
        for i in bits(a):
            a |= self.cover[i]
        return bool(a & b)

    def conflict(self, a, b):
        """Whether evaluations of footprints ``a`` and ``b`` (``(reads, writes)`` masks) may
        not be reordered: one writes what the other reads or writes."""
        return self.overlap(a[0], b[1]) or self.overlap(a[1], b[0] | b[1])


def _assigns(d):
    """Whether definition ``d`` assigns a value: not a parameter's, nor a bare declaration."""
    return d.node is not None and not (isinstance(d.node, c_ast.Decl) and d.node.init is None)


def _ancestors(n, target):
    """``id`` of each node from ``n`` down to ``target``, ``target`` included."""
    todo = [(n, (id(n),))]
    while todo:
        x, path = todo.pop()
        if x is target:
            return frozenset(path)
        todo += [(k, (*path, id(k))) for k in q.kids(x)]
    return NOTHING


def _params(fd):
    """The parameter declarations of function definition ``fd``."""
    args = fd.decl.type.args
    return [p for p in (args.params if args else ()) if isinstance(p, c_ast.Decl)]


def bare(canon):
    """A canonical type spelling without ``const`` and ``volatile``."""
    for qual in ("const ", "volatile "):
        canon = canon.replace(qual, "")
    return canon
