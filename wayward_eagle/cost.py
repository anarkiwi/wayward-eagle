"""Cognitive complexity of a statement tree (SonarSource's rules, as clang-tidy's
``readability-function-cognitive-complexity`` counts them)."""

from __future__ import annotations

from .tree import children


def cost(items, depth=0):
    """The cognitive complexity of ``items`` at nesting ``depth`` (SonarSource's rules on
    the statements, clang-tidy's ``readability-function-cognitive-complexity``): an ``if``,
    loop or ``switch`` one plus its depth, an ``else if`` and an ``else`` one, a ``goto``
    one; a statement's arms one level deeper. Conditions count the same either way."""
    out = 0
    for st in items:
        if st[0] == "j" and st[1].startswith("goto "):
            out += 1
        elif st[0] == "if":
            out += 1 + depth + _arms(st, depth + 1)
        elif st[0] in ("loop", "switch"):
            out += 1 + depth + sum(cost(sub, depth + 1) for sub in children(st))
    return out


def _arms(st, depth):
    """The cost of an ``if``'s arms, an ``else`` holding one ``if`` an ``else if``."""
    out, els = cost(st[3], depth), st[4]
    while len(els) == 1 and els[0][0] == "if":
        out += 1 + cost(els[0][3], depth)
        els = els[0][4]
    return out + (1 + cost(els, depth) if els else 0)
