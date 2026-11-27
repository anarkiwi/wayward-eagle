"""Reduced functions returning nothing left with top-level labels cut again, where that
lowers their gotos and total cognitive complexity and adds no piece returning exit codes
(docs/rewrites.md)."""

from typing import NamedTuple

from pycparser import c_ast

from . import query as q
from .flow import parameters
from .stmt import block


def recut(functions, again, status, taken):
    """``functions`` with each returning nothing whose body holds a top-level label cut
    again (``again(f, taken names)``) where :func:`measures` says it is better; each
    function's own definition stays last of its cut."""
    names = set(taken) | {f.decl.name for f in functions}
    out = []
    for f in functions:
        got = [f]
        if q.is_void(f.decl.type.type) and any(isinstance(st, c_ast.Label) for st in block(f.body)):
            cut = again(f, names)
            if measures(cut, status) < measures([f], status):
                got = cut
                names |= {g.decl.name for g in cut}
        out += got
    return out


def measures(functions, status):
    """What a second cut must lower and keep: its gotos and total cognitive complexity
    lower, its pieces returning exit codes no more (``<`` says so); its declarations
    (parameters and locals) are counted too."""
    gotos = sum(isinstance(n, c_ast.Goto) for f in functions for n in q.walk(f.body))
    complexity = sum(q.cognitive(f.body) for f in functions)
    params = sum(len(parameters(f)) for f in functions)
    decls = params + sum(isinstance(n, c_ast.Decl) for f in functions for n in q.walk(f.body))
    codes = sum(_returns_status(f, status) for f in functions)
    return Measures(gotos, complexity, decls, codes)


class Measures(NamedTuple):
    """A cut's gotos, total cognitive complexity, declarations and exit-code pieces."""

    gotos: int
    complexity: int
    decls: int
    codes: int

    def __lt__(self, other):
        """Whether this cut is better than ``other``: :func:`measures`."""
        return (
            self.gotos < other.gotos
            and self.complexity < other.complexity
            and self.codes <= other.codes
        )


def _returns_status(f, status):
    """Whether function definition ``f`` returns the exit codes' type ``status``."""
    t = f.decl.type.type
    names = status.type.names if status is not None else ["int"]
    return isinstance(t, c_ast.TypeDecl) and getattr(t.type, "names", None) == names
