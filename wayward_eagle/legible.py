"""Census checks of what C names and addresses opaquely, and of how it is laid out: opaque
identifiers, memory blocks standing in for variables, hardware at integer addresses, labels
reached only by jumps, and each function's size (docs/census.md)."""

from __future__ import annotations

from collections import Counter
from typing import NamedTuple

from pycparser import c_ast

from .c import ints, query as q
from .c.flow import build

HEX = frozenset("0123456789abcdefABCDEF")
#: The width of ``int`` constants are evaluated in.
BITS = 32


class Site(NamedTuple):
    """A defect: its class, the function holding it (``""`` at file scope), line, detail."""

    cls: str
    function: str
    line: int
    detail: str


# opaque-name.


def words(name):
    """``name``'s words: split at underscores, between letters and digits, and at camel case
    (before an upper-case letter after a lower-case one, or before the last upper-case
    letter of a run followed by a lower-case one), so ``f12_1`` is ``f``, ``12``, ``1`` and
    ``readHWReg`` is ``read``, ``HW``, ``Reg``."""
    out, word = [], ""
    for i, ch in enumerate(name):
        prev, nxt = name[i - 1] if i else "", name[i + 1 : i + 2]
        cut = word and (
            prev.isdigit() != ch.isdigit()
            or (prev.islower() and ch.isupper())
            or (prev.isupper() and ch.isupper() and nxt.islower())
        )
        if ch == "_" or cut:
            out += [word] if word else []
            word = ""
        word += "" if ch == "_" else ch
    return out + ([word] if word else [])


def meaningful(word):
    """Whether ``word`` carries meaning: two letters or more, not all hexadecimal digits (a
    single letter tags a kind, a number or hex tag an address)."""
    return len(word) > 1 and not word.isdigit() and not set(word) <= HEX


def opaque(names):
    """The names of ``names`` no word of which carries meaning (:func:`meaningful`) and some
    word of which is a number or hex tag: ``v13``, ``K_AF``, but not ``i``."""
    out = set()
    for n in names:
        ws = words(n)
        if not any(map(meaningful, ws)) and any(len(w) > 1 or w.isdigit() for w in ws):
            out.add(n)
    return out


def _here(unit, loc):
    """Whether location ``loc`` is spelled in ``unit``'s file outside a macro's body."""
    return loc is not None and loc.file == unit.path and (not loc.macro or loc.arg)


def identifiers(unit):
    """``{name: (line, function)}`` of each identifier ``unit``'s file names, where it first
    does: variables, functions and enumerators it declares or references, labels, and the
    object-like macros it expands."""
    out = {}
    for ext in unit.ast.ext:
        fn = ext.decl.name if isinstance(ext, c_ast.FuncDef) else ""
        for n in q.walk(ext):
            info = unit.sema.get(n)
            if info is None:
                continue
            if isinstance(n, c_ast.ID) and info.ref is not None and _here(unit, info.begin):
                at = n.name, info.begin.line
            elif isinstance(n, c_ast.Decl) and n.name and _here(unit, info.loc):
                at = n.name, info.loc.line
            elif isinstance(n, c_ast.Label) and _here(unit, info.begin):
                at = n.name, info.begin.line
            else:
                continue
            if at[0] not in out or at[1] < out[at[0]][0]:
                out[at[0]] = (at[1], fn)
    for n, name in unit.macros.items():
        line = unit.sema[n].begin.line
        if name not in out or line < out[name][0]:
            fn = next((f.decl.name for f in unit.functions if _within(unit, f, line)), "")
            out[name] = (line, fn)
    return out


def _within(unit, f, line):
    first, last = unit.span(f)
    return first <= line <= last


def opaque_names(unit):
    """``(sites, declared)``: an ``opaque-name`` site per distinct opaque identifier
    (:func:`opaque`) of ``unit``'s file at its first appearance, and how many it names."""
    names = identifiers(unit)
    sites = [Site("opaque-name", names[n][1], names[n][0], n) for n in sorted(opaque(names.keys()))]
    return sites, len(names)


# Addresses.


class Sum(NamedTuple):
    """An integer or address expression as a sum: its constant, its other terms ``(sign,
    node)``, and whether a literal spelled in the file outside a macro is in the constant."""

    constant: int
    terms: tuple
    literal: bool


def summed(unit, *exprs):
    """The :class:`Sum` of ``exprs`` added, through ``+``, ``-``, negation and casts to
    integer types; constant subexpressions evaluated."""
    const, terms, literal = 0, [], False
    todo = [(e, 1) for e in exprs]
    while todo:
        n, s = todo.pop()
        v = ints.value(n, {}, BITS)
        if v is not None:
            const += s * v[0]
            literal |= any(
                isinstance(x, c_ast.Constant) and _here(unit, unit.sema[x].begin)
                for x in q.walk(n)
                if x in unit.sema
            )
        elif isinstance(n, c_ast.BinaryOp) and n.op in ("+", "-"):
            todo += [(n.left, s), (n.right, -s if n.op == "-" else s)]
        elif isinstance(n, c_ast.UnaryOp) and n.op in ("+", "-"):
            todo.append((n.expr, -s if n.op == "-" else s))
        elif isinstance(n, c_ast.Cast) and ints.type_of(n.to_type, BITS) is not None:
            todo.append((n.expr, s))
        else:
            terms.append((s, n))
    return Sum(const, tuple(terms), literal)


def _array(unit, n):
    """The :class:`~wayward_eagle.c.Symbol` of the file-scope array ``n`` names, or None."""
    sym = unit.symbol(n) if isinstance(n, c_ast.ID) else None
    if sym is None or sym.kind != "VarDecl" or sym.function is not None:
        return None
    return sym if "[" in sym.type.canon else None


def _components(uses):
    """The number of objects distinct ``(index, offset)`` subscripts ``uses`` of one array
    touch: two are of one object when they share their offset (one base, two indices), or
    their index with offsets one apart (neighbouring elements, ``a[i]`` and ``a[i + 1]``)."""
    uses = sorted(uses, key=lambda u: u[1])
    parent = {u: u for u in uses}

    def find(u):
        while parent[u] != u:
            parent[u] = parent[parent[u]]
            u = parent[u]
        return u

    first, last = {}, {}
    for u in uses:
        index, off = u
        parent[find(u)] = find(first.setdefault(off, u))
        prev = last.get(index)
        if prev is not None and prev[1] == off - 1:
            parent[find(u)] = find(prev)
        last[index] = u
    return len({find(u) for u in uses})


def raw_memory(unit):
    """``raw-memory`` sites: every subscript of a file-scope array that is a memory block,
    one array standing in for many variables: its subscripts holding an index (a term that is
    not constant) touch more than one object (:func:`_components`)."""
    uses = {}
    for f in unit.functions:
        for n in q.walk(f):
            if not isinstance(n, c_ast.ArrayRef):
                continue
            s = summed(unit, n.name, n.subscript)
            arrays = [(i, _array(unit, t)) for i, (sign, t) in enumerate(s.terms) if sign > 0]
            arrays = [(i, a) for i, a in arrays if a is not None]
            if len(arrays) != 1:
                continue
            i, sym = arrays[0]
            index = frozenset(
                Counter(
                    (sign, q.key(t, unit, converted=False))
                    for j, (sign, t) in enumerate(s.terms)
                    if j != i
                ).items()
            )
            uses.setdefault(sym.id, (sym, []))[1].append((f, n, index, s.constant))
    sites = []
    for sym, found in uses.values():
        if _components({(index, off) for _, _, index, off in found if index}) > 1:
            sites += [
                Site("raw-memory", f.decl.name, unit.line(n), f"{sym.name} at {off:#x}")
                for f, n, _, off in found
            ]
    return sites


def _params(fd):
    args = fd.decl.type.args
    return [p for p in (args.params if args else ()) if isinstance(p, c_ast.Decl)]


def _pointed(unit, f, addressed):
    """``(expression, callee)`` of each integer ``f`` uses as an address: converted to a
    pointer (callee None), or passed as an argument ``addressed`` holds of."""
    for n in q.walk(f):
        info = unit.sema.get(n)
        if isinstance(n, c_ast.Cast) and info is not None and info.cast == "IntegralToPointer":
            yield n.expr, None
        elif info is not None and any(c.kind == "IntegralToPointer" for c in info.casts):
            yield n, None
        if isinstance(n, c_ast.FuncCall):
            name = q.callee(n)
            for i, a in enumerate(q.arguments(n)):
                if (name, i) in addressed:
                    yield a, name


def address_parameters(unit):
    """``{(function, index)}`` of the parameters of the file's functions that are addresses:
    one its function converts to a pointer, or passes as such a parameter, whole (under casts
    to integer types), a least fixed point over the call graph. A parameter added to a
    constant is an index, not an address."""
    params = {f: {unit.sema[p].ref: i for i, p in enumerate(_params(f))} for f in unit.functions}
    out, more = set(), True
    while more:
        more = False
        for f, own in params.items():
            for e, _ in list(_pointed(unit, f, out)):
                while isinstance(e, c_ast.Cast) and ints.type_of(e.to_type, BITS) is not None:
                    e = e.expr
                ref = unit.sema[e].ref if isinstance(e, c_ast.ID) and e in unit.sema else None
                if ref in own and (f.decl.name, own[ref]) not in out:
                    out.add((f.decl.name, own[ref]))
                    more = True
    return out


def raw_hardware(unit):
    """``raw-hardware`` sites: each integer used as an address (:func:`_pointed`) whose sum
    has a nonzero constant spelled as a literal: ``io_write(0xD401 + v, x)`` when
    ``io_write``'s first parameter is converted to a pointer."""
    addressed = address_parameters(unit)
    sites = []
    for f in unit.functions:
        for e, callee in _pointed(unit, f, addressed):
            s = summed(unit, e)
            if s.literal and s.constant:
                at = f"{s.constant:#x}" + (" + index" if s.terms else "")
                sites.append(
                    Site("raw-hardware", f.decl.name, unit.line(e), f"{callee or '*'} {at}")
                )
    return sites


# label-after-jump and size.


def labels_after_jump(unit, f):
    """``label-after-jump`` sites of function ``f``: each reachable label of its control-flow
    graph whose every predecessor is a ``goto``, nothing falling into it."""
    cfg = build(f.body, lambda call: getattr(unit.symbol(call), "noreturn", False), gotos=True)
    return [
        Site("label-after-jump", f.decl.name, unit.line(cfg.origin[x]), cfg.origin[x].name)
        for x in cfg.order
        if cfg.nodes[x] is None
        and isinstance(cfg.origin[x], c_ast.Label)
        and cfg.pred[x]
        and all(isinstance(cfg.nodes[p], c_ast.Goto) for p in cfg.pred[x])
    ]


def statements(unit, st):
    """The number of statements in statement ``st``, as clang counts them: a declaration
    statement declaring several variables one, labels, blocks and empty statements none."""
    if st is None or isinstance(st, c_ast.EmptyStatement):
        return 0
    if isinstance(st, c_ast.Compound):
        n, last = 0, None
        for x in q.statements(st):
            info = unit.sema.get(x)
            group = info.group if isinstance(x, c_ast.Decl) and info is not None else None
            n += statements(unit, x) if group is None or group != last else 0
            last = group
        return n
    own, subs = 1, ()
    if isinstance(st, (c_ast.Case, c_ast.Default)):
        own, subs = 0, st.stmts
    elif isinstance(st, c_ast.Label):
        own, subs = 0, (st.stmt,)
    elif isinstance(st, c_ast.If):
        subs = st.iftrue, st.iffalse
    elif isinstance(st, (*q.LOOPS, c_ast.Switch)):
        subs = (st.stmt,)
    return own + sum(statements(unit, x) for x in subs)


def size(unit):
    """``({function: lines}, {function: statements})`` of each function defined."""
    lines = {}
    for f in unit.functions:
        first, last = unit.span(f)
        lines[f.decl.name] = last - first + 1
    return lines, {f.decl.name: statements(unit, f.body) for f in unit.functions}
