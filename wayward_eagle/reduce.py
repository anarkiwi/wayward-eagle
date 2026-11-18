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
    """``([(function name, parameters, items)], sections)`` of the statements ``body`` of C
    function ``name`` (:class:`Function` ``fn``), each rewrite lowering cognitive
    complexity: a function returning nothing cut into sections (:func:`sections`,
    ``NAME_K``, called in turn), each section's pieces (:func:`_pieces`) then."""
    parts = sections(body, fn.local) if fn.void else [(None, body)]
    pieces = []
    for k, (lab, items) in enumerate(parts, 1):
        sec = name if len(parts) == 1 else f"{name}_{k}"
        pieces += _pieces(sec, returning(items, lab) if lab else items, fn)
    return pieces, len(parts)


def _pieces(sec, items, fn):
    """The functions of section ``sec``: its tails (:func:`tails`, ``SECTION_LABEL``) when
    it returns nothing, each flattened (:func:`flattened`), ended (:func:`guarded`) when it
    returns nothing, and its nested blocks extracted while above :data:`THRESHOLD`
    (:func:`extracted`, ``PIECE_xK``)."""
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
        body = guarded(flattened(body)) if fn.void else flattened(body)
        out += extracted(name, args, body, fn.local | set(params), fn.noreturn)
    return out


def _leaves(st, k, items, ctx):
    """Whether jump ``st``, statement ``k`` of ``items``, leaves them (:func:`_escapes`,
    ``ctx`` its ``(noreturn, loops, switches, labels)``)."""
    noreturn, loops, switches, inside = ctx
    text = st[1]
    return (
        "fallthrough" in text
        or (text.startswith("return") and not (k and noreturn(items[k - 1])))
        or (text.startswith("goto ") and text[5:-1] not in inside)
        or (text == "break;" and not loops + switches)
        or (text == "continue;" and not loops)
    )


def _escapes(items, noreturn, loops=0, switches=0, inside=frozenset()):
    """Whether control may leave ``items`` but by falling off their end: a return (not
    right after a statement ``noreturn`` holds of, a call that never returns), a fall into
    the next case, a ``break`` or ``continue`` of no loop or ``switch`` within them, a goto
    to a label outside them."""
    inside = inside or frozenset(labels(items))
    ctx = (noreturn, loops, switches, inside)
    for k, st in enumerate(items):
        if st[0] == "j":
            hit = _leaves(st, k, items, ctx)
        elif st[0] == "loop":
            hit = _escapes(st[4], noreturn, loops + 1, switches, inside)
        elif st[0] == "switch":
            hit = any(_escapes(b, noreturn, loops, switches + 1, inside) for _, b in st[3])
        else:
            hit = st[0] == "if" and any(
                _escapes(a, noreturn, loops, switches, inside) for a in st[3:5]
            )
        if hit:
            return True
    return False


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


def extracted(name, args, items, local, noreturn=never):
    """``[(function name, parameters, items)]`` of function ``name`` (``args`` its
    parameters) and the functions extracted from it: while its cognitive complexity is above
    :data:`THRESHOLD`, the nested block whose statements save the most nesting moved to
    depth 0 that control leaves only by its end (:func:`_escapes` with ``noreturn``), no
    goto outside it enters, and sets no variable used outside it becomes ``NAME_xK`` of the
    variables it reads that are used outside it, and its call; each extracted function in
    turn."""
    out, k = [], 0
    while cost(items) > THRESHOLD:
        best, gain = None, 0
        for block, depth in _blocks(items):
            g = cost(block, depth) - cost(block)
            if g <= gain or _escapes(block, noreturn):
                continue
            rest = _swap(items, block, [])
            if labels(block) & targets(rest):
                continue
            if {st[1] for st in walk(block) if st[0] == "a"} & names_in(rest) & local:
                continue
            best, gain = block, g
        if best is None:
            break
        k += 1
        xname, inputs = f"{name}_x{k}", sorted(
            names_in(best) & names_in(_swap(items, best, [])) & local, key=order
        )
        items = _swap(items, best, [("s", f"{xname}({', '.join(inputs)});", frozenset(inputs))])
        out += extracted(xname, inputs, guarded(best), local, noreturn)
    return [(name, args, items), *out]


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
