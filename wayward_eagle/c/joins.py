"""Forward gotos by their reaching condition: a label every goto to which ends a path through
the ``if`` statement before it goes, the statements between them guarded by the negated
condition of those paths, when each condition still holds there; and loops left by gotos
and one ``break`` to a tail ending in a jump left by ``break`` alone (docs/rewrites.md)."""

from __future__ import annotations

import copy
from collections import Counter
from functools import reduce

from pycparser import c_ast

from . import query as q
from .fold import clash, facts, volatile
from .refine import infinite
from .stmt import (
    block,
    braced,
    children,
    ends_in_jump,
    iff,
    is_goto,
    labels,
    negate,
    rebuild,
    targets,
)


class _Held(Exception):
    """A goto to the label is not where the rewrite can take it."""


def rejoined(items, local, outer=None, depth=0):
    """``items`` (nested lists first) with each label whose gotos all end paths through
    the ``if`` before it gone, where that keeps the cognitive complexity or lowers it;
    ``local`` the function's variables, ``outer`` its file-scope declarators."""
    arrays = {k for k, t in (outer or {}).items() if isinstance(t, c_ast.ArrayDecl)}
    counts = Counter(n.name for n in q.walk(braced(items)) if isinstance(n, c_ast.Goto))
    return _Joiner(local, arrays, counts).list(items, depth)


class _Joiner:  # pylint: disable=too-few-public-methods
    def __init__(self, local, arrays, counts):
        self.local, self.arrays, self.counts = set(local), arrays, counts

    def list(self, items, depth):
        """``items`` rewritten, nested lists first, at nesting ``depth``."""
        items = [
            rebuild(st, [self.list(sub, depth + 1) for sub in children(st)]) if children(st) else st
            for st in items
        ]
        k = 0
        while k < len(items):
            got = _exited(items, k, depth) if isinstance(items[k], q.LOOPS) else None
            if got is not None:
                items, name, gone = got
                self.counts[name] -= gone - 1
            k += 1
        k = 0
        while k < len(items):
            got = self._join(items, k, depth) if isinstance(items[k], c_ast.Label) else None
            if got is None:
                k += 1
            else:
                items = got
        return items

    def _join(self, items, j, depth):
        """``items`` with label ``items[j]`` joined, or None."""
        name = items[j].name
        holders = [i for i in range(j) if name in targets([items[i]])]
        if len(holders) != 1 or not isinstance(items[holders[0]], c_ast.If):
            return None
        i = holders[0]
        held = sum(isinstance(n, c_ast.Goto) and n.name == name for n in q.walk(items[i]))
        between = items[i + 1 : j]
        if held != self.counts[name] or labels(between):
            return None
        st = _tailed(items[i], name)
        try:
            paths = self._paths(st, name, bool(between))
        except _Held:
            return None
        if not between:
            return self._joined(items, i, j, depth, _dropped(st, name))
        cond = reduce(
            lambda a, b: c_ast.BinaryOp("||", a, b),
            (reduce(lambda a, b: c_ast.BinaryOp("&&", a, b), p) for p in paths),
        )
        guard = [iff(_not(copy.deepcopy(cond)), between)]
        return self._joined(items, i, j, depth, _dropped(st, name) + guard)

    def _joined(self, items, i, j, depth, new):
        """``items`` with ``items[i : j + 1]`` replaced by ``new`` where that costs no more,
        else None."""
        new = items[:i] + new + items[j + 1 :]
        if _cost(new, depth) > _cost(items, depth):
            return None
        self.counts[items[j].name] = 0
        return new

    def _paths(self, st, name, pure=True):
        """The paths through ``if`` statement ``st`` (each a list of conditions) ending in
        ``goto name``, each condition (where ``pure``) pure and unchanged by what runs after
        it in ``st``."""
        cond = st.cond
        mine = facts(cond, self.local, arrays=self.arrays)
        arms = [block(st.iftrue), block(st.iffalse)]
        after = facts(braced([*arms[0], *arms[1]]), self.local, arrays=self.arrays)
        if pure and (mine.effect or mine.stores or volatile(cond) or clash(mine, after)):
            raise _Held
        out = [[cond, *p] for p in self._ends(arms[0], name, pure)]
        return out + [[negate(cond), *p] for p in self._ends(arms[1], name, pure)]

    def _ends(self, items, name, pure):
        """The paths through ``items`` ending in ``goto name``: none when no goto to it is
        in them, else the paths through their last statement, the only one holding one."""
        if name not in targets(items):
            return []
        if any(name in targets([st]) for st in items[:-1]):
            raise _Held
        last = items[-1]
        if is_goto(last, name):
            return [[]]
        if isinstance(last, c_ast.If):
            return self._paths(last, name, pure)
        raise _Held


def _exited(items, k, depth):
    """``(items, label, gotos)`` with infinite loop ``items[k]``, left by one ``break`` of its own
    and by gotos to one label outside it, left by ``break`` alone: the statements after it
    (ending in a jump, holding no label, ``break`` or ``continue``) put at the ``break``, each
    goto a ``break`` and a goto after the loop; None where that does not fit or costs more."""
    loop, tail = items[k], items[k + 1 :]
    if not infinite(loop) or not ends_in_jump(tail) or labels(tail) or _loose(tail):
        return None
    body = block(loop.stmt)
    exits = _own(body)
    breaks = [e for e in exits if isinstance(e, c_ast.Break)]
    names = {e.name for e in exits if isinstance(e, c_ast.Goto)} - labels(body)
    if len(breaks) != 1 or len(names) != 1 or _buried(body, next(iter(names))):
        return None
    name = next(iter(names))

    def swap(st):
        if st is breaks[0]:
            return copy.deepcopy(tail)
        return [c_ast.Break()] if is_goto(st, name) else [st]

    new = items[:k] + [rebuild(loop, [_swapped(body, swap)]), c_ast.Goto(name)]
    if _cost(new, depth) > _cost(items, depth):
        return None
    return new, name, sum(is_goto(e, name) for e in exits)


def _own(items):
    """The jumps of ``items`` leaving the loop around them: gotos and ``break``s not in a
    loop or ``switch`` nested in them."""
    out = []
    for st in items:
        if isinstance(st, (c_ast.Break, c_ast.Goto)):
            out.append(st)
        elif not isinstance(st, (*q.LOOPS, c_ast.Switch)):
            for sub in children(st):
                out += _own(sub)
    return out


def _buried(items, name):
    """Whether a goto to ``name`` in ``items`` lies in a loop or ``switch`` nested in them."""
    for st in items:
        if isinstance(st, (*q.LOOPS, c_ast.Switch)):
            if name in targets([st]):
                return True
        elif any(_buried(sub, name) for sub in children(st)):
            return True
    return False


def _loose(items):
    """Whether ``items`` hold a ``break`` or ``continue``."""
    return any(isinstance(n, (c_ast.Break, c_ast.Continue)) for n in q.walk(braced(items)))


def _swapped(items, f):
    """``items`` with each statement ``f`` maps (to a list) replaced, outside nested loops
    and ``switch`` statements, nested lists first."""
    out = []
    for st in items:
        if children(st) and not isinstance(st, (*q.LOOPS, c_ast.Switch)):
            st = rebuild(st, [_swapped(sub, f) for sub in children(st)])
        out += f(st)
    return out


def _tailed(st, name):
    """``if`` statement ``st`` with, in each list nested in its arms, the statements after
    an ``if`` one arm of which ends in ``goto name`` and the other is empty moved into that
    other arm: they run on that path alone."""
    arms = [_tails(block(st.iftrue), name), _tails(block(st.iffalse), name)]
    return c_ast.If(st.cond, braced(arms[0]), braced(arms[1]), st.coord)


def _tails(items, name):
    out = []
    for k, st in enumerate(items):
        if isinstance(st, c_ast.If):
            st = _tailed(st, name)
            then, els = block(st.iftrue), block(st.iffalse)
            rest = items[k + 1 :]
            if rest and _ends_in(then, name) and not els:
                return out + [_tailed(c_ast.If(st.cond, braced(then), braced(rest)), name)]
            if rest and _ends_in(els, name) and not then:
                return out + [_tailed(c_ast.If(st.cond, braced(rest), braced(els)), name)]
        out.append(st)
    return out


def _ends_in(items, name):
    return bool(items) and is_goto(items[-1], name)


def _dropped(st, name):
    """``[st]`` with each ``goto name`` ending a path through it gone (``[]`` when nothing
    is left of it: its condition is pure)."""
    then, els = (_unended(block(arm), name) for arm in (st.iftrue, st.iffalse))
    if not then and not els:
        return []
    if not then:
        return [c_ast.If(negate(st.cond), braced(els), braced([]), st.coord)]
    return [c_ast.If(st.cond, braced(then), braced(els), st.coord)]


def _unended(items, name):
    if not items:
        return items
    last = items[-1]
    if is_goto(last, name):
        return items[:-1]
    if isinstance(last, c_ast.If) and name in targets([last]):
        return items[:-1] + _dropped(last, name)
    return items


def _not(e):
    """The negation of condition ``e``, through ``&&`` and ``||`` (De Morgan's laws)."""
    if isinstance(e, c_ast.BinaryOp) and e.op in ("&&", "||"):
        return c_ast.BinaryOp("||" if e.op == "&&" else "&&", _not(e.left), _not(e.right))
    return negate(e)


def _cost(items, depth):
    """The cognitive complexity of ``items`` at ``depth`` as printed: an ``else`` holding one
    ``if`` an ``else if``."""
    return q.cognitive(braced(_chained(items)), depth)


def _chained(items):
    out = []
    for st in items:
        if isinstance(st, c_ast.If):
            then, els = _chained(block(st.iftrue)), _chained(block(st.iffalse))
            tail = els[0] if len(els) == 1 and isinstance(els[0], c_ast.If) else braced(els)
            st = c_ast.If(st.cond, braced(then), tail if els else None, st.coord)
        elif children(st):
            st = rebuild(st, [_chained(sub) for sub in children(st)])
        out.append(st)
    return out
