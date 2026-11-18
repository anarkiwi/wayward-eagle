"""Readability-defect census of C: each site, by class, that clang-tidy's checks or the checks
here over clang's AST (as :mod:`wayward_eagle.c` holds it) find (docs/census.md)."""

from __future__ import annotations

import concurrent.futures
import functools
import glob
import json
import multiprocessing
import os
import pathlib
import subprocess
import sys
import tempfile
from bisect import bisect_right
from collections import Counter
from typing import NamedTuple

import clang_tidy
from pycparser import c_ast
import yaml
from tqdm import tqdm

from . import c
from .c import query as q
from .c.flow import Flow, bare
from .c.model import model as _model, promoted as _promoted, width as _width

COGNITIVE = "cognitive-complexity"
#: The class of each clang-tidy check's diagnostics.
TIDY = {
    "bugprone-branch-clone": "branch-clone",
    "readability-redundant-casting": "redundant-cast",
    "misc-redundant-expression": "redundant-expression",
    "readability-simplify-boolean-expr": "simplifiable-boolean",
    "readability-redundant-control-flow": "redundant-control-flow",
    "readability-else-after-return": "else-after-jump",
    "bugprone-assignment-in-if-condition": "assignment-in-if",
    "bugprone-inc-dec-in-conditions": "inc-dec-in-condition",
    "readability-avoid-nested-conditional-operator": "nested-conditional",
    "readability-misleading-indentation": "misleading-indentation",
    "bugprone-redundant-branch-condition": "redundant-branch-condition",
    "clang-analyzer-deadcode.DeadStores": "dead-store",
}
#: The classes the checks over clang's AST find.
AST = (
    "common-arm-statements",
    "subsumed-cast",
    "effect-in-condition",
    "status-call",
    "uncounted-loop",
    "goto",
    "duplicate-code",
    "copy-variable",
    "edge-copy",
)
#: The fewest statements a repeated sequence holds.
SEQUENCE = 2
CLASSES = (COGNITIVE, *TIDY.values(), *AST)
CONFIG = json.dumps({"Checks": ",".join(["-*", *TIDY])})
LABELS = (c_ast.Case, c_ast.Default)
UNMOVED = (c_ast.Decl, c_ast.DeclList, c_ast.Label, c_ast.EmptyStatement)
LOCALS = ("VarDecl", "ParmVarDecl")
COMPARE = frozenset(("==", "!=", "<", "<=", ">", ">="))
ARITH = frozenset("+-~")
#: The targets whose data models a cast must be a no-op on to be subsumed.
TARGETS = (c.HOST, c.NARROW)
real = functools.lru_cache(maxsize=None)(os.path.realpath)

CensusError = c.CError


class Site(NamedTuple):
    """A defect: its class, the function holding it (``""`` at file scope), line, detail."""

    cls: str
    function: str
    line: int
    detail: str


class Report(NamedTuple):
    """A file's sites, each function's cognitive complexity, and each function's number of
    automatic local variables."""

    sites: list
    complexity: dict
    locals: dict

    def counts(self):
        """Sites per class, every class included."""
        out = dict.fromkeys(CLASSES, 0)
        out.update(Counter(s.cls for s in self.sites))
        return out


def executable():
    """The pinned clang-tidy."""
    return pathlib.Path(clang_tidy.get_executable("clang-tidy"))


class Chain(NamedTuple):
    """A chain of ``case`` labels and the statement under the last, one statement to clang."""

    labels: list
    first: object


def groups(items, unit):
    """``items`` as clang's statements: each run of declarations one declaration statement
    declares a ``DeclList``, and each chain of labels with its first statement a
    :class:`Chain`, the statements after it items of their own."""
    out, last, chained = [], None, False
    for st in items:
        info = unit.sema.get(st)
        group = info.group if isinstance(st, c_ast.Decl) and info is not None else None
        if group is not None and group == last:
            out[-1].decls.append(st)
            continue
        last = group
        if group is not None:
            out.append(c_ast.DeclList([st]))
            unit.sema[out[-1]] = info
        elif isinstance(st, LABELS):
            if chained:
                out[-1].labels.append(st)
            else:
                out.append(Chain([st], None))
            chained = not st.stmts
            if st.stmts:
                out[-1] = out[-1]._replace(first=st.stmts[0])
                out += groups(st.stmts[1:], unit)
        else:
            out.append(st)
    return out


class Unit:  # pylint: disable=too-many-instance-attributes
    """One parsed C file and the census of its functions over clang's AST."""

    def __init__(self, path, args=()):
        self.path, self.args = real(str(path)), list(args)
        self.c = c.load(path, args=self.args)
        self.functions = self.c.functions
        self.spans = sorted((*self.c.span(f), f.decl.name) for f in self.functions)
        self.symbol_of = {f: self.c.sema[f].ref for f in self.functions}
        self.impure = self._impure()
        self.sites, self.fn, self.addressed, self.moved = [], None, set(), set()
        self.models = {c.HOST: _model(c.HOST)}

    def function_at(self, line):
        """The function whose definition spans ``line``, or ``""``."""
        i = bisect_right(self.spans, (line, sys.maxsize, "")) - 1
        return self.spans[i][2] if i >= 0 and self.spans[i][1] >= line else ""

    def census(self):
        """Sites of the checks over clang's AST, in every function defined in the file."""
        casts = []
        for f in self.functions:
            self._enter(f)
            self._visit(f, [], casts)
            self._duplicates(f)
            self._copies(f)
        if casts:
            narrow = Casts(c.load(self.path, args=self.args, target=c.NARROW))
            keep = narrow.subsumed()
            for site, k in casts:
                if k in keep:
                    self.sites.append(site)
        return self.sites

    def _enter(self, f):
        self.fn = f
        self.addressed = {
            self.c.sema[n.expr].ref
            for n in q.walk(f)
            if isinstance(n, c_ast.UnaryOp) and n.op == "&" and n.expr in self.c.sema
        }

    def line(self, n):
        """The line node ``n`` begins on."""
        return self.c.line(n)

    def add(self, cls, n, detail):
        """Record a site of ``cls`` at node ``n``."""
        self.add_line(cls, self.line(n), detail)

    def add_line(self, cls, line, detail):
        """Record a site of ``cls`` at ``line``."""
        self.sites.append(Site(cls, self.fn.decl.name, line, detail))

    def _visit(self, n, stack, casts):  # pylint: disable=too-many-branches
        """Check ``n`` (its ancestors ``stack``) and every node under it."""
        if isinstance(n, c_ast.Cast):
            got = Casts(self.c, self.models).subsumed_at(n, stack)
            if got is not None:
                casts.append(
                    (Site("subsumed-cast", self.fn.decl.name, self.line(n), got), _key(self.c, n))
                )
        elif isinstance(n, c_ast.Goto):
            self.add("goto", n, n.name if isinstance(n.name, str) else "indirect")
        elif isinstance(n, c_ast.Switch) or (isinstance(n, c_ast.If) and not _else_if(n, stack)):
            self._arms(n)
        if isinstance(n, (c_ast.If, *q.LOOPS)) and n.cond is not None:
            effects = self.effects(n.cond)
            status = self._status(n.cond) if effects else None
            if status is not None:
                self.add("status-call", n, f"call {status}")
            elif effects:
                self.add("effect-in-condition", n, ", ".join(effects))
        if isinstance(n, c_ast.Compound):
            sub = list(n.block_items or ())
            for i, st in enumerate(sub):
                if isinstance(st, q.LOOPS):
                    self._counted(st, groups(sub[:i], self.c))
        elif isinstance(n, LABELS):
            for i, st in enumerate(n.stmts):
                if isinstance(st, q.LOOPS):
                    self._counted(st, groups(n.stmts[:i], self.c))
        elif isinstance(n, q.LOOPS) and not (stack and isinstance(stack[-1], c_ast.Compound)):
            self._counted(n, [])
        stack.append(n)
        for k in q.kids(n):
            if not isinstance(k, q.TYPES):
                self._visit(k, stack, casts)
        stack.pop()

    # Effects.

    def callee(self, call):
        """The :class:`~wayward_eagle.c.Symbol` of the function a call names, or None."""
        sym = self.c.symbol(call)
        return sym if sym is not None and sym.kind == "FunctionDecl" else None

    def _impure(self):
        """Names of the defined functions a call to which may have an effect: one with an
        effect of its own, or calling one, or calling a function not defined here and
        declared neither ``pure`` nor ``const`` (a greatest fixed point)."""
        calls, impure = {}, set()
        for f in self.functions:
            name = f.decl.name
            calls[name] = set()
            for n in q.walk(f):
                if isinstance(n, c_ast.FuncCall):
                    sym = self.callee(n)
                    if sym is None:
                        impure.add(name)
                    elif not _pure(sym):
                        calls[name].add(sym.name)
                elif self._effect(n, f):
                    impure.add(name)
        more = True
        while more:
            more = False
            for f, callees in calls.items():
                if f not in impure and any(g in impure or g not in calls for g in callees):
                    impure.add(f)
                    more = True
        return impure

    def _effect(self, n, f):
        """Whether node ``n`` of function ``f`` has an effect beyond ``f``'s own automatic
        variables: a store elsewhere, a volatile access or inline assembly."""
        if isinstance(n, c.Opaque) and "Asm" in n.kind:
            return True
        info = self.c.sema.get(n)
        if info is not None and info.type is not None and _is_expr(info):
            if "volatile" in self.c.qualifiers(info.type):
                return True
        lhs = q.stored(n)
        if lhs is None:
            return False
        var = self.target(lhs)
        return not (
            var is not None
            and var.kind in LOCALS
            and var.storage != "static"
            and var.function == self.symbol_of[f]
        )

    def _call_effect(self, n):
        """Whether call ``n`` may have an effect."""
        sym = self.callee(n)
        if sym is None or sym.name in self.impure:
            return True
        return sym.name not in {f.decl.name for f in self.functions} and not _pure(sym)

    def target(self, lv):
        """The :class:`~wayward_eagle.c.Symbol` of the variable an lvalue names, or None."""
        var = q.variable(lv)
        return self.c.symbol(var) if var is not None else None

    def effects(self, n):
        """The effects of expression ``n``: stores, commas, and calls that may have one."""
        out = set()
        commas = {id(x) for x in q.commas(n)}
        for x in q.walk(n):
            lhs = q.stored(x)
            if lhs is not None:
                var = self.target(lhs)
                out.add(f"store {var.name}" if var is not None else "store")
            elif id(x) in commas:
                out.add("comma")
            elif isinstance(x, c_ast.FuncCall) and self._call_effect(x):
                out.add(f"call {q.callee(x) or ''}")
        return sorted(out)

    # status-call.

    def _status(self, cond):
        """The name of the function condition ``cond`` calls when it is only that call (under
        casts, ``!`` or a comparison with a constant) of a :meth:`status` function, its
        arguments without effects and no automatic variable of the caller addressed."""
        while True:
            if isinstance(cond, c_ast.Cast) or (isinstance(cond, c_ast.UnaryOp) and cond.op == "!"):
                cond = cond.expr
            elif isinstance(cond, c_ast.BinaryOp) and cond.op in COMPARE:
                if self.constant(cond.right):
                    cond = cond.left
                elif self.constant(cond.left):
                    cond = cond.right
                else:
                    return None
            else:
                break
        if not isinstance(cond, c_ast.FuncCall) or not self.status(q.callee(cond)):
            return None
        if any(self.effects(a) for a in q.arguments(cond)) or self._exposed():
            return None
        return q.callee(cond)

    @functools.cached_property
    def _statuses(self):
        """Names of the functions defined here returning an integer whose every ``return``
        returns a constant (an exit code)."""
        out = set()
        for f in self.functions:
            rets = [n.expr for n in q.walk(f) if isinstance(n, c_ast.Return)]
            if rets and all(
                r is not None and self.constant(r) and self.integer(self.c.sema[r].converted)
                for r in rets
            ):
                out.add(f.decl.name)
        return out

    def status(self, name):
        """Whether ``name`` is a function defined here returning only exit codes."""
        return name in self._statuses

    def _exposed(self):
        """Whether an automatic variable of this function is addressed, by ``&`` or as an
        array, so a callee might store to it."""
        fn = self.symbol_of[self.fn]
        return any(
            sym.kind in LOCALS
            and sym.function == fn
            and sym.storage not in ("static", "extern")
            and (sym.id in self.addressed or "[" in sym.type.canon)
            for sym in self.c.symbols.values()
        )

    # copy-variable and edge-copy.

    def _copyable(self, sym, rhs):
        """Whether ``sym = rhs`` copies ``rhs`` unchanged: a variable of ``sym``'s type, or a
        constant converted to ``sym``'s integer type."""
        if isinstance(rhs, c_ast.ID):
            other = self.c.symbol(rhs)
            if other is not None and other.kind in ("VarDecl", "ParmVarDecl"):
                return bare(other.type.canon) == bare(sym.type.canon)
        return self.integer(sym.type) and self.constant(rhs)

    def _copies(self, f):
        """copy-variable and edge-copy sites of function ``f``."""
        flow = Flow(self.c, f, self._call_effect, self._copyable)
        sym = self.c.symbols
        for d, _ in flow.copies():
            if sym[d.var].kind != "VarDecl":
                continue
            src = sym[d.copy[1]].name if d.copy[0] == "var" else self.c.source(_rhs(d.node))
            self.add("copy-variable", d.node, f"{sym[d.var].name} copies {src or 'a constant'}")
        for j, v, defs in flow.edge_copies():
            if all(id(d.node) in self.moved for d in defs):
                continue
            at = flow.cfg.nodes[j] if flow.cfg.origin[j] is None else flow.cfg.origin[j]
            lines = ", ".join(map(str, sorted(self.line(d.node) for d in defs)))
            self.add("edge-copy", at, f"{sym[v].name}: lines {lines}")

    def locals(self):
        """``{function: number of automatic variables it declares}``, parameters aside."""
        return {
            f.decl.name: sum(
                1
                for n in q.walk(f.body)
                if isinstance(n, c_ast.Decl)
                and (sym := self.c.symbol(n)) is not None
                and sym.kind == "VarDecl"
                and sym.storage not in ("static", "extern")
            )
            for f in self.functions
        }

    # duplicate-code.

    def _duplicates(self, f):  # pylint: disable=too-many-locals
        """Maximal sequences of at least ``SEQUENCE`` statements, equal but for the identity
        of the declarations they name, repeated within function ``f``."""
        ids, lists, where = {}, [], {}
        for n in q.walk(f):
            if not isinstance(n, c_ast.Compound):
                continue
            sts = groups(list(n.block_items or ()), self.c)
            seq = [ids.setdefault(self._seq_key(s), len(ids)) for s in sts]
            for i, x in enumerate(seq):
                where.setdefault(x, []).append((len(lists), i))
            lists.append((sts, seq))
        found = {}
        for spots in where.values():
            for j, (na, ia) in enumerate(spots):
                for nb, ib in spots[j + 1 :]:
                    size = _run(lists[na][1], ia, lists[nb][1], ib, na == nb)
                    if size >= SEQUENCE:
                        seq = tuple(lists[na][1][ia : ia + size])
                        found.setdefault(seq, set()).update(((na, ia), (nb, ib)))
        for seq, spots in found.items():
            lines = sorted(self._line(lists[n][0][i]) for n, i in spots)
            listed = ", ".join(map(str, lines))
            self.add_line("duplicate-code", lines[0], f"{len(seq)} statements at lines {listed}")

    def _seq_key(self, s):
        """A statement's key in a sequence: by name, an empty statement by its identity."""
        if isinstance(s, c_ast.EmptyStatement):
            return id(s)
        if isinstance(s, Chain):
            labels = tuple(q.key(getattr(x, "expr", None), self.c, True) for x in s.labels)
            return ("case", labels, q.key(s.first, self.c, True))
        return q.key(s, self.c, True)

    def _line(self, s):
        """The line statement ``s`` (a :class:`Chain` too) begins on."""
        return self.line(s.labels[0] if isinstance(s, Chain) else s)

    # common-arm-statements.

    def _arms(self, n):
        """The same statement leading or ending every arm of an if/else chain or switch."""
        if isinstance(n, c_ast.If):
            arms, conds, tail = [], [], n
            while isinstance(tail, c_ast.If):
                conds.append(tail.cond)
                arms.append(groups(q.statements(tail.iftrue), self.c))
                tail = tail.iffalse
            if tail is None:
                return
            arms.append(groups(q.statements(tail), self.c))
            sink = True
        else:
            got = _switch_arms(n, self.c)
            if got is None:
                return
            arms, sink = got
            conds = [n.cond]
        if not all(arms):
            return
        keys = [[q.key(s, self.c) for s in arm] for arm in arms]
        if all(k == keys[0] for k in keys):
            return
        first, last = arms[0][0], arms[0][-1]
        if (
            all(k[0] == keys[0][0] for k in keys)
            and not isinstance(first, UNMOVED)
            and self._hoistable(first, conds)
        ):
            self.add("common-arm-statements", n, f"leads every arm: line {self.line(first)}")
            self.moved.update(id(arm[0]) for arm in arms)
        if sink and all(k[-1] == keys[0][-1] for k in keys) and not isinstance(last, UNMOVED):
            self.add("common-arm-statements", n, f"ends every arm: line {self.line(last)}")
            self.moved.update(id(arm[-1]) for arm in arms)

    def _hoistable(self, st, conds):
        """Whether ``st`` may run before conditions ``conds``: they have no effect, ``st``
        stores to no variable they read, and, when they read more than this function's own
        variables, ``st`` stores to nothing else and calls nothing with an effect."""
        if any(self.effects(x) for x in conds):
            return False
        reads, memory = set(), False
        for n in (n for x in conds for n in q.walk(x)):
            sym = self.c.symbol(n) if isinstance(n, c_ast.ID) else None
            if sym is not None:
                reads.add(sym.id)
                memory |= not self._own(sym)
            memory |= isinstance(n, (c_ast.ArrayRef, c_ast.StructRef, c_ast.FuncCall))
            memory |= isinstance(n, c_ast.UnaryOp) and n.op == "*"
        for n in q.walk(st):
            if isinstance(n, c_ast.FuncCall) and memory and self._call_effect(n):
                return False
            lhs = q.stored(n)
            var = self.target(lhs) if lhs is not None else None
            if lhs is not None and (var is None or not self._own(var)) and memory:
                return False
            if var is not None and var.id in reads:
                return False
        return True

    def _own(self, var):
        """Whether ``var`` is a parameter or automatic variable of this function whose
        address is never taken."""
        return (
            var.kind in LOCALS
            and var.function == self.symbol_of[self.fn]
            and var.storage != "static"
            and var.id not in self.addressed
        )

    # uncounted-loop.

    def _counted(self, loop, before):  # pylint: disable=too-many-locals
        """A loop with a variable set to a constant before it, stepped by a constant once
        per iteration and tested by it, that is no ``for`` of that init, test and step."""
        if any(isinstance(n, (c_ast.Goto, c_ast.Label)) for n in q.walk(loop)):
            return
        parts = _loop_parts(loop)
        init, cond, inc, body = parts
        exits = [
            s.cond
            for s in q.statements(body)
            if isinstance(s, c_ast.If)
            and s.iffalse is None
            and [type(x) for x in q.statements(s.iftrue)] == [c_ast.Break]
        ]
        tests = [v for t in [cond, *exits] if t is not None for v in self._tested(t)]
        in_cond = {v.id for v in self._tested(cond)} if cond is not None else set()
        seen = set()
        for var in tests:
            if var is None or var.id in seen or not self._local(var):
                continue
            seen.add(var.id)
            steps = [
                (n, part)
                for part in (cond, inc, body)
                if part is not None
                for n in q.walk(part)
                if q.stored(n) is not None and _same(self.target(q.stored(n)), var)
            ]
            if len(steps) != 1 or not self._step(steps[0][0], var):
                continue
            step, part = steps[0]
            if not self._each_iteration(step, part, parts):
                continue
            mentions = init is not None and any(self._mentions(n, var) for n in q.walk(init))
            start = self._init(var, init) if mentions else self._preceding(var, before)
            if start is None or (start is init and part is inc and var.id in in_cond):
                continue
            self.add(
                "uncounted-loop",
                loop,
                f"{var.name}: set line {self.line(start)}, stepped line {self.line(step)}",
            )

    def _tested(self, n):
        """The declarations expression ``n`` reads outside its stores."""
        if q.stored(n) is not None:
            return []
        if isinstance(n, c_ast.ID):
            sym = self.c.symbol(n)
            return [sym] if sym is not None else []
        return [v for k in q.kids(n) if not isinstance(k, q.TYPES) for v in self._tested(k)]

    def _local(self, var):
        """Whether ``var`` is an automatic integer variable of this function whose address
        is never taken."""
        return var.kind == "VarDecl" and self._own(var) and self.integer(var.type)

    def integer(self, typ):
        """Whether :class:`~wayward_eagle.c.Type` ``typ`` is an integer type."""
        return _width(self.models[c.HOST], typ) is not None

    def _each_iteration(self, step, part, parts):
        """Whether ``step``, in loop part ``part``, runs once in every iteration."""
        _, cond, inc, body = parts
        if part is inc:
            return True
        if part is cond:
            return _always(cond, step)
        if not any(s is step for s in q.statements(body)):
            return False
        at = self.c.sema[step].begin.offset
        return not any(
            isinstance(n, c_ast.Continue) and self.c.sema[n].begin.offset < at
            for n in q.walk(body, stop=q.LOOPS)
        )

    def _mentions(self, n, var):
        """Whether node ``n`` declares or names ``var``."""
        if isinstance(n, (c_ast.Decl, c_ast.ID)) and n in self.c.sema:
            return self.c.sema[n].ref == var.id
        return False

    def constant(self, n):
        """Whether expression ``n`` reads no object and has no effect."""
        for x in q.walk(n):
            if isinstance(x, c_ast.FuncCall) or q.stored(x) is not None:
                return False
            if isinstance(x, c_ast.ID) and x in self.c.sema:
                sym = self.c.symbol(x)
                if sym is None or sym.kind != "EnumConstantDecl":
                    return False
        return True

    def value_of(self, n):
        """``n`` under casts between integer types."""
        while isinstance(n, c_ast.Cast) and self.integer(self.c.sema[n].type):
            n = n.expr
        return n

    def _step(self, n, var):
        """Whether store ``n`` adds a constant to ``var``: ``++``, ``--``, ``+=``, ``-=`` or
        ``var = var ± constant``."""
        if isinstance(n, c_ast.UnaryOp):
            return True
        if n.op in ("+=", "-="):
            return self.constant(n.rvalue)
        rhs = self.value_of(n.rvalue)
        if n.op != "=" or not isinstance(rhs, c_ast.BinaryOp) or rhs.op not in ("+", "-"):
            return False
        a, b = self.value_of(rhs.left), self.value_of(rhs.right)
        if _same(self.target(a), var) and self.constant(b):
            return True
        return rhs.op == "+" and _same(self.target(b), var) and self.constant(a)

    def _init(self, var, st):
        """``st`` if it sets ``var`` to a constant, else None."""
        if isinstance(st, (c_ast.DeclList, c_ast.Decl)):
            for d in st.decls if isinstance(st, c_ast.DeclList) else [st]:
                if self.c.sema[d].ref == var.id:
                    return st if d.init is not None and self.constant(d.init) else None
            return None
        if isinstance(st, c_ast.Assignment) and st.op == "=":
            if _same(self.target(st.lvalue), var) and self.constant(st.rvalue):
                return st
        return None

    def _preceding(self, var, before):
        """The last statement of ``before`` mentioning ``var``, if it sets ``var`` to a
        constant and no label lies after it."""
        for st in reversed(before):
            nodes = list(q.walk(st))
            if any(isinstance(n, c_ast.Label) for n in nodes):
                return None
            if any(self._mentions(n, var) for n in nodes):
                return self._init(var, st)
        return None

    # clang-tidy.

    def tidied(self, run):
        """Sites of the clang-tidy run :class:`Tidy` ``run``: each diagnostic of a check of
        :data:`TIDY` in this file, the other lines of this file its notes point at its
        detail; :class:`CensusError` for any error."""
        diags = run.wait()
        lines = [0, *(i + 1 for i, b in enumerate(self.c.text) if b == 10)]

        def where(d):
            here = d.get("FilePath") and real(d["FilePath"]) == self.path
            return bisect_right(lines, d["FileOffset"]) if here else None

        sites = []
        for d in diags:
            msg = d["DiagnosticMessage"]
            if d.get("Level") == "Error":
                raise CensusError(f"{msg.get('FilePath')}: {msg['Message']}")
            line = where(msg)
            if d["DiagnosticName"] not in TIDY or line is None:
                continue
            notes = {where(n) for n in d.get("Notes") or ()} - {None, line}
            text = msg["Message"]
            if notes:
                text += ": lines " + ", ".join(map(str, sorted(notes)))
            sites.append(Site(TIDY[d["DiagnosticName"]], self.function_at(line), line, text))
        if run.returncode:
            raise CensusError(run.stderr.strip())
        return sites

    def complexity(self):
        """``(sites, {function: score})`` of each function's cognitive complexity
        (:func:`wayward_eagle.c.query.cognitive`), those above zero, at its name."""
        sites, scores = [], {}
        for f in self.functions:
            score = q.cognitive(f.body)
            if score:
                name = f.decl.name
                scores[name] = score
                sites.append(Site(COGNITIVE, name, self.c.sema[f].loc.line, str(score)))
        return sites, scores

    def tidy(self, args=()):
        """Sites of clang-tidy's checks and of cognitive complexity, and each function's
        cognitive complexity."""
        sites, scores = self.complexity()
        return self.tidied(Tidy(self.path, args)) + sites, scores


# subsumed-cast.


def _no_op(model, role, to, frm, into):
    """Whether a cast to ``to`` of a value of ``frm`` used as ``role`` (converted to
    ``into``) is a no-op in ``model``."""
    if to.holds(frm):
        return role != "promoted" or _promoted(model, frm) == _promoted(model, to)
    return role == "converted" and to.wraps and into.wraps and to.bits >= into.bits


class Casts:
    """The subsumed-cast check over one parse of a file: its integer casts whose removal
    leaves the value of the expression they are in unchanged in the parse's data model."""

    def __init__(self, unit, models=None):
        self.c = unit
        self.model = (models or {}).get(unit.target) or _model(unit.target)

    def subsumed(self):
        """The keys (:func:`_key`) of every subsumed cast of the file's functions."""
        out = set()

        def visit(n, stack, fn):
            if isinstance(n, c_ast.Cast) and self.subsumed_at(n, stack) is not None:
                out.add(_key(self.c, n))
            stack.append(n)
            for k in q.kids(n):
                if not isinstance(k, q.TYPES):
                    visit(k, stack, fn)
            stack.pop()

        for f in self.c.functions:
            visit(f, [], f)
        return out

    def subsumed_at(self, n, stack):
        """The detail of cast ``n`` (its ancestors ``stack``) when it is
        subsumed, else None."""
        info = self.c.sema[n]
        sub = self.c.sema.get(n.expr)
        if info.cast != "IntegralCast" or sub is None or info.type.canon == sub.type.canon:
            return None
        to, frm = _width(self.model, info.type), _width(self.model, sub.type)
        if to is None or frm is None or not stack:
            return None
        role, into = self._context(stack, n)
        if role is None or (role == "converted" and into is None):
            return None
        if not _no_op(self.model, role, to, frm, into or to):
            return None
        return f"({info.type.qual}) of {sub.type.qual}"

    def _converted(self, n):
        """``("converted", Int)`` of the type clang converts ``n``'s value to."""
        return "converted", _width(self.model, self.c.sema[n].converted)

    def _context(self, stack, below):  # pylint: disable=too-many-return-statements
        """How the parent of ``below`` uses it: ``("converted", Int)`` converts it to a type
        the parent fixes, ``"promoted"`` promotes it as an arithmetic operand, ``"value"``
        reads only its value; None any other use."""
        a = stack[-1]
        if isinstance(a, c_ast.Cast):
            return "converted", _width(self.model, self.c.sema[a].type)
        if isinstance(a, (c_ast.Decl, c_ast.Return)):
            return self._converted(below)
        if isinstance(a, c_ast.ExprList):
            if len(stack) > 1 and isinstance(stack[-2], c_ast.FuncCall) and stack[-2].args is a:
                return self._argument(stack[-2], below)
            return None, None
        if isinstance(a, c_ast.Assignment):
            if a.lvalue is below:
                return None, None
            return self._converted(below) if a.op == "=" else ("promoted", None)
        if isinstance(a, c_ast.BinaryOp):
            return ("value" if a.op in ("&&", "||") else "promoted"), None
        if isinstance(a, c_ast.UnaryOp):
            return ("promoted" if a.op in ARITH else "value" if a.op == "!" else None), None
        if isinstance(a, c_ast.TernaryOp):
            return ("value" if a.cond is below else "promoted"), None
        if isinstance(a, (c_ast.If, c_ast.Switch, *q.LOOPS)):
            return ("value" if a.cond is below else None), None
        if isinstance(a, c_ast.ArrayRef):
            return ("value" if a.subscript is below else None), None
        return None, None

    def _argument(self, call, arg):
        """``("converted", Int)`` of argument ``arg`` of a call to a prototyped function
        within its parameters, else ``(None, None)``."""
        sym = self.c.symbol(call)
        if sym is None or sym.kind != "FunctionDecl":
            return None, None
        ftype = self.c.tree(sym.type)
        params = ftype.args.params if isinstance(ftype, c_ast.FuncDecl) and ftype.args else None
        if not params or isinstance(params[-1], c_ast.EllipsisParam):
            return None, None
        if len(params) == 1 and _is_void(params[0]):
            return None, None
        n = next(i for i, x in enumerate(q.arguments(call)) if x is arg)
        return self._converted(arg) if n < len(params) else (None, None)


def _is_void(param):
    t = getattr(param, "type", None)
    return isinstance(t, c_ast.TypeDecl) and getattr(t.type, "names", None) == ["void"]


def _key(unit, n):
    """A cast's identity across parses of one file: where its first token is spelled and
    where it expands."""
    info = unit.sema[n]
    s, b = info.spelling, info.begin
    return (s.file, s.offset, b.file, b.offset)


def _is_expr(info):
    """Whether :class:`~wayward_eagle.c.Info` ``info`` is of an expression."""
    return info.kind.endswith(("Expr", "Operator", "Literal"))


def _pure(sym):
    """Whether function ``sym`` is declared ``pure`` or ``const``."""
    return bool(sym.attrs & {"PureAttr", "ConstAttr"})


def _rhs(n):
    """The value an assignment or initialisation stores."""
    return n.init if isinstance(n, c_ast.Decl) else n.rvalue


def _same(a, b):
    """Whether symbols ``a`` and ``b`` are one."""
    return a is not None and b is not None and a.id == b.id


def _loop_parts(n):
    """``(init, cond, step, body)`` of a loop, None for each part it lacks."""
    if isinstance(n, c_ast.For):
        return n.init, n.cond, n.next, n.stmt
    return None, n.cond, None, n.stmt


def _run(a, ia, b, ib, one):
    """The length of the common run of ``a`` from ``ia`` and ``b`` from ``ib`` (``one`` when
    they are one list, the run ending before ``ib``), 0 when it extends back past either."""
    if ia and ib and a[ia - 1] == b[ib - 1]:
        return 0
    end = min(len(a) - ia, len(b) - ib, ib - ia if one else len(b))
    size = 0
    while size < end and a[ia + size] == b[ib + size]:
        size += 1
    return size


def _else_if(n, stack):
    """Whether ``if`` ``n`` is the ``else`` of the ``if`` holding it."""
    return bool(stack) and isinstance(stack[-1], c_ast.If) and stack[-1].iffalse is n


def _always(cond, step):
    """Whether ``step`` runs in every evaluation of ``cond`` that continues the loop: it lies
    under only ``&&``, commas and casts."""
    if cond is step:
        return True
    if isinstance(cond, (c_ast.Cast, c_ast.ExprList)) or (
        isinstance(cond, c_ast.BinaryOp) and cond.op == "&&"
    ):
        return any(_always(k, step) for k in q.kids(cond) if not isinstance(k, q.TYPES))
    return False


def _switch_arms(n, unit):
    """``(arms, sinkable)`` of a switch with a ``default`` whose arms but the last end in a
    jump: each arm its statements less a trailing ``break``, sinkable when every arm ends by
    ``break`` or by falling off the end. None for any other switch."""
    arms, default, chained = [], False, False
    for st in q.statements(n.stmt):
        if not isinstance(st, LABELS):
            if not arms:
                return None
            arms[-1].append(st)
            continue
        default |= isinstance(st, c_ast.Default)
        if not chained:
            arms.append([])
        chained = not st.stmts
        arms[-1] += st.stmts
    arms = [groups(a, unit) for a in arms]
    if not default or not all(arms) or any(not isinstance(a[-1], q.JUMPS) for a in arms[:-1]):
        return None
    sink = all(isinstance(a[-1], c_ast.Break) for a in arms[:-1]) and not isinstance(
        arms[-1][-1], (c_ast.Return, c_ast.Continue, c_ast.Goto)
    )
    return [a[:-1] if isinstance(a[-1], c_ast.Break) else a for a in arms], sink


class Tidy:  # pylint: disable=too-few-public-methods
    """clang-tidy's checks run on a file in the background, its diagnostics exported as
    YAML (``--export-fixes``) and read at :meth:`wait`."""

    def __init__(self, path, args=()):
        fd, self.fixes = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        self.proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [
                str(executable()),
                real(str(path)),
                "--quiet",
                f"--config={CONFIG}",
                f"--export-fixes={self.fixes}",
                "--",
                *args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.stderr, self.returncode, self.diagnostics = "", None, None

    def wait(self):
        """The run's diagnostics, once it ends (``stderr`` and ``returncode`` then)."""
        if self.returncode is None:
            self.stderr = self.proc.communicate()[1]
            self.returncode = self.proc.returncode
            with open(self.fixes, encoding="utf-8") as f:
                data = yaml.load(f, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            os.unlink(self.fixes)
            self.diagnostics = (data or {}).get("Diagnostics") or []
        return self.diagnostics


def census(path, include=(), args=()):
    """The ``Report`` of C file ``path``, its headers found in directories ``include``."""
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    args = [*(f"-I{d}" for d in include), *args]
    run = Tidy(path, args)
    try:
        unit = Unit(path, args)
        ast = unit.census()
    finally:
        run.wait()
    sites, scores = unit.complexity()
    sites += unit.tidied(run) + ast
    return Report(sorted(sites, key=lambda s: (s.line, s.cls, s.detail)), scores, unit.locals())


def census_all(paths, include=(), jobs=None, quiet=False):
    """``{path: Report}`` of every file of ``paths``, ``jobs`` at a time."""
    paths = list(paths)
    jobs = min(jobs or len(os.sched_getaffinity(0)), max(len(paths), 1))
    out = {}
    ctx = multiprocessing.get_context("forkserver")
    with concurrent.futures.ProcessPoolExecutor(jobs, mp_context=ctx) as pool:
        futures = {pool.submit(census, p, tuple(include)): p for p in paths}
        done = concurrent.futures.as_completed(futures)
        for fut in tqdm(done, total=len(paths), desc="census", unit="file", disable=quiet):
            out[futures[fut]] = fut.result()
    return {p: out[p] for p in paths}


def adapter(options):
    """virtuous-cycles adapter ``wayward-census``: site counts per key (docs/census.md).

    Options ``files`` (globs), ``include`` (directories), ``key`` (template over ``stem``,
    ``path``, ``class`` and ``function``), ``total`` (a file's complexity key) and ``locals``
    (a file's number of local variables key, empty for none)."""
    files = sorted({p for g in options["files"] for p in glob.glob(g, recursive=True)})
    if not files:
        raise ValueError(f"no files match {options['files']}")
    template = options.get("key", "{stem}:{class}")
    total = options.get("total", "{stem}:cognitive-complexity")
    local = options.get("locals", "{stem}:locals")
    out = Counter()
    for path, rep in census_all(files, options.get("include", ())).items():
        names = {"stem": pathlib.Path(path).stem, "path": path}
        if "{function}" not in template:
            for cls in CLASSES[1:]:
                out[template.format(function="", **names, **{"class": cls})] += 0
        for s in rep.sites:
            if s.cls != COGNITIVE:
                out[template.format(function=s.function, **names, **{"class": s.cls})] += 1
        out[total.format(**names)] += sum(rep.complexity.values())
        if local:
            out[local.format(**names)] += sum(rep.locals.values())
    return dict(out)
