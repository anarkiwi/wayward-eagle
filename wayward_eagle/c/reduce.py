"""Rewrites of a C function (a ``c_ast.FuncDef``) that lower its cognitive complexity and keep
what it does: sections, tails, early exits, flattened arms and extracted blocks
(docs/rewrites.md), over the statement lists of :mod:`wayward_eagle.c.stmt`."""

from __future__ import annotations

import copy
from itertools import chain, count
from typing import Callable, NamedTuple

from pycparser import c_ast

from . import query as q
from .listflow import Graph, propagated, reachable, unstored
from .refine import Rotation, refined, trimmed
from .stmt import (
    LABELS,
    arms,
    assigned_first,
    block,
    braced,
    call,
    children,
    cost,
    ends_in_jump,
    heads,
    iff,
    is_fallthrough,
    is_goto,
    labels,
    names_in,
    negate,
    order,
    own,
    rebuild,
    reheaded,
    remap,
    returning,
    swap,
    targets,
    writes,
)

#: The cognitive complexity above which nested blocks are extracted (SonarQube's default).
THRESHOLD = 25
#: What a function returning an exit code returns (:class:`Piece`).
STATUS = "status"


def never(_st):
    """No statement is a call that never returns."""
    return False


class Function(NamedTuple):
    """What the rewrites know of a C function: its parameters, its other variables, whether
    it returns nothing, whether a statement is a call that never returns, and the names its
    pieces may not take."""

    params: list
    local: set
    void: bool
    noreturn: Callable = never
    taken: frozenset = frozenset()


def fresh(base, taken):
    """``base``, or ``BASE_K`` for the least ``K`` from 2 when ``base`` is in ``taken``."""
    return next(n for n in chain([base], (f"{base}_{k}" for k in count(2))) if n not in taken)


class Piece(NamedTuple):
    """A function of the rewritten C: its name, parameters, statements and what it returns
    (None nothing, :data:`STATUS` an exit code, else the variable it names)."""

    name: str
    args: list
    items: list
    ret: str | None = None


def sections(items, local, stops=never):
    """``[(label, items)]`` of a function body cut at each top-level label no variable of
    ``local`` is used across, no goto crosses but into it from before, and that no return
    before it (but one after a call ``stops`` holds) skips: each section a function of its
    own (the label ending the section before it, none after the last)."""
    out, start = [], 0
    for i, st in enumerate(items):
        if not isinstance(st, c_ast.Label):
            continue
        before, rest, lab = items[start:i], items[i + 1 :], st.name
        shared = names_in(before) & names_in(rest) & local
        kinds = _kinds(before, At(stops))
        if shared or kinds is None or "return" in kinds:
            continue
        if targets(before) & (labels(rest) - {lab}):
            continue
        if targets(rest) & (labels(before) | {lab}):
            continue
        out.append((lab, before))
        start = i + 1
    return out + [(None, items[start:])]


def tails(items, local, make, stops=None):
    """``(head, [(label, items, params)])`` of a function body: from its last top-level label
    back, each label nothing after it jumps back before starts a tail taking the variables
    of ``local`` live into it; gotos to later tails and the fall into the
    next are those tails' calls (``make(label, params)``)."""
    out, refs = [], {}
    while True:
        i = next(
            (i for i in range(len(items) - 1, -1, -1) if isinstance(items[i], c_ast.Label)), None
        )
        if i is None:
            return items, out
        before, rest, lab = items[:i], items[i + 1 :], items[i].name
        if targets(rest) & (labels(before) | {lab}) or targets(before) & labels(rest):
            return items, out
        body = calling(rest, {t: make(t, ps) for t, ps in refs.items()})
        if out:
            body = [*body, make(out[0][0], refs[out[0][0]])]
        live = names_in(body) & names_in(before) & local
        params = sorted(live & Graph(body, stops).live_in(), key=order)
        refs[lab] = params
        out.insert(0, (lab, rest, params))
        items = before


def reduced(body, name, fn):
    """``([Piece], [section name], status variables)`` of the statements ``body`` of C
    function ``name`` (:class:`Function` ``fn``): a function returning nothing cut into
    sections (:func:`sections`, ``NAME_K``, called in turn), then each section's pieces."""
    parts = sections(body, fn.local, fn.noreturn) if fn.void else [(None, body)]
    pieces, temps, names = [], set(), []
    for k, (lab, items) in enumerate(parts, 1):
        sec = name if len(parts) == 1 else fresh(f"{name}_{k}", fn.taken)
        got, more = _pieces(sec, returning_at(items, lab) if lab else items, fn)
        pieces += got
        temps |= more
        names.append(sec)
    return pieces, names, temps


def _chain(sec, items, fn):
    """``[(function name, parameters, items)]`` of section ``sec``: its first statements and
    its tails (:func:`tails`, ``SECTION_LABEL``) when it returns nothing, each goto to a
    tail and the fall into the next one its call."""
    params = fn.params

    def make(t, ps):
        return call(fresh(f"{sec}_{t}", fn.taken), params + ps)

    first, links = tails(items, fn.local, make, fn.noreturn) if fn.void else (items, [])
    calls = {t: make(t, ps) for t, _, ps in links}
    seq = [(sec, params, first), *((q.callee(calls[t]), params + ps, b) for t, b, ps in links)]
    out = []
    for j, (name, args, body) in enumerate(seq):
        body = calling(body, calls)
        if j < len(links) and not ends_in_jump(body):
            body = [*body, calls[links[j][0]]]
        out.append((name, args, body))
    return out


def _pieces(sec, items, fn):
    """``([Piece], status variables)`` of section ``sec``: its chain (:func:`_chain`), each
    function flattened, ended (:func:`guarded`) when it returns nothing, and its nested
    blocks extracted while above :data:`THRESHOLD` (:func:`extracted`, ``PIECE_xK``)."""
    out, temps = [], set()
    local = fn._replace(local=fn.local | set(fn.params))
    for name, args, body in _chain(sec, items, fn):
        body = guarded(flattened(body)) if fn.void else flattened(body)
        got, more = extracted(name, args, body, local, None if fn.void else STATUS)
        out += got
        temps |= more
    return out, temps


#: The jump each kind of exit from a block takes.
JUMPS = {"return": returning, "break": c_ast.Break, "continue": c_ast.Continue}


class At(NamedTuple):
    """Where a statement is in the block being judged: the loops and ``switch`` statements
    around it inside the block, and which statements are calls that never return."""

    noreturn: Callable
    loops: int = 0
    switches: int = 0

    def into(self, st):
        """Where the statements nested in ``st`` are."""
        return At(
            self.noreturn,
            self.loops + isinstance(st, q.LOOPS),
            self.switches + isinstance(st, c_ast.Switch),
        )


def _is_jump(st):
    return isinstance(st, q.JUMPS) or is_fallthrough(st)


def _kind(st, k, items, at):
    """The kind of exit jump ``st`` (statement ``k`` of ``items``) takes out of the block,
    ``"stay"`` for none, None for a fall into the next case, which no extraction carries."""
    if is_fallthrough(st):
        return None
    if isinstance(st, c_ast.Return):
        return "stay" if k and at.noreturn(items[k - 1]) else "return"
    if isinstance(st, c_ast.Break) and not at.loops + at.switches:
        return "break"
    return "continue" if isinstance(st, c_ast.Continue) and not at.loops else "stay"


def _kinds(items, at):
    """The kinds of exit control leaves ``items`` by other than their end (``return``,
    ``break``, ``continue``), or None when it may fall into the next case."""
    out = set()
    for k, st in enumerate(items):
        if _is_jump(st):
            got = {_kind(st, k, items, at)}
        else:
            got = set()
            for sub in children(st):
                inner = _kinds(sub, at.into(st))
                if inner is None:
                    return None
                got |= inner
        if None in got:
            return None
        out |= got
    return out - {"stay"}


def _recode(items, codes, end, at):
    """``items`` with each exit of a kind in ``codes`` returning its code (``end()`` for
    None), and each other return (one after a call that never returns) ``end()``."""
    out = []
    for k, st in enumerate(items):
        if _is_jump(st):
            kind = _kind(st, k, items, at)
            if kind in codes:
                st = end() if codes[kind] is None else returning(codes[kind])
            elif isinstance(st, c_ast.Return):
                st = end()
        elif children(st):
            st = rebuild(st, [_recode(sub, codes, end, at.into(st)) for sub in children(st)])
        out.append(st)
    return out


def _blocks(items, depth=0, same=frozenset()):
    """``(block, depth, same)`` of each statement list nested in ``items`` at the nesting of
    its statements, ``same`` the kinds of exit that go where falling off its end does
    (``continue`` from a loop body, ``break`` from a case body, an arm ending ``items``)."""
    for k, st in enumerate(items):
        if isinstance(st, q.LOOPS):
            inner = frozenset({"continue"})
        elif isinstance(st, c_ast.Switch):
            inner = frozenset({"break"})
        else:
            inner = same if k == len(items) - 1 else frozenset()
        for sub in children(st):
            yield sub, depth + 1, inner
            yield from _blocks(sub, depth + 1, inner)


def _end(ret):
    """A fresh jump ending a function that returns ``ret`` (:class:`Piece`)."""
    if ret is None:
        return returning()
    return returning(0 if ret == STATUS else ret)


def _site(xname, site, codes):
    """``(call site, status variable)`` of extracted function ``xname`` (its call ``site``)
    returning exit ``codes`` (``{kind: code}``): one test of the call for one exit, else the
    status in ``NAME_k`` and a test per exit."""
    if len(codes) == 1:
        (kind,) = codes
        return [iff(site, [JUMPS[kind]()])], None
    tmp = f"{xname}_k"
    tests = [
        iff(c_ast.BinaryOp("==", c_ast.ID(tmp), c_ast.Constant("int", str(c))), [JUMPS[kind]()])
        for kind, c in codes.items()
    ]
    return [c_ast.Assignment("=", c_ast.ID(tmp), site), *tests], tmp


def _extraction(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    xname, chunk, rest, fn, same=frozenset(), graph=None
):
    """``(call site, piece, status variable)`` of extracting ``chunk`` to ``xname`` from a
    function whose other statements are ``rest`` (control flow ``graph``), or None when a
    goto crosses its edge, it falls into the next case, returns a value, or sets more than
    one variable live on leaving it, or one and leaves early to elsewhere than its end."""
    kinds = _kinds(chunk, At(fn.noreturn))
    used = names_in(rest) if graph is None else graph.leaving(chunk)
    outs = sorted(writes(chunk) & used & fn.local)
    if kinds is None or labels(chunk) & targets(rest) or targets(chunk) - labels(chunk):
        return None
    kinds -= same
    if "return" in kinds and not fn.void:
        return None
    if (kinds and outs) or len(outs) > 1:
        return None
    ret = outs[0] if outs else STATUS
    codes = {kind: c for c, kind in enumerate(sorted(kinds), 1)}
    if not kinds and not outs:
        body, ret = _recode(chunk, dict.fromkeys(same), returning, At(fn.noreturn)), None
    else:
        body = _recode(chunk, {**dict.fromkeys(same), **codes}, _ender(ret), At(fn.noreturn))
        body = body if ends_in_jump(body) else [*body, _end(ret)]
    shared = names_in(body) & (names_in(rest) | set(fn.params)) & fn.local
    inputs = sorted(shared & Graph(body, fn.noreturn).live_in(), key=order)
    site, piece = call(xname, inputs), Piece(xname, inputs, body, ret)
    if outs:
        return [c_ast.Assignment("=", c_ast.ID(ret), site)], piece, None
    if not kinds:
        return [site], piece, None
    got, tmp = _site(xname, site, codes)
    return got, piece, tmp


def _best(name, items, fn):
    """``(block, extraction)`` of the nested block of ``items`` whose extraction
    (:func:`_extraction`) saves the most cognitive complexity, its call site counted, or
    None when none saves any."""
    best, gain, graph = None, 0, Graph(items, fn.noreturn)
    for chunk, depth, same in _blocks(items, 0, frozenset({"return"} if fn.void else ())):
        got = _extraction(name, chunk, swap(items, chunk, []), fn, same, graph)
        if got is not None:
            g = cost(chunk, depth) - cost(got[1].items) - cost(got[0], depth)
            if g > gain:
                best, gain = (chunk, got), g
    return best


def extracted(name, args, items, fn, ret=None):  # pylint: disable=too-many-locals
    """``([Piece], status variables)`` of function ``name`` (parameters ``args``, returning
    ``ret``) and the functions extracted from it: while above :data:`THRESHOLD`, the block
    saving the most (:func:`_best`) becomes ``NAME_xK`` and its call; each in turn."""
    out, temps, k = [], set(), 0
    fn = fn._replace(void=ret is None, params=args)
    while cost(items) > THRESHOLD:
        k = next(j for j in count(k + 1) if f"{name}_x{j}" not in fn.taken)
        best = _best(f"{name}_x{k}", items, fn)
        if best is None:
            break
        chunk, (site, piece, tmp) = best
        items = swap(items, chunk, site)
        inner, more = extracted(
            piece.name, piece.args, ending(piece.items, _ender(piece.ret)), fn, piece.ret
        )
        out += inner
        temps |= more | ({tmp} - {None})
    return [Piece(name, args, items, ret), *out], temps


def _ender(ret):
    """A maker of the jump ending a function returning ``ret``."""
    return lambda: _end(ret)


def _splice(items, f):
    """``items`` with each statement, nested ones included, replaced by the list ``f``
    gives for it."""
    out = []
    for st in items:
        if children(st):
            st = rebuild(st, [_splice(sub, f) for sub in children(st)])
        out += f(st)
    return out


def calling(items, calls):
    """``items`` with each goto to a label of ``calls`` (``{label: call}``) the call and a
    return."""

    def f(st):
        if is_goto(st) and st.name in calls:
            return [calls[st.name], returning()]
        return [st]

    return _splice(items, f)


def returning_at(items, label):
    """``items`` of a section, each goto to the ``label`` ending it a return."""
    return _splice(items, lambda st: [returning()] if is_goto(st, label) else [st])


def _exits(items, i, jump):
    """The rewrites of ``if`` statement ``i`` of ``items`` that leave by ``jump()`` first."""
    st, last = items[i], i == len(items) - 1
    then, els = block(st.iftrue), block(st.iffalse)
    if last:
        pairs = [(els, then, True), (then, els, False)]
    elif not els and ends_in_jump(then):
        pairs = [(items[i + 1 :], then, True)]
    else:
        return []
    out = []
    for first, rest, flip in pairs:
        first = first if ends_in_jump(first) else [*first, jump()]
        out.append(([*items[:i], iff(negate(st.cond) if flip else st.cond, first)], rest))
    return out


def ending(items, jump, depth=0):
    """``items`` ending a function or loop body at nesting ``depth`` (``jump()`` its
    ``return`` or ``continue``), each ``if`` ending this level either way leaving first by
    one arm and the other after it, wherever that lowers the cost (:func:`cost`) once the
    statements moved up end the level in turn; each rewrite lowers it, so they end."""
    while True:
        low, best = cost(items, depth), None
        for i in range(len(items) - 1, -1, -1):
            for head, rest in _exits(items, i, jump) if isinstance(items[i], c_ast.If) else ():
                got = [*head, *ending(rest, jump, depth)]
                c = cost(got, depth)
                if c < low:
                    low, best = c, got
        if best is None:
            return items
        items = best


def guarded(items):
    """``items`` ending a function that returns nothing (:func:`ending` with ``return``)."""
    return ending(items, returning)


def flattened(items, depth=0):
    """``items`` at nesting ``depth`` with each ``if`` whose ``else`` ends in a jump and whose
    ``then`` does not tested the other way, the jumping arm first, wherever that lowers the
    cost (:func:`cost`), and each loop body ending in an ``if`` continuing first."""
    out = []
    for st in items:
        if isinstance(st, c_ast.If):
            then = flattened(block(st.iftrue), depth + 1)
            els = flattened(block(st.iffalse), depth + 1)
            st = rebuild(st, [then, els])
            if els and ends_in_jump(els) and not ends_in_jump(then):
                alt = [iff(negate(st.cond), els), *then]
                if cost(alt, depth) < cost([st], depth):
                    out += alt
                    continue
        elif isinstance(st, q.LOOPS):
            body = ending(flattened(block(st.stmt), depth + 1), c_ast.Continue, depth + 1)
            st = rebuild(st, [body])
        elif isinstance(st, c_ast.Switch):
            st = rebuild(st, [flattened(b, depth + 1) for b in children(st)])
        out.append(st)
    return out


class Unsupported(ValueError):
    """A function the rewrites leave as it is: one whose locals cannot move between
    functions (static, an array, address taken, one name of two types), or with a
    computed ``goto``."""


def _normal(items, types):  # pylint: disable=too-many-branches
    """``items`` in the form of :mod:`wayward_eagle.c.stmt`, each local declaration an
    assignment (its type in ``types``), braces around plain blocks dropped."""
    out = []
    for st in items:
        if isinstance(st, c_ast.Decl):
            out += _hoist(st, types)
        elif isinstance(st, c_ast.DeclList):
            out += [x for d in st.decls for x in _hoist(d, types)]
        elif isinstance(st, c_ast.Compound):
            out += _normal(block(st), types)
        elif isinstance(st, c_ast.Label):
            out.append(c_ast.Label(st.name, c_ast.EmptyStatement(), st.coord))
            if not isinstance(st.stmt, c_ast.EmptyStatement):
                out += _normal([st.stmt], types)
        elif isinstance(st, c_ast.If):
            then, els = _normal(block(st.iftrue), types), _normal(block(st.iffalse), types)
            out.append(rebuild(st, [then, els]))
        elif isinstance(st, c_ast.For):
            init = st.init
            if isinstance(init, c_ast.DeclList):
                sets = [x for d in init.decls for x in _hoist(d, types)]
                init = sets[0] if len(sets) == 1 else c_ast.ExprList(sets) if sets else None
            loop = c_ast.For(init, st.cond, st.next, None, st.coord)
            out.append(rebuild(loop, [_normal(block(st.stmt), types)]))
        elif isinstance(st, q.LOOPS):
            out.append(rebuild(st, [_normal(block(st.stmt), types)]))
        elif isinstance(st, c_ast.Switch):
            out.append(_switch(st, types))
        elif isinstance(st, LABELS):
            raise Unsupported("a case label outside its switch's body")
        else:
            out.append(st)
    return out


def _switch(st, types):
    """A ``switch`` in the arms form, each arm's statements one ``Compound``, an arm falling
    into the next ending in ``__attribute__((fallthrough))``: falling off an arm's end
    leaves the switch, as :func:`_clean` makes it again with a ``break``."""
    cases, pending = [], []

    def flush():
        if pending:
            cases[-1].stmts[0].block_items += _normal(pending, types)
            pending.clear()

    for x in block(st.stmt):
        if not isinstance(x, LABELS):
            if not cases:
                raise Unsupported("statements before a switch's first label")
            if not cases[-1].stmts:
                cases[-1].stmts = [braced([])]
            pending.append(x)
            continue
        flush()
        stmts = [braced(_normal(list(x.stmts), types))] if x.stmts else []
        if isinstance(x, c_ast.Case):
            cases.append(c_ast.Case(x.expr, stmts, x.coord))
        else:
            cases.append(c_ast.Default(stmts, x.coord))
    flush()
    out = c_ast.Switch(st.cond, braced(cases), st.coord)
    for _, items in arms(out)[:-1]:
        if not ends_in_jump(items):
            items.append(fallthrough())
    return out


def fallthrough():
    """``__attribute__((fallthrough));``"""
    attr = c_ast.AttributeSpecifier([c_ast.Attribute("fallthrough", None, None)])
    return c_ast.AttributedStatement([attr], c_ast.EmptyStatement())


def _hoist(d, types):
    """Local declaration ``d`` recorded in ``types``, as the assignment of its initializer."""
    if d.name is None or isinstance(d.type, c_ast.FuncDecl):
        raise Unsupported("a local type or function declaration")
    if "static" in d.storage or "extern" in d.storage or isinstance(d.type, c_ast.ArrayDecl):
        raise Unsupported(f"{d.name} cannot move between functions")
    tree = copy.deepcopy(d.type)
    if d.name in types and q.key(types[d.name]) != q.key(tree):
        raise Unsupported(f"{d.name} has two types")
    types[d.name] = tree
    if d.init is None:
        return []
    if isinstance(d.init, c_ast.InitList):
        raise Unsupported(f"{d.name} has an initializer list")
    return [c_ast.Assignment("=", c_ast.ID(d.name), d.init, d.coord)]


def _addressed(body):
    """The names whose address ``body`` takes."""
    return {
        n.expr.name
        for n in q.walk(body)
        if isinstance(n, c_ast.UnaryOp) and n.op == "&" and isinstance(n.expr, c_ast.ID)
    }


def _declared(tree, name):
    """A ``Decl`` of ``name`` of type ``tree``."""
    tree = copy.deepcopy(tree)
    t = tree
    while not isinstance(t, c_ast.TypeDecl):
        t = t.type
    t.declname = name
    return c_ast.Decl(name, list(t.quals or []), None, [], [], tree, None, None)


def declare(items, types):
    """``items`` with each variable of ``types`` they use declared in the innermost statement
    list holding every use that sets it before each read: at its first assignment there when
    that is a statement of the list, else at the list's start."""
    used = names_in(items) & set(types)
    return _declare(items, sorted(used, key=order), types)


def _declare(items, names, types):
    """``items`` declaring variables ``names`` (:func:`declare`)."""
    here, down = [], {}
    for v in names:
        users = [st for st in items if v in names_in([st])]
        if len(users) == 1 and v not in own(users[0]):
            subs = [sub for sub in children(users[0]) if v in names_in(sub)]
            if len(subs) == 1 and assigned_first(subs[0], v):
                down.setdefault(id(users[0]), {}).setdefault(id(subs[0]), []).append(v)
                continue
        here.append(v)
    out = list(items)
    for i, st in enumerate(out):
        if id(st) in down:
            subs = down[id(st)]
            lists = [_declare(s, subs[id(s)], types) if id(s) in subs else s for s in children(st)]
            out[i] = rebuild(st, lists)
    starts = []
    for v in here:
        first = next((i for i, st in enumerate(out) if v in names_in([st])), None)
        st = out[first] if first is not None else None
        d = _declared(types[v], v)
        if _initializes(st, v) and assigned_first(out, v):
            d.init, d.coord = st.rvalue, st.coord
            out[first] = d
        elif isinstance(st, c_ast.For) and _initializes(st.init, v) and _alone(out, st, v):
            d.init = st.init.rvalue
            out[first] = c_ast.For(c_ast.DeclList([d]), st.cond, st.next, st.stmt, st.coord)
        else:
            starts.append(d)
    return starts + out


def _alone(items, st, v):
    """Whether ``st`` is the only statement of ``items`` mentioning ``v``."""
    return [x for x in items if v in names_in([x])] == [st]


def _initializes(st, v):
    """Whether statement ``st`` is ``v = e`` with ``e`` not reading ``v``."""
    return (
        isinstance(st, c_ast.Assignment)
        and st.op == "="
        and isinstance(st.lvalue, c_ast.ID)
        and st.lvalue.name == v
        and v not in q.reads(st.rvalue)
    )


def _clean(items):
    """``items`` as plain ``c_ast``: an empty ``else`` none, one holding only an ``if`` an
    ``else if``, an arm falling off its end into the next a ``break``."""
    out = []
    for st in items:
        if children(st):
            st = rebuild(st, [_clean(sub) for sub in children(st)])
            if isinstance(st, c_ast.If):
                els = block(st.iffalse)
                st.iffalse = (
                    None
                    if not els
                    else els[0] if len(els) == 1 and isinstance(els[0], c_ast.If) else st.iffalse
                )
            if isinstance(st, c_ast.Switch):
                for _, sub in arms(st)[:-1]:
                    if not ends_in_jump(sub):
                        sub.append(c_ast.Break())
        out.append(st)
    return out


def _params(fd):
    """``[(name, type tree)]`` of a function definition's parameters."""
    args = fd.decl.type.args
    out = []
    for p in args.params if args is not None else ():
        if isinstance(p, c_ast.Decl) and p.name:
            out.append((p.name, copy.deepcopy(p.type)))
    return out


def _named_type(names, declname=None):
    return c_ast.TypeDecl(declname, [], None, c_ast.IdentifierType(names))


def _returns_void(fd):
    t = fd.decl.type.type
    return isinstance(t, c_ast.TypeDecl) and getattr(t.type, "names", None) == ["void"]


def _signature(name, args, ret, types, status):
    """A ``static`` declaration of piece ``name`` taking ``args`` and returning ``ret``."""
    if args:
        params = c_ast.ParamList([_declared(types[a], a) for a in args])
    else:
        params = c_ast.ParamList([c_ast.Typename(None, [], None, _named_type(["void"]))])
    if ret is None:
        rtype = _named_type(["void"], name)
    else:
        rtype = _declared(status if ret == STATUS else types[ret], name).type
    return c_ast.Decl(name, [], None, ["static"], [], c_ast.FuncDecl(params, rtype), None, None)


def reduce_function(  # pylint: disable=too-many-locals
    fd, noreturn=frozenset(), status=None, taken=frozenset()
):
    """``[FuncDef]`` of C function ``fd`` rewritten (:func:`reduced`): the functions it is cut
    into (each ``static``, none named as in ``taken``) first, ``fd``'s own last; ``[fd]`` when
    its locals cannot move. ``noreturn``: functions that never return; ``status``: exit codes."""
    status = status if status is not None else _named_type(["int"])
    params = _params(fd)
    types = dict(params)
    try:
        body = _normal(block(copy.copy(fd.body)), types)
        if _addressed(fd.body) & set(types):
            raise Unsupported("a local's address is taken")
        if any(isinstance(n, c_ast.Goto) and not isinstance(n.name, str) for n in q.walk(fd.body)):
            raise Unsupported("a computed goto")
    except Unsupported:
        return [fd]
    names = [n for n, _ in params]

    def stops(st):
        return isinstance(st, c_ast.FuncCall) and q.callee(st) in noreturn

    name = fd.decl.name
    body = Rotation(types, stops, widen=True)(refined(body, set(types)))
    taken = frozenset(taken) | {n.name for n in q.walk(fd.body) if isinstance(n, c_ast.ID)}
    fn = Function(names, set(types) - set(names), _returns_void(fd), stops, taken)
    pieces, secs, temps = reduced(body, name, fn)
    types.update({t: copy.deepcopy(status) for t in temps})
    pieces = [p._replace(items=finished(p.items, p.ret is None, types, stops)) for p in pieces]
    if len(secs) > 1:
        pieces.append(Piece(name, names, [call(sec, names) for sec in secs]))
    out = []
    for p in pruned(pieces, name):
        mine = {v: t for v, t in types.items() if v not in p.args}
        items = _clean(declare(p.items, mine))
        unread = [a for a in p.args if a not in names_in(items)]
        items = [c_ast.Cast(_void(), c_ast.ID(a)) for a in unread] + items
        decl = fd.decl if p.name == name else _signature(p.name, p.args, p.ret, types, status)
        out.append(c_ast.FuncDef(decl, None, braced(items)))
    out.sort(key=lambda f: f.decl.name == name)
    return out


def finished(items, void, types, stops=None):
    """Statements ``items`` of a function (returning nothing when ``void``) refined
    (:func:`~wayward_eagle.c.refine.refined`, rotated), copies propagated, dead stores and
    unreached statements dropped, and trailing jumps trimmed."""
    local = set(types)
    items = reachable(Rotation(types, stops)(refined(items, local)), stops)
    return trimmed(unstored(propagated(items, types, stops), local, stops), void)


def _exprs(items, f):
    """``items`` with each node of their expressions for which ``f`` gives one replaced."""

    def fix(_lst, _i, st):
        exprs = heads(st)
        new = [q.replaced(e, f) for e in exprs]
        return reheaded(st, new) if any(a is not b for a, b in zip(new, exprs)) else st

    return remap(items, fix)


def pruned(pieces, top):
    """``pieces`` without each parameter a ``static`` piece (not ``top``) never mentions, nor
    its argument in each call of it."""
    pieces = list(pieces)
    while True:
        for k, p in enumerate(pieces):
            gone = {i for i, a in enumerate(p.args) if a not in names_in(p.items)}
            if gone and p.name != top:
                break
        else:
            return pieces
        pieces[k] = p._replace(args=[a for i, a in enumerate(p.args) if i not in gone])

        def drop(n, p=p, gone=gone):
            if isinstance(n, c_ast.FuncCall) and q.callee(n) == p.name:
                args = [a for i, a in enumerate(q.arguments(n)) if i not in gone]
                return c_ast.FuncCall(n.name, c_ast.ExprList(args) if args else None, n.coord)
            return None

        pieces = [x._replace(items=_exprs(x.items, drop)) for x in pieces]


def _void():
    return c_ast.Typename(None, [], None, _named_type(["void"]))


def prototypes(functions):
    """The declarations of the ``static`` functions of ``functions`` (``FuncDef``, as
    :func:`reduce_function` returns them) but the last, the function cut."""
    return [f.decl for f in functions[:-1] if "static" in f.decl.storage]


def reduce_unit(unit, status=None, functions=None):
    """The C text of :class:`~wayward_eagle.c.Unit` ``unit`` with each function definition
    (of the names ``functions``, all when None) reduced (:func:`reduce_function`) in place,
    the rest of the file as written."""
    from .printer import render  # pylint: disable=import-outside-toplevel

    noreturn = {s.name for s in unit.symbols.values() if s.noreturn}
    taken = {s.name for s in unit.symbols.values() if s.function is None}
    text, out, at = unit.text, [], 0
    for f in unit.functions:
        if functions is not None and f.decl.name not in functions:
            continue
        got = reduce_function(f, noreturn, status, taken - {f.decl.name})
        taken |= {g.decl.name for g in got}
        if got == [f]:
            continue
        info = unit.sema[f]
        out += [text[at : info.begin.offset].decode(), render(prototypes(got) + got, unit.macros)]
        at = info.end.offset + info.end.toklen
    return "".join(out) + text[at:].decode()
