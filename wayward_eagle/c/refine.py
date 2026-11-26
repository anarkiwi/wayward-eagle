"""Readability rewrites of statement lists that keep what they do and never raise their
cognitive complexity but to take effects out of loop conditions: common arm statements moved
out, exit tests as loop conditions, counted loops as ``for``, no trailing ``return;`` or
``continue;`` (docs/rewrites.md)."""

from __future__ import annotations

from typing import NamedTuple

from pycparser import c_ast

from . import query as q
from .listflow import Graph, true
from .ints import MODELS, Int, constant, promoted, type_of, value
from .stmt import (
    arms,
    block,
    braced,
    children,
    cost,
    dropped,
    ends_in_jump,
    iff,
    is_fallthrough,
    labels,
    names_in,
    negate,
    pure,
    rebuild,
    swap,
    targets,
    walk,
    writes,
)

INT = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["int"]))
ORDERED = ("<", ">", "<=", ">=", "==", "!=")


def jumps_out(items, kind):
    """Whether ``items`` hold a ``kind`` (``c_ast.Break`` or ``c_ast.Continue``) leaving the
    loop or ``switch`` around them."""
    for st in items:
        if isinstance(st, kind):
            return True
        nested = isinstance(st, q.LOOPS) or (isinstance(st, c_ast.Switch) and kind is c_ast.Break)
        if not nested and any(jumps_out(sub, kind) for sub in children(st)):
            return True
    return False


def _leaves(items):
    """Whether control may leave ``items`` other than by their end."""
    if any(isinstance(st, (c_ast.Return, c_ast.Goto)) for st in walk(items)):
        return True
    return jumps_out(items, c_ast.Break) or jumps_out(items, c_ast.Continue)


def exit_test(st):
    """The condition of ``if (c) break;``, else None."""
    if isinstance(st, c_ast.If) and not block(st.iffalse):
        then = block(st.iftrue)
        if len(then) == 1 and isinstance(then[0], c_ast.Break):
            return st.cond
    return None


def stepped(st):
    """Whether loop ``st`` is a ``for`` with an init or a step."""
    return isinstance(st, c_ast.For) and (st.init is not None or st.next is not None)


def unconditional(st):
    """Whether loop ``st`` tests nothing: no condition, or a nonzero literal."""
    return st.cond is None or true(st.cond)


def infinite(st):
    """Whether loop ``st`` has no condition, init or step: ``for (;;)``, ``while (1)``."""
    return not stepped(st) and unconditional(st)


def effectful(e):
    """Whether expression ``e`` stores or holds a comma expression."""
    return bool(q.writes(e) or q.commas(e))


def leaving(cond):
    """Statements that evaluate loop condition ``cond`` and break when it is false: a comma
    expression's operands in turn, ``a && b`` as ``a`` then ``b``."""
    if isinstance(cond, c_ast.ExprList):
        return [e for e in cond.exprs[:-1] if not pure(e)] + leaving(cond.exprs[-1])
    if isinstance(cond, c_ast.BinaryOp) and cond.op == "&&":
        return leaving(cond.left) + leaving(cond.right)
    return [] if true(cond) else [iff(negate(cond), [c_ast.Break()])]


def shaped(st):
    """Loop ``st`` with a condition that stores or holds a comma as ``for (;;)`` evaluating it
    in statements (:func:`leaving`; a ``do`` loop's only with no ``continue``); a loop with no
    condition whose body starts with ``if (c) break;`` testing ``!c``, or (with no init, step
    or ``continue``) whose body ends in it as ``do ... while (!c)``."""
    body = block(st.stmt)
    if st.cond is not None and effectful(st.cond):
        if isinstance(st, c_ast.DoWhile):
            if not jumps_out(body, c_ast.Continue):
                st = c_ast.For(None, None, None, braced(body + leaving(st.cond)), st.coord)
        else:
            init, step = (st.init, st.next) if isinstance(st, c_ast.For) else (None, None)
            st = c_ast.For(init, None, step, braced(leaving(st.cond) + body), st.coord)
    body = block(st.stmt)
    if not body or not unconditional(st):
        return st
    first, last = exit_test(body[0]), exit_test(body[-1])
    if first is not None:
        if stepped(st):
            return c_ast.For(st.init, negate(first), st.next, braced(body[1:]), st.coord)
        return c_ast.While(negate(first), braced(body[1:]), st.coord)
    if last is not None and infinite(st) and not jumps_out(body, c_ast.Continue):
        return c_ast.DoWhile(negate(last), braced(body[:-1]), st.coord)
    return st


def sequenced(st):
    """``[loop, *T]`` of infinite loop ``st`` whose body ends ``if (c) { S; continue; } T``
    with ``T`` always leaving it (by ``break`` or ``return``, no ``continue``) and no other
    ``break`` before ``T``: the loop ``{ A; if (!c) break; S }``, then ``T`` falling out
    where it broke (:func:`_fallen`); else None."""
    body = block(st.stmt)
    if not infinite(st) or labels([st]) or targets([st]):
        return None
    k = next((k for k in range(len(body) - 1, -1, -1) if _continued(body[k])), None)
    if k is None or not body[k + 1 :]:
        return None
    then, tail = block(body[k].iftrue)[:-1], body[k + 1 :]
    if jumps_out(body[:k] + then, c_ast.Break) or jumps_out(tail, c_ast.Continue):
        return None
    out = _fallen(tail) if ends_in_jump(tail) else None
    if out is None:
        return None
    test = iff(negate(body[k].cond), [c_ast.Break()])
    return [c_ast.For(None, None, None, braced(body[:k] + [test] + then), st.coord), *out]


def _continued(st):
    """Whether ``st`` is ``if (c) { ...; continue; }``."""
    then = block(st.iftrue) if isinstance(st, c_ast.If) else []
    return bool(then) and isinstance(then[-1], c_ast.Continue) and not block(st.iffalse)


def _fallen(items):
    """``items``, each path ending in a jump, with their loop's ``break`` falling off their
    end instead: ending them or an arm of an ``if`` ending them; an ``if`` before the end
    with one arm breaking and the other going on to a ``return`` tests for the other arm
    first, holding the rest; else None."""
    out = []
    for i, st in enumerate(items):
        if isinstance(st, c_ast.Break):
            return out
        if not jumps_out([st], c_ast.Break):
            out.append(st)
            continue
        if not isinstance(st, c_ast.If):
            return None
        arms_ = children(st)
        if i == len(items) - 1:
            got = [_fallen(a) if ends_in_jump(a) else a for a in arms_]
            return None if None in got else out + [rebuild(st, got)]
        rest = items[i + 1 :]
        broke = [jumps_out(a, c_ast.Break) for a in arms_]
        a, b = (arms_[0], arms_[1]) if broke[0] else (arms_[1], arms_[0])
        if all(broke) or not ends_in_jump(a) or jumps_out(b + rest, c_ast.Break):
            return None
        cond = negate(st.cond) if broke[0] else st.cond
        fallen = _fallen(a)
        return None if fallen is None else out + [iff(cond, b + rest), *fallen]
    return out


def step_of(e, v):
    """Whether expression ``e`` stores to variable ``v`` alone (``v = e``, ``v += e``, ``++v``)."""
    var = q.variable(q.stored(e))
    return var is not None and var.name == v and q.writes(e) == {v}


def _init(before, v, own):
    """The index in ``before`` of ``v = e;``, the last statement mentioning ``v``, when it
    may move past those after it: none a label or leaving, and, when there are any, ``e``
    reads only variables of ``own`` that they do not store."""
    for j in range(len(before) - 1, -1, -1):
        st, rest = before[j], before[j + 1 :]
        if v not in names_in([st]):
            continue
        if not (isinstance(st, c_ast.Assignment) and st.op == "=" and step_of(st, v)):
            return None
        read = q.reads(st.rvalue)
        if v in read or labels(rest) or _leaves(rest):
            return None
        if rest and not (pure(st.rvalue) and read <= own and not read & writes(rest)):
            return None
        return j
    return None


def _spots(items):
    """``(list, index, statement)`` of the statements in ``items`` outside nested loops."""
    for i, st in enumerate(items):
        yield items, i, st
        if not isinstance(st, q.LOOPS):
            for sub in children(st):
                yield from _spots(sub)


def _updates(items, v):
    """``[(list, index)]`` of the statement ending each path through loop body ``items``
    that goes on to the next iteration (past its end or by ``continue``), each storing ``v``
    alone (:func:`step_of`), or None when some such path ends otherwise."""
    out = []
    for lst, i, st in _spots(items):
        if isinstance(st, c_ast.Continue):
            if i == 0 or not step_of(lst[i - 1], v):
                return None
            out.append((lst, i - 1))
    ends = _ends(items, v)
    return None if ends is None else out + ends


def _ends(items, v):
    if not items:
        return None
    last = items[-1]
    if step_of(last, v):
        return [(items, len(items) - 1)]
    if not isinstance(last, c_ast.If):
        return None
    out = []
    for arm in children(last):
        if not ends_in_jump(arm):
            got = _ends(arm, v)
            if got is None:
                return None
            out += got
    return out


def counted(st, before, own):
    """``(for loop, index of its init in before)`` of ``while (c)`` loop ``st`` by dewolf's
    rules for a variable ``v`` that ``c`` reads and does not store: ``before`` sets it last
    (:func:`_init`), one store to ``v`` alone ends every path of the body that continues,
    and the body stores ``v`` nowhere else; else None."""
    whilst = isinstance(st, c_ast.While) or (
        isinstance(st, c_ast.For) and st.init is None and st.next is None
    )
    if not whilst or st.cond is None or true(st.cond) or labels([st]) or targets([st]):
        return None
    body = block(st.stmt)
    for v in sorted(q.reads(st.cond) - q.writes(st.cond)):
        spots = _updates(body, v)
        if not spots or len({q.key(lst[i]) for lst, i in spots}) != 1:
            continue
        rest = dropped(body, {(id(lst), i) for lst, i in spots})
        j = _init(before, v, own)
        if v in writes(rest) or j is None:
            continue
        lst, i = spots[0]
        return c_ast.For(before[j], st.cond, lst[i], braced(rest), st.coord), j
    return None


def _chain(st):
    """``(if statements, arms)`` of an ``if``/``else if`` chain ending in ``else``, or
    ``(None, arms)`` of a ``switch`` with a ``default``; else None."""
    if isinstance(st, c_ast.Switch):
        dflt = any(isinstance(x, c_ast.Default) for x in block(st.stmt))
        return (None, [list(b) for _, b in arms(st)]) if dflt else None
    ifs = []
    while True:
        ifs.append(st)
        els = block(st.iffalse)
        if len(els) == 1 and isinstance(els[0], c_ast.If):
            st = els[0]
            continue
        return (ifs, [list(block(x.iftrue)) for x in ifs] + [list(els)]) if els else None


def _unchain(st, ifs, lists):
    """``st`` (of which :func:`_chain` gave ``ifs``) with arms ``lists``; a lone ``if`` with
    an empty first arm testing the other way."""
    if ifs is None:
        return rebuild(st, lists)
    els = lists[-1]
    for x, then in zip(reversed(ifs), reversed(lists[:-1])):
        els = [c_ast.If(x.cond, braced(then), braced(els), x.coord)]
    top = els[0]
    if len(ifs) == 1 and not block(top.iftrue):
        return iff(negate(top.cond), block(top.iffalse))
    return top


def _memory(e, own):
    """Whether expression ``e`` reads anything but variables of ``own``."""
    if q.reads(e) - own:
        return True
    return any(
        isinstance(n, (c_ast.ArrayRef, c_ast.StructRef, c_ast.FuncCall))
        or (isinstance(n, c_ast.UnaryOp) and n.op == "*")
        for n in q.walk(e)
    )


def _hoistable(st, conds, own, switch):
    """Whether ``st`` may run before conditions ``conds``: they have no effect, ``st`` stores
    no variable they read, and when they read more than the variables ``own``, ``st``
    stores only those and calls nothing."""
    if isinstance(st, (c_ast.Label, c_ast.EmptyStatement)) or is_fallthrough(st) or labels([st]):
        return False
    if switch and (isinstance(st, c_ast.Break) or jumps_out([st], c_ast.Break)):
        return False
    if any(effectful(c) or q.calls(c) for c in conds):
        return False
    if writes([st]) & set().union(*map(q.reads, conds)):
        return False
    if any(_memory(c, own) for c in conds):
        nodes = list(q.walk(st))
        stores = [q.stored(n) for n in nodes if q.stored(n) is not None]
        calls = any(isinstance(n, c_ast.FuncCall) for n in nodes)
        return not calls and all(isinstance(s, c_ast.ID) and s.name in own for s in stores)
    return True


def common(st, own):
    """``(lead, st, tail)``: the statements leading every arm of ``if``/``else`` chain or
    ``switch`` ``st`` that may run before its conditions (:func:`_hoistable`) moved before
    it, and those ending every arm (but a ``switch`` arm's ``break``) moved after it."""
    got = _chain(st)
    if got is None:
        return [], st, []
    ifs, lists = got
    conds = [st.cond] if ifs is None else [x.cond for x in ifs]
    ends = [bool(a) and isinstance(a[-1], c_ast.Break) and ifs is None for a in lists]
    cores = [a[:-1] if e else a for a, e in zip(lists, ends)]
    sink = ifs is not None or not any(
        jumps_out(a, c_ast.Break) or (a and is_fallthrough(a[-1])) for a in cores
    )
    lead, tail = [], []
    while all(cores) and len({tuple(map(q.key, a)) for a in cores}) > 1:
        first = cores[0][0]
        if len({q.key(a[0]) for a in cores}) == 1 and _hoistable(first, conds, own, not ifs):
            lead.append(first)
            cores = [a[1:] for a in cores]
        elif sink and len({q.key(a[-1]) for a in cores}) == 1:
            tail.insert(0, cores[0][-1])
            cores = [a[:-1] for a in cores]
        else:
            break
    if not lead and not tail:
        return [], st, []
    lists = [a + [c_ast.Break()] if e else a for a, e in zip(cores, ends)]
    return lead, _unchain(st, ifs, lists), tail


def refined(items, own):
    """``items`` with each nested list refined, statements common to every arm moved out
    (:func:`common`), each loop sequenced where that costs less (:func:`sequenced`), shaped
    (:func:`shaped`) and counted (:func:`counted`); ``own`` the function's variables."""
    out = []
    for st in items:
        if children(st):
            st = rebuild(st, [refined(sub, own) for sub in children(st)])
        tail = []
        if isinstance(st, (c_ast.If, c_ast.Switch)):
            lead, st, tail = common(st, own)
            out += lead
        if isinstance(st, q.LOOPS):
            seq = sequenced(st)
            if seq is not None and cost(seq) < cost([st]):
                st, tail = seq[0], seq[1:] + tail
            st = shaped(st)
            got = counted(st, out, own)
            if got is not None:
                st, j = got
                del out[j]
        out.append(st)
        out += tail
    return out


def _loops(items):
    """``(list, index, loop)`` of every loop in ``items``."""
    for i, st in enumerate(items):
        if isinstance(st, q.LOOPS):
            yield items, i, st
        for sub in children(st):
            yield from _loops(sub)


def _steps(exprs):
    """Whether each of ``exprs`` stores one variable a value of variables and constants."""
    return all(
        q.variable(q.stored(e)) is not None
        and len(q.writes(e)) == 1
        and pure(e.rvalue if isinstance(e, c_ast.Assignment) else e.expr)
        for e in exprs
    )


def bottom(st):
    """``(B, x, S)`` of loop ``st`` running ``B``, leaving when ``x`` holds, then steps ``S``
    (:func:`_steps`): ``for (;;) { B; if (x) break; S }`` with no ``continue`` in ``B``, or
    ``do B while (!x && (S, 1))``; else None."""
    body = block(st.stmt)
    if infinite(st):
        for k in range(len(body) - 1, -1, -1):
            x = exit_test(body[k])
            if x is not None:
                rest, steps = body[:k], body[k + 1 :]
                ok = steps and _steps(steps) and not jumps_out(rest, c_ast.Continue)
                return (rest, x, steps) if ok else None
            if not isinstance(body[k], (c_ast.Assignment, c_ast.UnaryOp)):
                return None
        return None
    c = st.cond
    if not (isinstance(st, c_ast.DoWhile) and isinstance(c, c_ast.BinaryOp) and c.op == "&&"):
        return None
    tail = c.right
    if isinstance(tail, c_ast.ExprList) and true(tail.exprs[-1]) and _steps(tail.exprs[:-1]):
        return (body, negate(c.left), list(tail.exprs[:-1])) if tail.exprs[:-1] else None
    return None


class Count(NamedTuple):
    """A counted loop: variable ``v`` set to ``init``, leaving when ``exit`` holds, else
    stored by ``step``."""

    v: str
    init: c_ast.Node
    step: c_ast.Node
    exit: c_ast.Node

    def stepped(self, x, t, bits):
        """The value of ``v`` (``x``, of type ``t``) after ``step``, None when undefined."""
        step, v = self.step, c_ast.ID(self.v)
        if isinstance(step, c_ast.UnaryOp):
            e = c_ast.BinaryOp(step.op[-1], v, c_ast.Constant("int", "1"))
        else:
            e = step.rvalue if step.op == "=" else c_ast.BinaryOp(step.op[:-1], v, step.rvalue)
        got = value(e, {self.v: (x, t)}, bits)
        return None if got is None else t.wrap(got[0])

    def sequence(self, t, bits, limit=None):
        """``(values, after)``: the values ``v`` (type ``t``) holds on each run of the body
        until ``exit`` holds within ``limit`` runs (every value of ``t``, of up to 16 bits),
        and its value one step more; None when undefined or endless."""
        start = value(self.init, {}, bits)
        if t is None or start is None or (limit is None and t.bits > 16):
            return None
        x, seq = t.wrap(start[0]), []
        for _ in range(1 << t.bits if limit is None else limit):
            seq.append(x)
            test = value(self.exit, {self.v: (x, t)}, bits)
            x = self.stepped(x, t, bits)
            if test is None or x is None:
                return None
            if test[0]:
                return seq, x
        return None

    def head(self, seq, end, types):
        """The most readable condition true at each value of ``seq`` and false at ``end``
        (``types`` one per model): an ordered comparison of ``v``, or of an operand of a
        comparison in ``exit``, when its values rise or fall without wrapping, else ``!=``."""
        sides = [c_ast.ID(self.v)] + [
            s
            for n in q.walk(self.exit)
            if isinstance(n, c_ast.BinaryOp) and n.op in ORDERED
            for s in (n.left, n.right)
            if self.v in q.reads(s) and not isinstance(s, c_ast.ID)
        ]
        consts, found, want = _consts(self.exit), [], [1] * len(seq) + [0]
        for rank, f in enumerate(sides):
            vals = [self.values(f, x, types) for x in seq + [end]]
            for op, k in _candidates(vals) if None not in vals else ():
                cond = c_ast.BinaryOp(op, f, constant(k))
                if [self.values(cond, x, types) for x in seq + [end]] == want:
                    found.append(((k not in consts, op == "!=", rank > 0, len(op) > 1), cond))
        return min(found, key=lambda x: x[0])[1] if found else None

    def values(self, e, x, types):
        """The value of ``e`` with ``v`` at ``x`` (of ``types``, one per model), None unless
        defined and the same in every model."""
        got = [value(e, {self.v: (t.wrap(x), t)}, b) for b, t in zip(MODELS, types)]
        if None in got or len({g[0] for g in got}) != 1:
            return None
        return got[0][0]


def _candidates(vals):
    """``[(operator, constant)]`` of the comparisons true of each of ``vals`` but the last
    and false of it that a reader takes for a bound: ``<``/``<=`` when they rise, ``>``/``>=``
    when they fall, and ``!=`` when the last is new."""
    *got, last = vals
    out = [("!=", last)] if last not in got else []
    pairs = list(zip(vals, vals[1:]))
    if all(a < b for a, b in pairs):
        out += [("<", last), ("<=", got[-1])]
    if all(a > b for a, b in pairs):
        out += [(">", last), (">=", got[-1])]
    return out


def _consts(e):
    """The values of the integer literals in ``e``, negated under unary minus."""
    out = set()
    for n in q.walk(e):
        neg = isinstance(n, c_ast.UnaryOp) and n.op == "-" and isinstance(n.expr, c_ast.Constant)
        got = value(n.expr if neg else n, {}, 32) if neg or isinstance(n, c_ast.Constant) else None
        if got is not None:
            out.add(-got[0] if neg else got[0])
    return out


class Rotation:
    """Loops whose exit test precedes the steps ending their body, as ``for`` loops testing
    at the head (:meth:`rotated`); ``types`` the variables' type trees, each widened to
    ``int`` when only that gives a head test and ``widen``; ``stops`` noreturn calls."""

    def __init__(self, types, stops=None, widen=False):
        self.types, self.stops, self.widen = types, stops, widen

    def __call__(self, items):
        """``items`` with every loop that rotates rotated."""
        failed = {}
        while True:
            graph = Graph(items, self.stops)
            for lst, i, st in _loops(items):
                if id(st) not in failed:
                    got = self.rotated(items, graph, lst, i)
                    if got is not None:
                        items = swap(items, lst, got)
                        break
                    failed[id(st)] = st
            else:
                return items

    def rotated(self, items, graph, lst, i):
        """List ``lst`` (in ``items``, of control flow ``graph``) with its loop ``i`` as a
        ``for`` (:meth:`_count`) when the steps store no other variable live after it, and
        ``v`` is dead after it or no ``break`` leaves before the exit test; else None."""
        body, steps, count, j = self._count(lst, i) or (None,) * 4
        if body is None:
            return None
        after = graph.live_in(graph.after[(id(lst), i)])
        live = count.v in after
        if (set().union(*map(q.writes, steps)) - {count.v}) & after or (
            live and jumps_out(body, c_ast.Break)
        ):
            return None
        rest = lst[:j] + lst[j + 1 : i] + lst[i + 1 :]
        plan = self._plan(count, not live and count.v not in names_in(swap(items, lst, rest)))
        if plan is None:
            return None
        loop = c_ast.For(lst[j], plan[0], _comma(steps), braced(body), lst[i].coord)
        fix = [c_ast.Assignment("=", c_ast.ID(count.v), constant(plan[1]))] if live else []
        return lst[:j] + lst[j + 1 : i] + [loop, *fix] + lst[i + 1 :]

    def _count(self, lst, i):
        """``(B, S, Count, index of the init)`` of loop ``i`` of ``lst``, a :func:`bottom`
        shape whose exit test reads one variable ``v``, set to a value before it
        (:func:`_init`), stored by one step of ``v`` alone and nowhere in ``B``; or None."""
        got = bottom(lst[i])
        if got is None or labels([lst[i]]) or targets([lst[i]]):
            return None
        body, exit_, steps = got
        read = sorted(q.reads(exit_))
        v = read[0] if len(read) == 1 else None
        mine = [s for s in steps if v in q.writes(s)]
        j = _init(lst[:i], v, set(self.types)) if v else None
        if j is None or len(mine) != 1 or q.reads(mine[0]) - {v} or v in writes(body):
            return None
        return body, steps, Count(v, lst[j].rvalue, mine[0], exit_), j

    def _plan(self, count, alone):
        """``(head condition, last value)`` of ``count``, its values the same in each model
        (as many as with a 16-bit ``int``), or None; its variable widened to ``int`` when
        ``alone`` (mentioned by nothing else) and only that gives a condition."""
        types = [type_of(self.types.get(count.v), b) for b in MODELS]
        first = count.sequence(types[0], MODELS[0])
        if first is None or count.sequence(types[1], MODELS[1], len(first[0])) != first:
            return None
        seq, end = first
        cond = count.head(seq, end, types)
        wide = [Int(b, True) for b in MODELS]
        if cond is None and self.widen and alone and all(map(_widens, types, MODELS)):
            runs = [count.sequence(w, b, len(seq)) for b, w in zip(MODELS, wide)]
            if None not in runs and all(r[0] == seq for r in runs):
                cond = count.head(seq, runs[0][1], wide)
                if cond is not None:
                    self.types[count.v] = INT
        return None if cond is None else (cond, seq[-1])


def _comma(exprs):
    """One expression evaluating ``exprs`` in turn."""
    return exprs[0] if len(exprs) == 1 else c_ast.ExprList(list(exprs))


def _widens(t, bits):
    """Whether ``int`` is the promoted type of ``t`` with an ``int`` of ``bits``."""
    return t is not None and promoted(t, bits) == Int(bits, True)


def trimmed(items, ret):
    """``items`` without a ``return;`` ending them when ``ret`` (a function returning
    nothing), and no loop body ending in ``continue;``; an ``if`` ending them trimmed."""
    out = []
    for st in items:
        if children(st):
            loop = isinstance(st, q.LOOPS)
            subs = [trimmed(b, False) for b in children(st)]
            st = rebuild(st, [_trim(b, c_ast.Continue) for b in subs] if loop else subs)
        out.append(st)
    return _trim(out, c_ast.Return) if ret else out


def _trim(items, kind):
    """``items`` without the ``kind`` jump (with no value) ending them or the arms of an
    ``if`` ending them; such an ``if`` left with no arm and a pure condition dropped."""
    while items:
        last = items[-1]
        if isinstance(last, kind) and getattr(last, "expr", None) is None:
            items = items[:-1]
        elif isinstance(last, c_ast.If):
            then, els = (_trim(b, kind) for b in children(last))
            if then or els or not pure(last.cond):
                return items[:-1] + [rebuild(last, [then, els])]
            items = items[:-1]
        else:
            return items
    return items
