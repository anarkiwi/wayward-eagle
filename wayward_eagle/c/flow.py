"""The control flow graph of a C function body in the statement-list form of
:mod:`wayward_eagle.c.stmt`, and dataflow over it: reachability, liveness and available
copies. Sets of variables are bit masks over :attr:`Graph.bits`."""

from __future__ import annotations

from pycparser import c_ast

from . import query as q
from .stmt import arms, block, children, dropped, heads, is_fallthrough, pure, reheaded, remap


def _never(_st):
    return False


def killed(e):
    """The variables expression ``e`` stores to on every evaluation: the targets of ``=`` at
    its top level, through comma expressions."""
    if isinstance(e, c_ast.Assignment) and e.op == "=" and isinstance(e.lvalue, c_ast.ID):
        return {e.lvalue.name} | killed(e.rvalue)
    if isinstance(e, c_ast.ExprList):
        return set().union(*map(killed, e.exprs))
    return set()


def copy_of(e):
    """``(v, u)`` when expression ``e`` is the copy ``v = u`` of one variable to another."""
    if (
        isinstance(e, c_ast.Assignment)
        and e.op == "="
        and isinstance(e.lvalue, c_ast.ID)
        and isinstance(e.rvalue, c_ast.ID)
        and e.lvalue.name != e.rvalue.name
    ):
        return e.lvalue.name, e.rvalue.name
    return None


def true(e):
    """Whether condition ``e`` is a nonzero integer literal."""
    if not isinstance(e, c_ast.Constant) or "int" not in e.type:
        return False
    try:
        return int(e.value.rstrip("uUlL"), 0) != 0
    except ValueError:
        return False


class Graph:  # pylint: disable=too-many-instance-attributes
    """The control flow graph of statement list ``items``: a node per expression evaluated
    (``expr``), ``succ`` its successors (none after a call ``stops`` holds), node 0 the exit.
    ``at``, ``heads`` and ``after`` give each statement's entry, head and next nodes by
    ``(id(list), index)``; ``span`` the range of each list's nodes by ``id(list)``."""

    def __init__(self, items, stops=None):
        self.stops = stops or _never
        self.bits, self.succ, self.expr = {}, [], []
        self.use, self.kill, self.store = [], [], []
        self.heads, self.span, self.after, self.at, self.labels = {}, {}, {}, {}, {}
        gotos = []
        self._gotos = gotos
        self._node(None)
        self.entry = self._list(items, 0, None, None, None)
        for n, name in gotos:
            self.succ[n] = [self.labels.get(name, 0)]
        self._live = self._copies = None

    def mask(self, names):
        """The mask of variable ``names``."""
        out = 0
        for v in names:
            out |= 1 << self.bits.setdefault(v, len(self.bits))
        return out

    def names(self, mask):
        """The variables of ``mask``."""
        return {v for v, b in self.bits.items() if mask >> b & 1}

    def _node(self, e, succ=()):
        self.succ.append(list(succ))
        self.expr.append(e)
        none = e is None
        self.use.append(0 if none else self.mask(q.reads(e)))
        self.kill.append(0 if none else self.mask(killed(e)))
        self.store.append(0 if none else self.mask(q.writes(e)))
        return len(self.succ) - 1

    def _list(self, items, nxt, brk, cont, fall):  # pylint: disable=too-many-arguments
        """The entry of ``items`` falling into ``nxt``; ``break``, ``continue`` and a fall
        into the next case going to ``brk``, ``cont`` and ``fall``."""
        lo = len(self.succ)
        for i in range(len(items) - 1, -1, -1):
            self.after[(id(items), i)] = nxt
            nxt = self.at[(id(items), i)] = self._stmt(
                items[i], (id(items), i), nxt, (brk, cont, fall)
            )
        self.span[id(items)] = (lo, len(self.succ))
        return nxt

    def _stmt(self, st, at, nxt, ctx):
        brk, cont, fall = ctx
        if isinstance(st, q.LOOPS):
            return self._loop(st, at, nxt, fall)
        if isinstance(st, c_ast.Label):
            self.labels[st.name] = self._node(None, [nxt])
            return self.labels[st.name]
        n = self._node(heads(st)[0] if heads(st) else None)
        self.heads[at] = [n] if heads(st) else []
        if isinstance(st, c_ast.If):
            self.succ[n] = [self._list(sub, nxt, brk, cont, fall) for sub in children(st)]
        elif isinstance(st, c_ast.Switch):
            after = nxt
            for _, sub in reversed(arms(st)):
                after = self._list(sub, nxt, nxt, cont, after)
                self.succ[n].insert(0, after)
            if not any(isinstance(x, c_ast.Default) for x in block(st.stmt)):
                self.succ[n].append(nxt)
        elif isinstance(st, c_ast.Goto):
            self._gotos.append((n, st.name))
        elif not self.stops(st):
            to = {c_ast.Return: 0, c_ast.Break: brk, c_ast.Continue: cont}.get(type(st), nxt)
            self.succ[n] = [fall if is_fallthrough(st) else to]
        return n

    def _loop(self, st, at, nxt, fall):
        """The entry of loop ``st``: its init, condition (a node of none when it has none),
        body and step."""
        step_e = st.next if isinstance(st, c_ast.For) else None
        init_e = st.init if isinstance(st, c_ast.For) else None
        cond = self._node(st.cond)
        step = self._node(step_e, [cond]) if step_e is not None else cond
        body = self._list(block(st.stmt), step, nxt, step, fall)
        self.succ[cond] = [body] if st.cond is None or true(st.cond) else [body, nxt]
        init = self._node(init_e, [cond]) if init_e is not None else None
        parts = ((init_e, init), (st.cond, cond), (step_e, step))
        self.heads[at] = [n for e, n in parts if e is not None]
        if init is not None:
            return init
        return body if isinstance(st, c_ast.DoWhile) else cond

    def reachable(self):
        """The nodes control reaches from the entry."""
        seen, todo = {self.entry}, [self.entry]
        while todo:
            for s in self.succ[todo.pop()]:
                if s not in seen:
                    seen.add(s)
                    todo.append(s)
        return seen

    def _preds(self):
        preds = [[] for _ in self.succ]
        for n, ss in enumerate(self.succ):
            for s in ss:
                preds[s].append(n)
        return preds

    def live(self):
        """``[mask]`` of the variables live into each node: read before stored on some path
        from it."""
        if self._live is None:
            live, preds = [0] * len(self.succ), self._preds()
            todo = list(range(len(self.succ)))
            while todo:
                n = todo.pop()
                out = 0
                for s in self.succ[n]:
                    out |= live[s]
                got = self.use[n] | (out & ~self.kill[n])
                if got != live[n]:
                    live[n] = got
                    todo += preds[n]
            self._live = live
        return self._live

    def live_out(self, n):
        """The variables live out of node ``n``."""
        out, live = 0, self.live()
        for s in self.succ[n]:
            out |= live[s]
        return self.names(out)

    def live_in(self, n=None):
        """The variables live into node ``n`` (the entry by default)."""
        return self.names(self.live()[self.entry if n is None else n])

    def leaving(self, items):
        """The variables live wherever control goes on leaving nested statement list
        ``items``: past its end, or by a jump out of it."""
        lo, hi = self.span[id(items)]
        live, out = self.live(), 0
        for n in range(lo, hi):
            for s in self.succ[n]:
                if not lo <= s < hi:
                    out |= live[s]
        return self.names(out)

    def copies(self):
        """``[{(v, u)}]`` of the copies ``v = u`` available into each node: on every path to
        it the last store to ``v`` is that copy, and ``u`` is not stored since."""
        if self._copies is None:
            made = [copy_of(e) if e is not None else None for e in self.expr]
            pairs = sorted({c for c in made if c is not None})
            gen = [1 << pairs.index(c) if c else 0 for c in made]
            kill = [0] * len(self.succ)
            for k, (v, u) in enumerate(pairs):
                vu = self.mask((v, u))
                for n, s in enumerate(self.store):
                    if s & vu and not gen[n] >> k & 1:
                        kill[n] |= 1 << k
            ins = self._must(gen, kill, (1 << len(pairs)) - 1)
            self._copies = [{c for k, c in enumerate(pairs) if m >> k & 1} for m in ins]
        return self._copies

    def _must(self, gen, kill, full):
        """``[mask]`` into each node of a forward problem holding on every path from the
        entry: ``gen`` and ``kill`` the masks each node adds and removes."""
        preds = self._preds()
        ins, outs = [full] * len(self.succ), [full] * len(self.succ)
        todo = list(range(len(self.succ)))
        while todo:
            n = todo.pop()
            got = 0 if n == self.entry else full
            for p in preds[n]:
                got &= outs[p]
            ins[n], out = got, gen[n] | (got & ~kill[n])
            if out != outs[n]:
                outs[n] = out
                todo += self.succ[n]
        return ins


def _positions(items):
    """``(list, index, statement)`` of every statement of ``items``, nested ones included."""
    for i, st in enumerate(items):
        yield items, i, st
        for sub in children(st):
            yield from _positions(sub)


def reachable(items, stops=None):
    """``items`` without the statements control never reaches."""
    graph = Graph(items, stops)
    seen = graph.reachable()
    gone = {(id(lst), i) for lst, i, _ in _positions(items) if graph.at[(id(lst), i)] not in seen}
    return dropped(items, gone)


def unstored(items, local, stops=None):
    """``items`` without each store ``v = e;`` to a variable of ``local`` dead after it, of a
    value ``e`` computed from variables and constants alone."""
    while True:
        graph = Graph(items, stops)
        gone = set()
        for lst, i, st in _positions(items):
            if _store(st, local):
                (n,) = graph.heads[(id(lst), i)]
                if st.lvalue.name not in graph.live_out(n):
                    gone.add((id(lst), i))
        if not gone:
            return items
        items = dropped(items, gone)


def _store(st, local):
    return (
        isinstance(st, c_ast.Assignment)
        and st.op == "="
        and isinstance(st.lvalue, c_ast.ID)
        and st.lvalue.name in local
        and pure(st.rvalue)
    )


def propagated(items, types, stops=None):
    """``items`` reading ``u`` for ``v`` wherever a copy ``v = u`` (of one type) is available,
    for each copy whose every use is such a read; the copies are then dead."""
    graph = Graph(items, stops)
    avail = graph.copies()
    same = {
        (v, u)
        for c in avail
        for v, u in c
        if v in types and u in types and q.key(types[v]) == q.key(types[u])
    }
    ok = {p for p in same if all(_covered(graph, n, p, avail) for n in _makers(graph, p))}

    def fix(lst, i, st):
        nodes = graph.heads.get((id(lst), i), [])
        exprs = heads(st)
        new = [_renamed(e, dict(avail[n] & ok)) or e for e, n in zip(exprs, nodes)]
        return reheaded(st, new) if any(a is not b for a, b in zip(new, exprs)) else st

    return remap(items, fix)


def _makers(graph, pair):
    return [n for n, e in enumerate(graph.expr) if e is not None and copy_of(e) == pair]


def _covered(graph, start, pair, avail):
    """Whether every read of ``v`` that copy node ``start`` (``v = u``) reaches has the copy
    available and may read ``u`` instead."""
    v = graph.mask(pair[:1])
    seen, todo = set(), list(graph.succ[start])
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        if graph.use[n] & v and (pair not in avail[n] or not _renamed(graph.expr[n], dict([pair]))):
            return False
        if not graph.kill[n] & v:
            todo += graph.succ[n]
    return True


def _renamed(e, names):
    """``e`` reading ``names[v]`` for each ``v`` (:func:`query.renamed`), ``e`` itself when
    none applies, None when ``e`` stores to a ``v`` but by ``=`` or to its replacement."""
    names = {v: u for v, u in names.items() if v in q.reads(e)}
    if not names:
        return e
    stored = q.writes(e)
    if set(names.values()) & stored:
        return None
    if any(q.variable(q.stored(x)) and q.stored(x).name in names for x in _compound(e)):
        return None
    return q.renamed(e, names)


def _compound(e):
    """The stores in ``e`` that also read their target: ``op=``, increments, decrements."""
    return [x for x in q.walk(e) if q.stored(x) is not None and getattr(x, "op", "=") != "="]
