"""A C function body as nested statement lists of ``c_ast`` nodes, and what they read, write
and jump to: the form :mod:`wayward_eagle.c.reduce` rewrites. Every nested body is a
``Compound`` whose ``block_items`` is the list, labels hold an empty statement, and a
``switch`` body is its arms, each a chain of labels, the last holding one ``Compound``."""

from __future__ import annotations

import weakref

from pycparser import c_ast

from . import query as q

LOOPS = q.LOOPS
LABELS = (c_ast.Case, c_ast.Default)
COMPARISONS = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}


def block(n):
    """The statement list ``n`` holds: a ``Compound``'s own list, else a new list of it."""
    if n is None:
        return []
    if isinstance(n, c_ast.Compound):
        if n.block_items is None:
            n.block_items = []
        return n.block_items
    return [n]


def braced(items):
    """A ``Compound`` holding statement list ``items`` itself."""
    return c_ast.Compound(items)


def is_fallthrough(st):
    """Whether ``st`` is ``__attribute__((fallthrough));``."""
    return isinstance(st, c_ast.AttributedStatement) and any(
        a.name.strip("_") == "fallthrough" for s in st.attributes for a in s.attributes
    )


def is_jump(st):
    """Whether ``st`` leaves the statement list it is in: a ``goto``, ``break``,
    ``continue``, ``return`` or a fall into the next case."""
    return isinstance(st, q.JUMPS) or is_fallthrough(st)


def is_goto(st, label=None):
    """Whether ``st`` is a ``goto`` (to ``label`` when given)."""
    return isinstance(st, c_ast.Goto) and (label is None or st.name == label)


def arms(sw):
    """``[(labels, items)]`` of a ``switch``: each arm's chain of labels and the statement
    list its last label holds."""
    out, chain = [], []
    for st in block(sw.stmt):
        chain.append(st)
        if st.stmts:
            out.append((chain, block(st.stmts[0])))
            chain = []
    if chain:
        chain[-1].stmts = [braced([])]
        out.append((chain, block(chain[-1].stmts[0])))
    return out


def children(st):
    """The statement lists nested in ``st``."""
    if isinstance(st, c_ast.If):
        return [block(st.iftrue), block(st.iffalse)]
    if isinstance(st, LOOPS):
        return [block(st.stmt)]
    if isinstance(st, c_ast.Switch):
        return [b for _, b in arms(st)]
    return []


def rebuild(st, lists):
    """A copy of ``st`` holding statement lists ``lists`` (as :func:`children` lists them)."""
    if isinstance(st, c_ast.If):
        return c_ast.If(st.cond, braced(lists[0]), braced(lists[1]), st.coord)
    if isinstance(st, c_ast.While):
        return c_ast.While(st.cond, braced(lists[0]), st.coord)
    if isinstance(st, c_ast.DoWhile):
        return c_ast.DoWhile(st.cond, braced(lists[0]), st.coord)
    if isinstance(st, c_ast.For):
        return c_ast.For(st.init, st.cond, st.next, braced(lists[0]), st.coord)
    if isinstance(st, c_ast.Switch):
        cases = []
        for (chain, _), items in zip(arms(st), lists):
            *first, last = chain
            cases += [_label(x, []) for x in first] + [_label(last, [braced(items)])]
        return c_ast.Switch(st.cond, braced(cases), st.coord)
    return st


def _label(label, stmts):
    if isinstance(label, c_ast.Case):
        return c_ast.Case(label.expr, stmts, label.coord)
    return c_ast.Default(stmts, label.coord)


def mapped(items, f):
    """``items`` with each nested statement list replaced by ``f`` of it."""
    return [rebuild(st, [f(sub) for sub in children(st)]) if children(st) else st for st in items]


def heads(st):
    """The expressions ``st`` evaluates outside its nested statement lists."""
    if isinstance(st, (c_ast.If, c_ast.While, c_ast.DoWhile, c_ast.Switch)):
        return [st.cond]
    if isinstance(st, c_ast.For):
        return [x for x in (st.init, st.cond, st.next) if x is not None]
    if isinstance(st, (c_ast.Label, c_ast.Goto)) or is_fallthrough(st):
        return []
    return [st]


def _facts(st):
    """``(reads, writes)`` of ``st`` outside its nested statement lists, memoized: nodes are
    never changed once built."""
    got = _FACTS.get(st)
    if got is None:
        r, w = set(), set()
        for e in heads(st):
            r |= q.reads(e)
            w |= q.writes(e)
        got = _FACTS[st] = (frozenset(r), frozenset(w))
    return got


def _tree(st):
    """``(reads, writes)`` of ``st`` with its nested statements, memoized."""
    got = _TREE.get(st)
    if got is None:
        r, w = _facts(st)
        for sub in children(st):
            for x in sub:
                xr, xw = _tree(x)
                r, w = r | xr, w | xw
        got = _TREE[st] = (r, w)
    return got


_FACTS = weakref.WeakKeyDictionary()
_TREE = weakref.WeakKeyDictionary()


def own(st):
    """The variables ``st`` reads or writes outside its nested statement lists."""
    r, w = _facts(st)
    return r | w


def reads(st):
    """The variables ``st`` reads outside its nested statement lists."""
    return _facts(st)[0]


def own_writes(st):
    """The variables ``st`` writes outside its nested statement lists."""
    return _facts(st)[1]


def walk(items):
    """Every statement of ``items``, nested ones included, in order."""
    for st in items:
        yield st
        for sub in children(st):
            yield from walk(sub)


def names_in(items):
    """Every variable ``items`` read or write."""
    out = set()
    for st in items:
        r, w = _tree(st)
        out |= r | w
    return out


def reads_in(items):
    """Every variable ``items`` read, nested statements included."""
    out = set()
    for st in items:
        out |= _tree(st)[0]
    return out


def writes(items):
    """Every variable ``items`` set, in an assignment, increment or decrement anywhere in
    them (a loop's condition, a comma expression) included."""
    out = set()
    for st in items:
        out |= _tree(st)[1]
    return out


def targets(items):
    """The labels the gotos of ``items`` jump to, nested ones included."""
    return {st.name for st in walk(items) if is_goto(st) and isinstance(st.name, str)}


def labels(items):
    """The labels of ``items``, nested ones included."""
    return {st.name for st in walk(items) if isinstance(st, c_ast.Label)}


def order(name):
    """A variable's sort key: its number after the prefix letter."""
    return (len(name), name)


def assigned_first(items, v, stops=None):
    """Whether every read of ``v`` in ``items`` follows an assignment on every path, a path
    ending at a statement ``stops`` holds (a call that never returns)."""
    return _da(items, v, False, stops or _never)[1]


def _never(_st):
    return False


def _leaf(e, v, done):
    """``(done after, ok)`` of evaluating ``e``, a statement with no nested lists."""
    if v in reads(e) and not done:
        return done, False
    return done or v in own_writes(e), True


def _da(  # pylint: disable=too-many-return-statements,too-many-branches,too-many-locals
    items, v, done, stops
):
    """``(definitely assigned after, no read before assignment)`` over ``items``; control
    reaching a label may come from anywhere, so nothing is assigned there."""
    gone = False
    for st in items:
        if isinstance(st, c_ast.Label):
            done, gone = False, False
            continue
        if gone:
            continue
        if isinstance(st, c_ast.If):
            start, ok = _leaf(st.cond, v, done)
            if not ok:
                return done, False
            a, ok = _da(block(st.iftrue), v, start, stops)
            b, ok2 = _da(block(st.iffalse), v, start, stops)
            if not (ok and ok2):
                return done, False
            done = a and b
        elif isinstance(st, LOOPS):
            if isinstance(st, c_ast.For) and st.init is not None:
                done, ok = _leaf(st.init, v, done)
                if not ok:
                    return done, False
            cond = q.reads(st.cond) if st.cond is not None else set()
            if v in cond and not done and not isinstance(st, c_ast.DoWhile):
                return done, False
            set_after, ok = _da(block(st.stmt), v, done, stops)
            late = cond | (q.reads(st.next) if getattr(st, "next", None) is not None else set())
            if not ok or (v in late and not set_after and not isinstance(st, c_ast.While)):
                return done, False
        elif isinstance(st, c_ast.Switch):
            if v in q.reads(st.cond) and not done:
                return done, False
            got = [(chain, _da(b, v, done, stops)) for chain, b in arms(st)]
            if not all(ok for _, (_, ok) in got):
                return done, False
            dflt = any(isinstance(x, c_ast.Default) for chain, _ in got for x in chain)
            done = done or (dflt and all(a for _, (a, _) in got))
        else:
            done, ok = _leaf(st, v, done)
            if not ok:
                return done, False
            if is_jump(st) or stops(st):
                done, gone = True, True
    return done, True


def ends_in_jump(items):
    """Whether control never falls off the end of ``items``."""
    if not items:
        return False
    last = items[-1]
    if is_jump(last):
        return True
    if isinstance(last, c_ast.If):
        els = block(last.iffalse)
        return bool(els) and ends_in_jump(block(last.iftrue)) and ends_in_jump(els)
    return isinstance(last, c_ast.For) and last.cond is None and not _breaks(block(last.stmt))


def _breaks(items):
    """Whether ``items`` break out of their loop."""
    for st in items:
        if isinstance(st, c_ast.Break):
            return True
        if isinstance(st, (c_ast.If, c_ast.Switch)) and any(_breaks(b) for b in children(st)):
            return True
    return False


def cost(items, depth=0):
    """The cognitive complexity of ``items`` at nesting ``depth`` (SonarSource's rules as
    clang-tidy's ``readability-function-cognitive-complexity`` counts them on statements):
    an ``if``, loop or ``switch`` one plus its depth, an ``else if``, ``else`` and ``goto``
    one; a statement's arms one level deeper. Conditions count the same either way."""
    out = 0
    for st in items:
        if is_goto(st):
            out += 1
        elif isinstance(st, c_ast.If):
            out += 1 + depth + _arms(st, depth + 1)
        elif isinstance(st, (*LOOPS, c_ast.Switch)):
            out += 1 + depth + sum(cost(sub, depth + 1) for sub in children(st))
    return out


def _arms(st, depth):
    """The cost of an ``if``'s arms, an ``else`` holding one ``if`` an ``else if``."""
    out, els = cost(block(st.iftrue), depth), block(st.iffalse)
    while len(els) == 1 and isinstance(els[0], c_ast.If):
        out += 1 + cost(block(els[0].iftrue), depth)
        els = block(els[0].iffalse)
    return out + (1 + cost(els, depth) if els else 0)


def negate(e):
    """The condition true exactly when ``e`` is false: ``!`` removed, a comparison (of
    integers or pointers) turned round, else ``!e``."""
    if isinstance(e, c_ast.UnaryOp) and e.op == "!":
        return e.expr
    if isinstance(e, c_ast.BinaryOp) and e.op in COMPARISONS:
        return c_ast.BinaryOp(COMPARISONS[e.op], e.left, e.right, e.coord)
    return c_ast.UnaryOp("!", e, e.coord)


def call(name, args):
    """The call ``name(args...)`` of variables ``args``."""
    return c_ast.FuncCall(
        c_ast.ID(name), c_ast.ExprList([c_ast.ID(a) for a in args]) if args else None
    )


def returning(value=None):
    """``return value;`` (``value`` an expression, an int, a variable's name, or None)."""
    if isinstance(value, int):
        value = c_ast.Constant("int", str(value))
    elif isinstance(value, str):
        value = c_ast.ID(value)
    return c_ast.Return(value)


def iff(cond, then, els=()):
    """``if (cond) { then } else { els }``."""
    return c_ast.If(cond, braced(list(then)), braced(list(els)))


def swap(items, old, new):
    """``items`` with the statement list ``old`` (``items`` itself, or one nested in it, by
    identity) replaced by ``new``."""
    if items is old:
        return new
    return mapped(items, lambda sub: swap(sub, old, new))


#: Expression nodes that compute a value from variables and constants alone.
PURE = (
    c_ast.ID,
    c_ast.Constant,
    c_ast.BinaryOp,
    c_ast.TernaryOp,
    c_ast.Cast,
    c_ast.Typename,
    *q.TYPES,
)


def pure(e):
    """Whether expression ``e`` is computed from variables and constants alone."""
    return all(
        isinstance(n, PURE) or (isinstance(n, c_ast.UnaryOp) and n.op in "-+~!") for n in q.walk(e)
    )


def dropped(items, spots):
    """``items`` without the statements at ``spots`` (``{(id(list), index)}``)."""
    out = []
    for i, st in enumerate(items):
        if (id(items), i) not in spots:
            out.append(
                rebuild(st, [dropped(s, spots) for s in children(st)]) if children(st) else st
            )
    return out


def reheaded(st, exprs):
    """A copy of ``st`` evaluating ``exprs`` in place of its :func:`heads`."""
    if isinstance(st, c_ast.If):
        return c_ast.If(exprs[0], st.iftrue, st.iffalse, st.coord)
    if isinstance(st, (c_ast.While, c_ast.DoWhile, c_ast.Switch)):
        return type(st)(exprs[0], st.stmt, st.coord)
    if isinstance(st, c_ast.For):
        it = iter(exprs)
        init, cond, nxt = (None if x is None else next(it) for x in (st.init, st.cond, st.next))
        return c_ast.For(init, cond, nxt, st.stmt, st.coord)
    return exprs[0] if exprs else st


def remap(items, f):
    """``items`` with each statement ``st`` at ``(list, index)`` replaced by
    ``f(list, index, st)``, its nested lists remapped first."""
    out = []
    for i, st in enumerate(items):
        new = f(items, i, st)
        if children(st):
            new = rebuild(new, [remap(sub, f) for sub in children(st)])
        out.append(new)
    return out
