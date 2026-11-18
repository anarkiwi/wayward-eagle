"""Rewrites of a C function's statement tree that lower its cognitive complexity and keep
what it does: sections, tails, early exits, flattened arms and extracted blocks."""

from __future__ import annotations

from typing import Callable, NamedTuple

from .cost import cost
from .tree import (
    CONTINUE,
    RETURN,
    assigned_first,
    children,
    ends_in_jump,
    labels,
    names_in,
    order,
    targets,
    walk,
)

#: The cognitive complexity a function is left at (SonarQube's default for C): above it,
#: nested blocks are extracted (:func:`extracted`).
THRESHOLD = 25


def sections(items, local):
    """``[(label, items)]`` of a function body cut at each top-level label no variable of
    ``local`` is used across and no goto crosses but into it from before: what follows
    the label runs the same whatever ran before it, so each section is a function of its
    own (the label ending the section before it, none after the last)."""
    out, start = [], 0
    for i, st in enumerate(items):
        if st[0] != "l":
            continue
        before, after, lab = items[start:i], items[i + 1 :], st[1][:-2]
        shared = names_in(before) & names_in(after) & local
        if shared or targets(before) & (labels(after) - {lab}):
            continue
        if targets(after) & (labels(before) | {lab}):
            continue
        out.append((lab, before))
        start = i + 1
    return out + [(None, items[start:])]


def tails(items, local, call):
    """``(head, [(label, items, params)])`` of a function body: from its last top-level
    label back, each label nothing after it jumps back before is the start of a tail, the
    statements after it to the next one, ``params`` the variables of ``local`` the tail
    reads before it sets them (:func:`assigned_first`), the gotos to later tails and the
    fall into the next read as those tails' calls (``call(label, params)``)."""
    out, refs = [], {}
    while True:
        i = next((i for i in range(len(items) - 1, -1, -1) if items[i][0] == "l"), None)
        if i is None:
            return items, out
        before, after, lab = items[:i], items[i + 1 :], items[i][1][:-2]
        if targets(after) & (labels(before) | {lab}) or targets(before) & labels(after):
            return items, out
        body = calling(after, {t: call(t, ps) for t, ps in refs.items()})
        if out:
            body = [*body, call(out[0][0], refs[out[0][0]])]
        live = names_in(body) & names_in(before) & local
        params = sorted((v for v in live if not assigned_first(body, v)), key=order)
        refs[lab] = params
        out.insert(0, (lab, after, params))
        items = before


def never(_st):
    """No statement is a call that never returns."""
    return False


class Function(NamedTuple):
    """What the rewrites know of a C function: its parameters, its other variables, whether
    it returns nothing, and whether a statement is a call that never returns."""

    params: list
    local: set
    void: bool
    noreturn: Callable = never


def reduced(body, name, fn):
    """``([Piece], sections, status variables)`` of the statements ``body`` of C
    function ``name`` (:class:`Function` ``fn``), each rewrite lowering cognitive
    complexity: a function returning nothing cut into sections (:func:`sections`,
    ``NAME_K``, called in turn), each section's pieces (:func:`_pieces`) then."""
    parts = sections(body, fn.local) if fn.void else [(None, body)]
    pieces, temps = [], set()
    for k, (lab, items) in enumerate(parts, 1):
        sec = name if len(parts) == 1 else f"{name}_{k}"
        got, more = _pieces(sec, returning(items, lab) if lab else items, fn)
        pieces += got
        temps |= more
    return pieces, len(parts), temps


def _chain(sec, items, fn):
    """``[(function name, parameters, items)]`` of section ``sec``: its first statements and
    its tails (:func:`tails`, ``SECTION_LABEL``) when it returns nothing, each goto to a
    tail and the fall into the next one its call."""
    params = fn.params

    def call(t, ps):
        return ("s", f"{sec}_{t}({', '.join(params + ps)});", frozenset(params + ps))

    first, chain = tails(items, fn.local, call) if fn.void else (items, [])
    calls = {t: call(t, ps) for t, _, ps in chain}
    seq = [(sec, params, first), *((f"{sec}_{t}", params + ps, b) for t, b, ps in chain)]
    out = []
    for j, (name, args, body) in enumerate(seq):
        body = calling(body, calls)
        if j < len(chain) and not ends_in_jump(body):
            body = [*body, calls[chain[j][0]]]
        out.append((name, args, body))
    return out


def _pieces(sec, items, fn):
    """``([Piece], status variables)`` of section ``sec``: its chain (:func:`_chain`), each
    function flattened (:func:`flattened`), ended (:func:`guarded`) when it returns nothing,
    and its nested blocks extracted while above :data:`THRESHOLD` (:func:`extracted`,
    ``PIECE_xK``)."""
    out, temps = [], set()
    local = fn._replace(local=fn.local | set(fn.params))
    for name, args, body in _chain(sec, items, fn):
        body = guarded(flattened(body)) if fn.void else flattened(body)
        got, more = extracted(name, args, body, local, None if fn.void else STATUS)
        out += got
        temps |= more
    return out, temps


BREAK = ("j", "break;", frozenset())
#: The jump each kind of exit from a block takes.
JUMPS = {"return": RETURN, "break": BREAK, "continue": CONTINUE}


class At(NamedTuple):
    """Where a statement is in the block being judged: the loops and ``switch`` statements
    around it inside the block, and which statements are calls that never return."""

    noreturn: Callable
    loops: int = 0
    switches: int = 0

    def into(self, st):
        """Where the statements nested in ``st`` are."""
        return At(
            self.noreturn, self.loops + (st[0] == "loop"), self.switches + (st[0] == "switch")
        )


def _kind(st, k, items, at):
    """The kind of exit jump ``st`` (statement ``k`` of ``items``) takes out of the block,
    ``"stay"`` for none, None for a fall into the next case, which no extraction carries."""
    text = st[1]
    if "fallthrough" in text:
        return None
    if text.startswith("return"):
        return "stay" if k and at.noreturn(items[k - 1]) else "return"
    if text == "break;" and not at.loops + at.switches:
        return "break"
    return "continue" if text == "continue;" and not at.loops else "stay"


def _kinds(items, at):
    """The kinds of exit control leaves ``items`` by other than their end (``return``,
    ``break``, ``continue``), or None when it may fall into the next case."""
    out = set()
    for k, st in enumerate(items):
        if st[0] == "j":
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
    """``items`` with each exit of a kind in ``codes`` returning its code, and each other
    return (one after a call that never returns, never reached) ``end``."""

    def f(k, st, items):
        if st[0] != "j":
            return st
        kind = _kind(st, k, items, at)
        if kind in codes:
            return ("j", f"return {codes[kind]};", st[2])
        return end if st[1].startswith("return") else st

    out = []
    for k, st in enumerate(items):
        st = f(k, st, items)
        if st[0] == "if":
            st = (*st[:3], *(_recode(a, codes, end, at) for a in st[3:5]), *st[5:])
        elif st[0] == "loop":
            st = (*st[:4], _recode(st[4], codes, end, at.into(st)))
        elif st[0] == "switch":
            st = (*st[:3], [(lab, _recode(b, codes, end, at.into(st))) for lab, b in st[3]])
        out.append(st)
    return out


def _blocks(items, depth=0):
    """``(block, depth)`` of each statement list nested in ``items``: arms, loop and case
    bodies, at the nesting of their statements."""
    for st in items:
        for sub in children(st):
            yield sub, depth + 1
            yield from _blocks(sub, depth + 1)


def _swap(items, block, new):
    """``items`` with the nested statement list ``block`` (by identity) replaced by ``new``."""

    def f(st):
        if st[0] == "if":
            return (
                *st[:3],
                new if st[3] is block else _swap(st[3], block, new),
                new if st[4] is block else _swap(st[4], block, new),
                *st[5:],
            )
        if st[0] == "loop":
            return (*st[:4], new if st[4] is block else _swap(st[4], block, new))
        if st[0] == "switch":
            return (
                *st[:3],
                [(lab, new if b is block else _swap(b, block, new)) for lab, b in st[3]],
            )
        return st

    return [f(st) for st in items]


class Piece(NamedTuple):
    """A function of the rewritten C: its name, parameters, statements and what it returns
    (None nothing, :data:`STATUS` an exit code, else the variable it names)."""

    name: str
    args: list
    items: list
    ret: str | None = None


#: What a function returning an exit code returns (:class:`Piece`).
STATUS = "status"


def _end(ret):
    """The jump ending a function that returns ``ret`` (:class:`Piece`)."""
    if ret is None:
        return RETURN
    return ("j", "return 0;" if ret == STATUS else f"return {ret};", frozenset())


def _site(xname, call, refs, codes):
    """``(call site, status variable)`` of extracted function ``xname`` returning exit
    ``codes`` (``{kind: code}``): one test of the call for one exit, else the status in
    ``NAME_k`` and a test per exit."""
    if len(codes) == 1:
        (kind,) = codes
        return [("if", call, refs, [JUMPS[kind]], [], f"!{call}")], None
    tmp = f"{xname}_k"
    tests = [
        ("if", f"{tmp} == {c}", frozenset({tmp}), [JUMPS[kind]], [], f"{tmp} != {c}")
        for kind, c in codes.items()
    ]
    return [("a", tmp, call, refs), *tests], tmp


def _extraction(xname, block, rest, fn):
    """``(call site, piece, status variable)`` of extracting ``block`` to ``xname`` from a
    function whose other statements are ``rest`` (:class:`Function` ``fn``), or None when it
    cannot be (a goto outside enters it, it falls into the next case, it returns from a
    function returning a value, or it both leaves early and sets a variable used after it,
    or sets more than one): its exits (:func:`_kinds`) become a status it returns, the one
    variable it sets that is used after it its value."""
    kinds = _kinds(block, At(fn.noreturn))
    outs = sorted({st[1] for st in walk(block) if st[0] == "a"} & names_in(rest) & fn.local)
    if kinds is None or labels(block) & targets(rest) or ("return" in kinds and not fn.void):
        return None
    if (kinds and outs) or len(outs) > 1:
        return None
    inputs = sorted(names_in(block) & names_in(rest) & fn.local, key=order)
    call, refs = f"{xname}({', '.join(inputs)})", frozenset(inputs)
    if not kinds and not outs:
        return [("s", f"{call};", refs)], Piece(xname, inputs, block), None
    ret = outs[0] if outs else STATUS
    codes = {kind: c for c, kind in enumerate(sorted(kinds), 1)}
    body = _recode(block, codes, _end(ret), At(fn.noreturn))
    body = body if ends_in_jump(body) else [*body, _end(ret)]
    site, tmp = ([("a", ret, call, refs)], None) if outs else _site(xname, call, refs, codes)
    return site, Piece(xname, inputs, body, ret), tmp


def _best(name, items, fn):
    """``(block, extraction)`` of the nested block of ``items`` whose extraction
    (:func:`_extraction`) saves the most cognitive complexity, its call site counted, or
    None when none saves any."""
    best, gain = None, 0
    for block, depth in _blocks(items):
        got = _extraction(name, block, _swap(items, block, []), fn)
        if got is not None:
            g = cost(block, depth) - cost(got[1].items) - cost(got[0], depth)
            if g > gain:
                best, gain = (block, got), g
    return best


def extracted(name, args, items, fn, ret=None):
    """``([Piece], status variables)`` of function ``name`` (``args`` its parameters, ``ret``
    what it returns, :class:`Function` ``fn``) and the functions extracted from it: while its
    cognitive complexity is above :data:`THRESHOLD`, the block extracting which saves the
    most (:func:`_best`) becomes ``NAME_xK`` and its call site; each extracted function in
    turn."""
    out, temps, k = [], set(), 0
    fn = fn._replace(void=ret is None)
    while cost(items) > THRESHOLD:
        best = _best(f"{name}_x{k + 1}", items, fn)
        if best is None:
            break
        k += 1
        block, (site, piece, tmp) = best
        items = _swap(items, block, site)
        inner, more = extracted(
            piece.name, piece.args, ending(piece.items, _end(piece.ret)), fn, piece.ret
        )
        out += inner
        temps |= more | ({tmp} - {None})
    return [Piece(name, args, items, ret), *out], temps


def _splice(items, f):
    """``items`` with each statement, nested ones included, replaced by the list ``f``
    gives for it."""
    out = []
    for st in items:
        if st[0] == "if":
            st = (*st[:3], _splice(st[3], f), _splice(st[4], f), *st[5:])
        elif st[0] == "loop":
            st = (*st[:4], _splice(st[4], f))
        elif st[0] == "switch":
            st = (*st[:3], [(lab, _splice(b, f)) for lab, b in st[3]])
        out += f(st)
    return out


def calling(items, calls):
    """``items`` with each goto to a label of ``calls`` (``{label: call statement}``) the
    call and a return."""

    def f(st):
        if st[0] == "j" and st[1].startswith("goto ") and st[1][5:-1] in calls:
            return [calls[st[1][5:-1]], RETURN]
        return [st]

    return _splice(items, f)


def _map(items, f):
    """``items`` with each statement, nested ones included, replaced by ``f`` of it."""
    out = []
    for st in items:
        st = f(st)
        if st[0] == "if":
            st = (*st[:3], _map(st[3], f), _map(st[4], f), *st[5:])
        elif st[0] == "loop":
            st = (*st[:4], _map(st[4], f))
        elif st[0] == "switch":
            st = (*st[:3], [(lab, _map(b, f)) for lab, b in st[3]])
        out.append(st)
    return out


def returning(items, label):
    """``items`` of a section, each goto to the ``label`` ending it a return."""
    jump = ("j", f"goto {label};")
    return _map(items, lambda st: RETURN if st[:2] == jump else st)


def _exits(items, i, jump):
    """The rewrites of ``if`` statement ``i`` of ``items`` that leave by ``jump`` first."""
    st, last = items[i], i == len(items) - 1
    if last:
        pairs = [(st[4], st[3], st[5], st[1]), (st[3], st[4], st[1], st[5])]
    elif not st[4] and ends_in_jump(st[3]):
        pairs = [(items[i + 1 :], st[3], st[5], st[1])]
    else:
        return []
    out = []
    for first, rest, cond, neg in pairs:
        first = first if ends_in_jump(first) else [*first, jump]
        out.append(([*items[:i], ("if", cond, st[2], first, [], neg)], rest))
    return out


def ending(items, jump, depth=0):
    """``items`` ending a function or loop body at nesting ``depth`` (``jump`` its ``return``
    or ``continue``), each ``if`` that ends this level either way leaving first by one arm,
    ending in ``jump``, and the other after it at this level, wherever that lowers the
    cognitive complexity (:func:`cost`) once the statements moved up end the level in turn: a
    last ``if`` either way round, one whose arm leaves before the rest with the rest as the
    arm leaving first. Each rewrite lowers the cost and recurs only into the statements of
    one ``if``, so the rewrites end."""
    while True:
        low, best = cost(items, depth), None
        for i in range(len(items) - 1, -1, -1):
            for head, rest in _exits(items, i, jump) if items[i][0] == "if" else ():
                got = [*head, *ending(rest, jump, depth)]
                c = cost(got, depth)
                if c < low:
                    low, best = c, got
        if best is None:
            return items
        items = best


def guarded(items):
    """``items`` ending a function that returns nothing (:func:`ending` with ``return``)."""
    return ending(items, RETURN)


def flattened(items, depth=0):
    """``items`` at nesting ``depth`` with each ``if`` whose ``else`` ends in a jump and whose
    ``then`` does not tested the other way, its ``else`` the jumping arm and its ``then``
    after it, wherever that lowers the cognitive complexity (:func:`cost`), and each loop
    body ending in an ``if`` rewritten to continue first (:func:`ending`)."""
    out = []
    for st in items:
        if st[0] == "if":
            st = (*st[:3], flattened(st[3], depth + 1), flattened(st[4], depth + 1), *st[5:])
            if st[4] and ends_in_jump(st[4]) and not ends_in_jump(st[3]):
                alt = [("if", st[5], st[2], st[4], [], st[1]), *st[3]]
                if cost(alt, depth) < cost([st], depth):
                    out += alt
                    continue
        elif st[0] == "loop":
            st = (*st[:4], ending(flattened(st[4], depth + 1), CONTINUE, depth + 1))
        elif st[0] == "switch":
            st = (*st[:3], [(lab, flattened(b, depth + 1)) for lab, b in st[3]])
        out.append(st)
    return out
