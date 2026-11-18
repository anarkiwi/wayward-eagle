"""The statement tree of a C function body and what it reads, writes and jumps to.

Statements: ``("a", name, text, refs)`` an assignment, ``("s", text, refs)``, ``("j", text,
refs)`` a jump, ``("l", text, refs)`` a label, ``("if", cond, refs, then, else, negated
cond)``, ``("loop", kind, cond, refs, body)`` (kind ``for``, ``while`` or ``do``) and
``("switch", sel, refs, [(labels, body)])``; ``refs`` the variables a statement reads.
"""

from __future__ import annotations

import re

#: An assignment inside an expression: a name, then ``=`` that is no comparison.
ASSIGN = re.compile(r"(?<![\w.>])(\w+)\s*=(?!=)")

RETURN = ("j", "return;", frozenset())


CONTINUE = ("j", "continue;", frozenset())


def own(st):
    """The variables statement ``st`` reads or writes outside its nested statements."""
    if st[0] == "a":
        return st[3] | {st[1]}
    return st[2] if st[0] in ("s", "j", "l", "if", "switch") else st[3]


def children(st):
    """The statement lists nested in ``st``."""
    if st[0] == "if":
        return [st[3], st[4]]
    if st[0] == "loop":
        return [st[4]]
    if st[0] == "switch":
        return [b for _, b in st[3]]
    return []


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
        out |= own(st)
        for sub in children(st):
            out |= names_in(sub)
    return out


def assigned_first(items, v):
    """Whether every read of ``v`` in ``items`` follows an assignment on every path."""
    return _da(items, v, False)[1]


def _da(items, v, done):  # pylint: disable=too-many-return-statements,too-many-branches
    """``(definitely assigned after, no read before assignment)`` over ``items``; control
    reaching a label may come from anywhere, so nothing is assigned there."""
    gone = False
    for st in items:
        kind = st[0]
        if kind == "l":
            done, gone = False, False
            continue
        if gone:
            continue
        if kind == "a":
            if v in st[3] and not done:
                return done, False
            done = done or st[1] == v
        elif kind == "d":
            if v in st[3] and not done:
                return done, False
            done = done or any(n == v and e is not None for n, e in st[2])
        elif kind in ("s", "j"):
            if v in st[2] and not done:
                return done, False
            if kind == "j":
                done, gone = True, True
        elif kind == "if":
            if v in st[2] and not done:
                return done, False
            a, ok = _da(st[3], v, done)
            b, ok2 = _da(st[4], v, done)
            if not (ok and ok2):
                return done, False
            done = a and b
        elif kind == "loop":
            if v in st[3] and not done and st[1] == "while":
                return done, False
            after, ok = _da(st[4], v, done)
            if not ok or (v in st[3] and not after and st[1] == "do"):
                return done, False
        else:
            if v in st[2] and not done:
                return done, False
            arms = [_da(b, v, done) for _, b in st[3]]
            if not all(ok for _, ok in arms):
                return done, False
            dflt = any(None in labels for labels, _ in st[3])
            done = done or (dflt and all(a for a, _ in arms))
    return done, True


def ends_in_jump(items):
    """Whether control never falls off the end of ``items``."""
    if not items:
        return False
    last = items[-1]
    if last[0] == "j":
        return True
    if last[0] == "if":
        return bool(last[4]) and ends_in_jump(last[3]) and ends_in_jump(last[4])
    return last[0] == "loop" and last[1] == "for" and not _breaks(last[4])


def _breaks(items):
    """Whether ``items`` break out of their loop."""
    for st in items:
        if st[0] == "j" and st[1] == "break;":
            return True
        if st[0] == "if" and (_breaks(st[3]) or _breaks(st[4])):
            return True
        if st[0] == "switch" and any(_breaks(b) for _, b in st[3]):
            return True
    return False


def targets(items):
    """The labels the gotos of ``items`` jump to, nested ones included."""
    return {st[1][5:-1] for st in walk(items) if st[0] == "j" and st[1].startswith("goto ")}


def labels(items):
    """The labels of ``items``, nested ones included."""
    return {st[1][:-2] for st in walk(items) if st[0] == "l"}


def order(name):
    """A variable's sort key: its number after the prefix letter."""
    return (len(name), name)


def _texts(st):
    """The expression texts of statement ``st`` outside its nested statements."""
    if st[0] == "a":
        return [st[2]]
    if st[0] in ("s", "if", "switch"):
        return [st[1]]
    return [st[2]] if st[0] == "loop" else []


def writes(items):
    """Every variable ``items`` set: assigned, or set by an assignment inside an
    expression (a loop's step in its condition, a comma expression)."""
    out = set()
    for st in walk(items):
        if st[0] == "a":
            out.add(st[1])
        for text in _texts(st):
            out |= set(ASSIGN.findall(text))
    return out
