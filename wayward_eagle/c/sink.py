"""Forward gotos as structure: the statements a ``goto`` jumps past, put on each path that
falls through to its label, so the ``goto`` leaves and the label goes (docs/rewrites.md,
*Sunk tails*), over the statement lists of :mod:`wayward_eagle.c.stmt`."""

from __future__ import annotations

import copy
from collections import Counter

from pycparser import c_ast

from . import query as q
from .stmt import arms, block, braced, cost, ends_in_jump, is_goto, is_site, mapped, negate
from .stmt import rebuild


class _Held(ValueError):
    """A ``goto`` inside a loop or a nested ``switch``, or a tail of code for a second path:
    no tail is put after it."""


def _gotos(items, label=None):
    """The ``goto`` statements (to ``label``, when given) in ``items``, by label."""
    return Counter(n.name for st in items for n in q.walk(st) if is_goto(n, label))


def _placed(tail, placed):
    """A copy of ``tail`` for one more path, ``placed`` (``{id: tail}``) the tails of code
    (not :func:`~wayward_eagle.c.stmt.is_site` alone) put on one already: such a tail put on
    a second holds (:class:`_Held`), each statement of code staying in one place."""
    if not all(map(is_site, tail)):
        if id(tail) in placed:
            raise _Held(tail)
        placed[id(tail)] = tail
    return copy.deepcopy(tail)


def _sunk(items, tail, label, placed, *, after=None, end=True):
    """``items`` then ``tail``, each ``goto label`` in ``items`` leaving past ``tail``: a copy
    of ``tail`` (:func:`_placed`) on each path falling off their end (none for an arm
    falling into the next, ``end`` false). In a ``switch`` arm (``after`` what follows the
    ``switch``) a ``break`` takes ``after`` first and a ``goto label`` breaks."""
    for j, st in enumerate(items):
        if _gotos([st], label) or after is not None and _breaks(st):
            nested = isinstance(st, (c_ast.If, c_ast.Switch))
            rest = (
                _sunk(items[j + 1 :], tail, label, placed, after=after, end=end) if nested else []
            )
            return [*items[:j], *_into(st, rest, label, placed, after)]
    return items if ends_in_jump(items) or not end else [*items, *_placed(tail, placed)]


def _breaks(st):
    """Whether ``st`` breaks out of the ``switch`` it is in."""
    if isinstance(st, c_ast.Break):
        return True
    return isinstance(st, c_ast.If) and any(map(_breaks, [*block(st.iftrue), *block(st.iffalse)]))


def _into(st, rest, label, placed, after):
    """Statement ``st`` then ``rest``, as :func:`_sunk` makes it."""
    if is_goto(st, label):
        return [c_ast.Break()] if after is not None else []
    if isinstance(st, c_ast.Break) and after is not None:
        return [*_placed(after, placed), st]
    if isinstance(st, c_ast.If):
        then = _sunk(block(st.iftrue), rest, label, placed, after=after)
        els = _sunk(block(st.iffalse), rest, label, placed, after=after)
        if not then:
            return [c_ast.If(negate(st.cond), braced(els), braced([]), st.coord)]
        return [c_ast.If(st.cond, braced(then), braced(els), st.coord)]
    if isinstance(st, c_ast.Switch) and after is None:
        bodies = arms(st)
        last = len(bodies) - 1
        lists = [
            _sunk(b, rest, label, placed, after=rest, end=k == last)
            for k, (_, b) in enumerate(bodies)
        ]
        return [rebuild(st, lists)]
    raise _Held(label)


def sunk(items, depth=0, gotos=None):
    """``items`` (nested lists first) with each label whose every ``goto`` lies in the
    statements before it in its own list gone, the statements between the first of them and
    the label put on each path falling to it (:func:`_sunk`) where no statement of code
    goes on two (:func:`_placed`) and that keeps the cost
    (:func:`~wayward_eagle.c.stmt.cost`) or lowers it."""
    gotos = _gotos(items) if gotos is None else gotos
    items = mapped(items, lambda sub: sunk(sub, depth + 1, gotos))
    k = 0
    while k < len(items):
        st = items[k]
        name = st.name if isinstance(st, c_ast.Label) else None
        before = [i for i in range(k) if _gotos([items[i]], name)] if name else []
        if not before or sum(_gotos(items[before[0] : k], name).values()) != gotos[name]:
            k += 1
            continue
        i = before[0]
        try:
            new = _sunk(items[i:k], [], name, {})
        except _Held:
            k += 1
            continue
        if cost(new, depth) > cost(items[i:k], depth):
            k += 1
            continue
        gotos[name] = 0
        items = [*items[:i], *new, *items[k + 1 :]]
        k = i + len(new)
    return items
