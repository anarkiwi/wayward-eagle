"""Queries over ``c_ast`` nodes: children, walks, structural equality, what a node reads,
writes, jumps to and calls. Each works on bare ``c_ast``; given a :class:`Unit`, equality
and the variable queries use clang's declarations and types instead of names."""

from __future__ import annotations

from pycparser import c_ast

#: Unary operators that store to their operand.
INC_DEC = frozenset(("++", "--", "p++", "p--"))
LOOPS = (c_ast.While, c_ast.DoWhile, c_ast.For)
JUMPS = (c_ast.Goto, c_ast.Break, c_ast.Continue, c_ast.Return)
#: ``c_ast`` nodes that spell a type rather than compute a value.
TYPES = (
    c_ast.TypeDecl,
    c_ast.PtrDecl,
    c_ast.ArrayDecl,
    c_ast.FuncDecl,
    c_ast.IdentifierType,
    c_ast.Typename,
    c_ast.Struct,
    c_ast.Union,
    c_ast.Enum,
)


def kids(n):
    """The child nodes of ``n``, in order."""
    return [c for _, c in n.children()]


def walk(n, stop=()):
    """``n`` and every node under it in preorder, not entering children of a type in
    ``stop``."""
    todo = [n]
    while todo:
        c = todo.pop()
        yield c
        todo += reversed([k for _, k in c.children() if not isinstance(k, stop)])


def statements(n):
    """The statements of a compound statement, or ``[n]`` for any other statement."""
    if isinstance(n, c_ast.Compound):
        return list(n.block_items or ())
    return [] if n is None else [n]


def commas(n):
    """Every comma expression in ``n``: an ``ExprList`` that is no call's arguments."""
    args = {id(c.args) for c in walk(n) if isinstance(c, c_ast.FuncCall)}
    return [c for c in walk(n) if isinstance(c, c_ast.ExprList) and id(c) not in args]


def _attrs(n):
    return tuple(tuple(v) if isinstance(v, list) else v for v in (getattr(n, a) for a in n.attr_names))


def key(n, unit=None, by_name=False):
    """``n``'s structure, equal exactly when the trees are. Given ``unit``: a name by the
    identity of its declaration (``by_name``: by its name and whether it is at file scope),
    a declaration by its name and canonical type, and an expression by its canonical type
    and implicit casts too."""
    if n is None:
        return None
    if unit is None:
        return (type(n).__name__, _attrs(n), tuple(key(c) for c in kids(n)))
    info = unit.sema.get(n)
    extra = _attrs(n)
    if isinstance(n, c_ast.ID) and info is not None and info.ref is not None:
        sym = unit.symbols.get(info.ref)
        extra = (n.name, sym.function is None if by_name else info.ref)
    if info is not None and info.type is not None:
        extra += (info.type.canon, tuple((c.kind, c.type.canon) for c in info.casts))
    sub = tuple(key(c, unit, by_name) for c in kids(n) if not isinstance(c, TYPES))
    if isinstance(n, (c_ast.Cast, c_ast.Typename)) or (
        isinstance(n, c_ast.UnaryOp) and isinstance(n.expr, c_ast.Typename)
    ):
        sub += tuple(key(c) for c in kids(n) if isinstance(c, TYPES))
    return (type(n).__name__, extra, sub)


def equal(a, b, unit=None):
    """Whether ``a`` and ``b`` are equal trees (:func:`key`)."""
    return key(a, unit) == key(b, unit)


def stored(n):
    """The operand ``n`` stores to when it is an assignment, increment or decrement."""
    if isinstance(n, c_ast.Assignment):
        return n.lvalue
    if isinstance(n, c_ast.UnaryOp) and n.op in INC_DEC:
        return n.expr
    return None


def variable(e):
    """The name an lvalue is, when it is a plain variable (``c_ast.ID``)."""
    return e if isinstance(e, c_ast.ID) else None


def calls(n):
    """Every call in ``n``."""
    return [c for c in walk(n) if isinstance(c, c_ast.FuncCall)]


def callee(call):
    """The name a call calls, None for a call through an expression."""
    return call.name.name if isinstance(call.name, c_ast.ID) else None


def arguments(call):
    """The argument expressions of a call."""
    return list(call.args.exprs) if call.args is not None else []


def subscripts(n):
    """``(node, base, index)`` of every array subscript in ``n``."""
    return [(c, c.name, c.subscript) for c in walk(n) if isinstance(c, c_ast.ArrayRef)]


def jumps(n):
    """Every ``goto``, ``break``, ``continue`` and ``return`` in ``n``."""
    return [c for c in walk(n) if isinstance(c, JUMPS)]


def labels(n):
    """The names of the labels in ``n``."""
    return {c.name for c in walk(n) if isinstance(c, c_ast.Label)}


def targets(n):
    """The labels the ``goto`` statements in ``n`` name."""
    return {c.name for c in walk(n) if isinstance(c, c_ast.Goto) and isinstance(c.name, str)}


def writes(n):
    """The variables ``n`` stores to: assigned, incremented or decremented, at any depth."""
    out = set()
    for c in walk(n):
        if isinstance(c, c_ast.Decl) and c.name and c.init is not None:
            out.add(c.name)
        var = variable(stored(c))
        if var is not None:
            out.add(var.name)
    return out


def reads(n):
    """The variables ``n`` reads: every name it mentions but a called function's and an
    assignment's target (a compound assignment's target is read)."""
    skips = {
        id(c.lvalue)
        for c in walk(n)
        if isinstance(c, c_ast.Assignment) and c.op == "=" and variable(c.lvalue) is not None
    }
    out, todo = set(), [n]
    while todo:
        c = todo.pop()
        if c is None or id(c) in skips or isinstance(c, TYPES):
            continue
        if isinstance(c, c_ast.ID):
            out.add(c.name)
        elif isinstance(c, c_ast.StructRef):
            todo.append(c.name)
        elif isinstance(c, c_ast.FuncCall) and isinstance(c.name, c_ast.ID):
            todo.append(c.args)
        else:
            todo += kids(c)
    return out


def names(n):
    """The variables ``n`` reads or writes."""
    return reads(n) | writes(n)
