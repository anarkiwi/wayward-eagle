"""Readability-defect census of C: each site, by class, that clang-tidy's checks or the checks
here over clang's AST find (docs/census.md)."""

from __future__ import annotations

import concurrent.futures
import ctypes
import functools
import glob
import json
import multiprocessing
import os
import pathlib
import re
import subprocess
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from typing import NamedTuple

import clang_tidy
from clang import cindex as ci
from tqdm import tqdm

K = ci.CursorKind
TK = ci.TypeKind

COGNITIVE = "cognitive-complexity"
#: The class of each clang-tidy check's diagnostics.
TIDY = {
    "readability-function-cognitive-complexity": COGNITIVE,
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
    "uncounted-loop",
    "goto",
    "duplicate-code",
)
#: The fewest statements a repeated sequence holds.
SEQUENCE = 2
CLASSES = tuple(TIDY.values()) + AST
CONFIG = json.dumps(
    {
        "Checks": ",".join(["-*", *TIDY]),
        "CheckOptions": {
            "readability-function-cognitive-complexity.Threshold": 0,
            "readability-function-cognitive-complexity.DescribeBasicIncrements": False,
        },
    }
)
DIAG = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<level>warning|error|note): (?P<msg>.*?)"
    r"(?: \[(?P<check>[^\]]+)\])?$"
)
SCORE = re.compile(r"function '(\w+)' has cognitive complexity of (\d+)")
LOOPS = (K.WHILE_STMT, K.DO_STMT, K.FOR_STMT)
JUMPS = (K.BREAK_STMT, K.RETURN_STMT, K.CONTINUE_STMT, K.GOTO_STMT, K.INDIRECT_GOTO_STMT)
UNMOVED = (K.DECL_STMT, K.LABEL_STMT, K.NULL_STMT)
#: Binary operator spellings that store to their left operand.
STORES = frozenset("= *= /= %= += -= <<= >>= &= ^= |=".split())
#: libclang's unary operator kinds: increments and decrements, ``&``, ``*``, ``+ - ~``, ``!``.
INC_DEC, ADDR, DEREF, ARITH, LNOT = frozenset((1, 2, 3, 4)), 5, 6, frozenset((7, 8, 9)), 10
SIGNED = frozenset((TK.SCHAR, TK.CHAR_S, TK.SHORT, TK.INT, TK.LONG, TK.LONGLONG, TK.INT128))
CXSTRING = ci._CXString  # pylint: disable=protected-access
real = functools.lru_cache(maxsize=None)(os.path.realpath)


class CensusError(RuntimeError):
    """A file the compiler rejects."""


class Site(NamedTuple):
    """A defect: its class, the function holding it (``""`` at file scope), line, detail."""

    cls: str
    function: str
    line: int
    detail: str


class Report(NamedTuple):
    """A file's sites, and each function's cognitive complexity."""

    sites: list
    complexity: dict

    def counts(self):
        """Sites per class, every class included."""
        out = dict.fromkeys(CLASSES, 0)
        out.update(Counter(s.cls for s in self.sites))
        return out


class Int(NamedTuple):
    """An integer type's range, width, and whether conversion to it wraps modulo its width."""

    lo: int
    hi: int
    bits: int
    wraps: bool

    def holds(self, other):
        """Whether every value of ``other`` is a value of this type."""
        return self.lo <= other.lo and other.hi <= self.hi


#: The widths of ``int`` the conversion rules are held over.
INT_BITS = (16, 32, 64)
EXACT = re.compile(r"(u?)int(8|16|32|64)_t")
#: The least width of each standard integer type.
WIDTH = dict.fromkeys((TK.SCHAR, TK.CHAR_S, TK.UCHAR, TK.CHAR_U), 8)
WIDTH.update(dict.fromkeys((TK.SHORT, TK.USHORT, TK.INT, TK.UINT), 16))
WIDTH.update(dict.fromkeys((TK.LONG, TK.ULONG), 32))
WIDTH.update(dict.fromkeys((TK.LONGLONG, TK.ULONGLONG), 64))
WIDTH.update(dict.fromkeys((TK.INT128, TK.UINT128), 128))
#: The types at least as wide as ``int``, whatever its width.
NATIVE = frozenset((TK.INT, TK.UINT, TK.LONG, TK.ULONG))


def executable():
    """The pinned clang-tidy."""
    return pathlib.Path(clang_tidy.get_executable("clang-tidy"))


def resource_dir():
    """The pinned clang-tidy's builtin headers, shared with libclang so both parse alike."""
    return next((executable().parents[1] / "lib" / "clang").iterdir())


def _sized(bits, signed):
    """The ``Int`` of a ``bits``-wide integer type, ``signed`` or not."""
    if signed:
        return Int(-(1 << (bits - 1)), (1 << (bits - 1)) - 1, bits, True)
    return Int(0, (1 << bits) - 1, bits, True)


def _int(t):
    """Type ``t`` as an ``Int`` at each width of ``int`` of ``INT_BITS``: an exact-width
    typedef at its width, a standard type at its least width but ``int`` and ``long`` at
    least the width of ``int``. None for a type no integer."""
    while t.kind in (TK.ELABORATED, TK.TYPEDEF):
        if t.kind == TK.ELABORATED:
            t = t.get_named_type()
            continue
        exact = EXACT.fullmatch(t.get_declaration().spelling)
        if exact:
            return (_sized(int(exact[2]), not exact[1]),) * len(INT_BITS)
        t = t.get_declaration().underlying_typedef_type
    t = t.get_canonical()
    if t.kind == TK.ENUM:
        return _int(t.get_declaration().enum_type)
    if t.kind == TK.BOOL:
        return (Int(0, 1, 1, False),) * len(INT_BITS)
    if t.kind not in WIDTH:
        return None
    least = WIDTH[t.kind]
    return tuple(
        _sized(max(least, w) if t.kind in NATIVE else least, t.kind in SIGNED) for w in INT_BITS
    )


def _promoted(t, w):
    """``Int`` ``t`` after the integer promotions with an ``int`` ``w`` bits wide."""
    native = _sized(w, True)
    return native if native.holds(t) else t


@functools.cache
def _operators():
    """libclang's binary and unary operator kind functions, and the binary spellings."""
    lib = ci.conf.lib
    for name in ("clang_getCursorBinaryOperatorKind", "clang_getCursorUnaryOperatorKind"):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [ci.Cursor], ctypes.c_int
    spell = lib.clang_getBinaryOperatorKindSpelling
    spell.argtypes, spell.restype = [ctypes.c_int], CXSTRING
    names = [""] + [CXSTRING.from_result(spell(k)) for k in range(1, 34)]
    return lib.clang_getCursorBinaryOperatorKind, lib.clang_getCursorUnaryOperatorKind, names


def binop(c):
    """The spelling of binary operator ``c`` (``=``, ``+=``, ``&&``, ``,``, ...)."""
    kind, _, names = _operators()
    return names[kind(c)]


def unop(c):
    """libclang's kind of unary operator ``c``."""
    return _operators()[1](c)


def kids(c):
    """The children of ``c``."""
    return list(c.get_children())


def strip(c):
    """``c`` under parentheses and implicit conversions."""
    while c.kind in (K.PAREN_EXPR, K.UNEXPOSED_EXPR):
        sub = kids(c)
        if len(sub) != 1:
            break
        c = sub[0]
    return c


def value_of(c):
    """``c`` under parentheses, implicit conversions and casts between integer types."""
    while True:
        c = strip(c)
        if c.kind != K.CSTYLE_CAST_EXPR or _int(c.type) is None:
            return c
        c = kids(c)[-1]


def statements(c):
    """The statements of a compound statement, or ``[c]`` for any other statement."""
    return kids(c) if c.kind == K.COMPOUND_STMT else [c]


def walk(c, stop=()):
    """``c`` and every node under it, not entering nodes whose kind is in ``stop``."""
    yield c
    for k in c.get_children():
        if k.kind not in stop:
            yield from walk(k, stop)


def key(c, by_name=False):
    """``c``'s structure: equal exactly when the ASTs are equal, a reference by the identity
    of its declaration, or ``by_name`` by its name and whether it is at file scope."""
    kind, extra = c.kind, ()
    if kind in (K.DECL_REF_EXPR, K.MEMBER_REF_EXPR):
        ref = c.referenced
        if ref is None or not by_name:
            extra = (c.spelling, ref.hash if ref else None)
        else:
            extra = (c.spelling, ref.semantic_parent.kind == K.TRANSLATION_UNIT)
    elif kind in (K.INTEGER_LITERAL, K.CHARACTER_LITERAL, K.FLOATING_LITERAL, K.STRING_LITERAL):
        extra = tuple(t.spelling for t in c.get_tokens())
    elif kind in (K.BINARY_OPERATOR, K.COMPOUND_ASSIGNMENT_OPERATOR):
        extra = (binop(c),)
    elif kind == K.UNARY_OPERATOR:
        extra = (unop(c),)
    elif kind in (K.GOTO_STMT, K.LABEL_STMT, K.VAR_DECL):
        extra = (c.spelling,)
    if kind.is_expression() or kind == K.VAR_DECL:
        extra += (c.type.get_canonical().spelling,)
    return (kind.value, extra, tuple(key(k, by_name) for k in c.get_children()))


def stored(c):
    """The operand ``c`` stores to, if it is an assignment, increment or decrement."""
    if c.kind in (K.BINARY_OPERATOR, K.COMPOUND_ASSIGNMENT_OPERATOR) and binop(c) in STORES:
        return kids(c)[0]
    if c.kind == K.UNARY_OPERATOR and unop(c) in INC_DEC:
        return kids(c)[0]
    return None


def constant(c):
    """Whether expression ``c`` reads no object and has no effect."""
    for n in walk(c):
        if n.kind == K.CALL_EXPR or stored(n) is not None:
            return False
        if n.kind == K.DECL_REF_EXPR and n.referenced.kind != K.ENUM_CONSTANT_DECL:
            return False
    return True


def target(c):
    """The variable an lvalue names, when it is a plain variable."""
    c = strip(c)
    return c.referenced if c.kind == K.DECL_REF_EXPR else None


def same(a, b):
    """Whether declarations ``a`` and ``b`` are one."""
    return a is not None and b is not None and a == b


def declared_pure(fn):
    """Whether function ``fn`` is declared ``pure`` or ``const``."""
    decls = (fn, fn.get_definition(), fn.canonical)
    return any(
        k.kind in (K.PURE_ATTR, K.CONST_ATTR)
        for d in decls
        if d is not None
        for k in d.get_children()
    )


class Unit:  # pylint: disable=too-many-instance-attributes
    """One parsed C file and the census of its functions over clang's AST."""

    def __init__(self, path, args=()):
        self.path = real(str(path))
        tu = ci.Index.create().parse(str(path), args=[*args, "-resource-dir", str(resource_dir())])
        errors = [str(d) for d in tu.diagnostics if d.severity >= ci.Diagnostic.Error]
        if errors:
            raise CensusError("\n".join(errors))
        self.tu = tu
        self.functions = [
            c
            for c in tu.cursor.get_children()
            if c.kind == K.FUNCTION_DECL
            and c.is_definition()
            and c.location.file
            and real(c.location.file.name) == self.path
        ]
        self.defined = {f.spelling for f in self.functions}
        self.spans = sorted(
            (f.extent.start.line, f.extent.end.line, f.spelling) for f in self.functions
        )
        toks = [
            (t.extent.start.offset, t.spelling)
            for t in tu.get_tokens(extent=tu.cursor.extent)
            if t.location.file and real(t.location.file.name) == self.path
        ]
        self.offsets = [o for o, _ in toks]
        self.words = [w for _, w in toks]
        self.impure = self._impure()
        self.sites, self.fn, self.addressed = [], None, set()

    def function_at(self, line):
        """The function whose definition spans ``line``, or ``""``."""
        i = bisect_right(self.spans, (line, sys.maxsize, "")) - 1
        return self.spans[i][2] if i >= 0 and self.spans[i][1] >= line else ""

    def census(self):
        """Sites of the checks over clang's AST, in every function defined in the file."""
        for f in self.functions:
            self.fn = f
            self.addressed = {
                target(kids(n)[0]).hash
                for n in walk(f)
                if n.kind == K.UNARY_OPERATOR and unop(n) == ADDR and target(kids(n)[0])
            }
            self._visit(f, [])
            self._duplicates(f)
        return self.sites

    def add(self, cls, c, detail):
        """Record a site of ``cls`` at node ``c``."""
        self.add_line(cls, c.location.line, detail)

    def add_line(self, cls, line, detail):
        """Record a site of ``cls`` at ``line``."""
        self.sites.append(Site(cls, self.fn.spelling, line, detail))

    def _visit(self, c, stack):
        """Check ``c`` (its ancestors ``stack``) and every node under it."""
        kind = c.kind
        if kind == K.CSTYLE_CAST_EXPR:
            self._cast(c, stack)
        elif kind in (K.GOTO_STMT, K.INDIRECT_GOTO_STMT):
            self.add(
                "goto",
                c,
                "".join(k.spelling for k in kids(c)) if kind == K.GOTO_STMT else "indirect",
            )
        elif kind == K.SWITCH_STMT or (kind == K.IF_STMT and not _else_if(c, stack)):
            self._arms(c)
        if kind in (K.IF_STMT, *LOOPS):
            cond = self.condition(c)
            effects = self.effects(cond) if cond is not None else []
            if effects:
                self.add("effect-in-condition", c, ", ".join(effects))
        if kind == K.COMPOUND_STMT:
            sub = kids(c)
            for i, st in enumerate(sub):
                if st.kind in LOOPS:
                    self._counted(st, sub[:i])
        elif kind in LOOPS and not (stack and stack[-1].kind == K.COMPOUND_STMT):
            self._counted(c, [])
        stack.append(c)
        for k in c.get_children():
            self._visit(k, stack)
        stack.pop()

    # Effects.

    def _impure(self):
        """Names of the defined functions a call to which may have an effect: one with an
        effect of its own, or calling one, or calling a function not defined here and
        declared neither ``pure`` nor ``const`` (a greatest fixed point)."""
        calls, impure = {}, set()
        for f in self.functions:
            calls[f.spelling] = set()
            for n in walk(f):
                if n.kind == K.CALL_EXPR:
                    callee = n.referenced
                    if callee is None or callee.kind != K.FUNCTION_DECL:
                        impure.add(f.spelling)
                    elif not declared_pure(callee):
                        calls[f.spelling].add(callee.spelling)
                elif _effect(n, f):
                    impure.add(f.spelling)
        more = True
        while more:
            more = False
            for f, callees in calls.items():
                if f not in impure and any(g in impure or g not in calls for g in callees):
                    impure.add(f)
                    more = True
        return impure

    def _call_effect(self, n):
        """Whether call ``n`` may have an effect."""
        callee = n.referenced
        if callee is None or callee.spelling in self.impure:
            return True
        return callee.spelling not in self.defined and not declared_pure(callee)

    def effects(self, c):
        """The effects of expression ``c``: stores, commas, and calls that may have one."""
        out = set()
        for n in walk(c):
            lhs = stored(n)
            if lhs is not None:
                var = target(lhs)
                out.add(f"store {var.spelling}" if var is not None else "store")
            elif n.kind == K.BINARY_OPERATOR and binop(n) == ",":
                out.add("comma")
            elif n.kind == K.CALL_EXPR and self._call_effect(n):
                out.add(f"call {n.spelling}")
        return sorted(out)

    # Statements.

    def for_parts(self, c):
        """``(init, cond, inc, body)`` of a ``for``, None for each part it lacks."""
        i = bisect_left(self.offsets, c.extent.start.offset)
        if self.words[i : i + 2] != ["for", "("]:
            return None
        depth, semis, j = 0, [], i + 1
        for j in range(i + 1, len(self.words)):
            w = self.words[j]
            depth += (w == "(") - (w == ")")
            if depth == 1 and w == ";":
                semis.append(self.offsets[j])
            if depth == 0:
                break
        if len(semis) != 2 or depth:
            return None
        bounds = (semis[0], semis[1], self.offsets[j])
        parts = [None] * 4
        for k in kids(c):
            parts[bisect_right(bounds, k.extent.start.offset)] = k
        return tuple(parts)

    def loop_parts(self, c):
        """``(init, cond, inc, body)`` of a loop, or None."""
        if c.kind == K.FOR_STMT:
            return self.for_parts(c)
        sub = kids(c)
        return (
            (None, sub[0], None, sub[1]) if c.kind == K.WHILE_STMT else (None, sub[1], None, sub[0])
        )

    def condition(self, c):
        """The condition of an ``if``, ``switch`` or loop, or None."""
        if c.kind in (K.IF_STMT, K.SWITCH_STMT):
            return kids(c)[0]
        parts = self.loop_parts(c)
        return parts[1] if parts else None

    # duplicate-code.

    def _duplicates(self, f):  # pylint: disable=too-many-locals
        """Maximal sequences of at least ``SEQUENCE`` statements, equal but for the identity
        of the declarations they name, repeated within function ``f``."""
        ids, lists, where = {}, [], {}
        for c in walk(f):
            if c.kind == K.COMPOUND_STMT:
                sts = kids(c)
                seq = [
                    ids.setdefault(s.hash if s.kind == K.NULL_STMT else key(s, True), len(ids))
                    for s in sts
                ]
                for i, x in enumerate(seq):
                    where.setdefault(x, []).append((len(lists), i))
                lists.append((sts, seq))
        groups = {}
        for spots in where.values():
            for j, (na, ia) in enumerate(spots):
                for nb, ib in spots[j + 1 :]:
                    size = _run(lists[na][1], ia, lists[nb][1], ib, na == nb)
                    if size >= SEQUENCE:
                        seq = tuple(lists[na][1][ia : ia + size])
                        groups.setdefault(seq, set()).update(((na, ia), (nb, ib)))
        for seq, spots in groups.items():
            lines = sorted(lists[n][0][i].location.line for n, i in spots)
            listed = ", ".join(map(str, lines))
            self.add_line("duplicate-code", lines[0], f"{len(seq)} statements at lines {listed}")

    # subsumed-cast.

    def _cast(self, c, stack):
        """A cast between integer types whose removal leaves the value of the expression it
        is in unchanged by C's conversion rules."""
        to, sub = _int(c.type), strip(kids(c)[-1])
        frm = _int(sub.type)
        if to is None or frm is None or c.type.get_canonical() == sub.type.get_canonical():
            return
        below, i = c, len(stack) - 1
        while i >= 0 and stack[i].kind in (K.PAREN_EXPR, K.UNEXPOSED_EXPR):
            if len(kids(stack[i])) != 1:
                return
            below, i = stack[i], i - 1
        role, into = self._context(stack[i], below) if i >= 0 else (None, None)
        if role == "converted" and into is None:
            role = None
        if role is not None and all(
            _no_op(role, t, f, i, w) for t, f, i, w in zip(to, frm, into or to, INT_BITS)
        ):
            self.add("subsumed-cast", c, f"({c.type.spelling}) of {sub.type.spelling}")

    def _context(self, a, below):  # pylint: disable=too-many-return-statements
        """How ``a`` uses its operand ``below``: ``("converted", type)`` converts it to a
        type ``a`` fixes, ``"promoted"`` promotes it as an arithmetic operand, ``"value"``
        reads only its value; None any other use."""
        kind, sub = a.kind, kids(a)
        first = sub[0] == below
        if kind in (K.CSTYLE_CAST_EXPR, K.VAR_DECL):
            return "converted", _int(a.type)
        if kind == K.RETURN_STMT:
            return "converted", _int(self.fn.result_type)
        if kind == K.CALL_EXPR:
            return _argument(a, below)
        if kind == K.BINARY_OPERATOR:
            op = binop(a)
            if op == "=":
                return ("converted", _int(sub[0].type)) if not first else (None, None)
            if op in ("&&", "||"):
                return "value", None
            return ("promoted", None) if op != "," else (None, None)
        if kind == K.COMPOUND_ASSIGNMENT_OPERATOR:
            return ("promoted", None) if not first else (None, None)
        if kind == K.UNARY_OPERATOR:
            u = unop(a)
            return ("promoted" if u in ARITH else "value" if u == LNOT else None), None
        if kind == K.CONDITIONAL_OPERATOR:
            return ("value" if first else "promoted"), None
        if kind in (K.IF_STMT, K.SWITCH_STMT, *LOOPS):
            return ("value" if self.condition(a) == below else None), None
        if kind == K.ARRAY_SUBSCRIPT_EXPR:
            return ("value" if not first else None), None
        return None, None

    # common-arm-statements.

    def _arms(self, c):
        """The same statement leading or ending every arm of an if/else chain or switch."""
        if c.kind == K.IF_STMT:
            arms, conds, tail = [], [], c
            while tail is not None and tail.kind == K.IF_STMT:
                sub = kids(tail)
                conds.append(sub[0])
                arms.append(statements(sub[1]))
                tail = sub[2] if len(sub) > 2 else None
            if tail is None:
                return
            arms.append(statements(tail))
            sink = True
        else:
            got = _switch_arms(c)
            if got is None:
                return
            arms, sink = got
            conds = [kids(c)[0]]
        if not all(arms):
            return
        keys = [[key(s) for s in arm] for arm in arms]
        if all(k == keys[0] for k in keys):
            return
        first, last = arms[0][0], arms[0][-1]
        if (
            all(k[0] == keys[0][0] for k in keys)
            and first.kind not in UNMOVED
            and self._hoistable(first, conds)
        ):
            self.add("common-arm-statements", c, f"leads every arm: line {first.location.line}")
        if sink and all(k[-1] == keys[0][-1] for k in keys) and last.kind not in UNMOVED:
            self.add("common-arm-statements", c, f"ends every arm: line {last.location.line}")

    def _hoistable(self, st, conds):
        """Whether ``st`` may run before conditions ``conds``: they have no effect, ``st``
        stores to no variable they read, and, when they read more than this function's own
        variables, ``st`` stores to nothing else and calls nothing with an effect."""
        if any(self.effects(x) for x in conds):
            return False
        reads, memory = set(), False
        for n in (n for x in conds for n in walk(x)):
            if n.kind == K.DECL_REF_EXPR and n.referenced is not None:
                reads.add(n.referenced.hash)
                memory |= not self._own(n.referenced)
            memory |= n.kind in (K.ARRAY_SUBSCRIPT_EXPR, K.MEMBER_REF_EXPR, K.CALL_EXPR)
            memory |= n.kind == K.UNARY_OPERATOR and unop(n) == DEREF
        for n in walk(st):
            if n.kind == K.CALL_EXPR and memory and self._call_effect(n):
                return False
            lhs = stored(n)
            var = target(lhs) if lhs is not None else None
            if lhs is not None and (var is None or not self._own(var)) and memory:
                return False
            if var is not None and var.hash in reads:
                return False
        return True

    def _own(self, var):
        """Whether ``var`` is a parameter or automatic variable of this function whose
        address is never taken."""
        return (
            var.kind in (K.VAR_DECL, K.PARM_DECL)
            and var.semantic_parent == self.fn
            and var.storage_class != ci.StorageClass.STATIC
            and var.hash not in self.addressed
        )

    # uncounted-loop.

    def _counted(self, loop, before):  # pylint: disable=too-many-locals
        """A loop with a variable set to a constant before it, stepped by a constant once
        per iteration and tested by it, that is no ``for`` of that init, test and step."""
        parts = self.loop_parts(loop)
        if parts is None or any(n.kind in (*JUMPS[3:], K.LABEL_STMT) for n in walk(loop)):
            return
        init, cond, inc, body = parts
        exits = [
            kids(s)[0]
            for s in statements(body)
            if s.kind == K.IF_STMT
            and len(kids(s)) == 2
            and [x.kind for x in statements(kids(s)[1])] == [K.BREAK_STMT]
        ]
        tests = [v for t in [cond, *exits] if t is not None for v in _tested(t)]
        in_cond = {v.hash for v in _tested(cond)} if cond is not None else set()
        seen = set()
        for var in tests:
            if var is None or var.hash in seen or not self._local(var):
                continue
            seen.add(var.hash)
            steps = [
                (n, part)
                for part in (cond, inc, body)
                if part is not None
                for n in walk(part)
                if stored(n) is not None and same(target(stored(n)), var)
            ]
            if len(steps) != 1 or not _step(steps[0][0], var):
                continue
            step, part = steps[0]
            if not self._each_iteration(step, part, parts):
                continue
            mentions = init is not None and any(_mentions(n, var) for n in walk(init))
            start = _init(var, init) if mentions else _preceding(var, before)
            if start is None or (start is init and part is inc and var.hash in in_cond):
                continue
            self.add(
                "uncounted-loop",
                loop,
                f"{var.spelling}: set line {start.location.line}, "
                f"stepped line {step.location.line}",
            )

    def _local(self, var):
        """Whether ``var`` is an automatic integer variable of this function whose address
        is never taken."""
        return var.kind == K.VAR_DECL and self._own(var) and _int(var.type) is not None

    @staticmethod
    def _each_iteration(step, part, parts):
        """Whether ``step``, in loop part ``part``, runs once in every iteration."""
        _, cond, inc, body = parts
        if part is inc:
            return True
        if part is cond:
            return _always(cond, step)
        if step not in statements(body):
            return False
        at = step.extent.start.offset
        return not any(
            n.kind == K.CONTINUE_STMT and n.extent.start.offset < at for n in walk(body, stop=LOOPS)
        )

    # clang-tidy.

    def tidy(self, args=()):
        """Sites of clang-tidy's checks, and each function's cognitive complexity."""
        run = subprocess.run(
            [str(executable()), self.path, "--quiet", f"--config={CONFIG}", "--", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        found, noted = [], False
        for line in run.stdout.splitlines():
            m = DIAG.match(line)
            if m is None:
                continue
            if m["level"] == "error":
                raise CensusError(line)
            here = real(m["file"]) == self.path
            if m["level"] == "warning":
                noted = here and m["check"] in TIDY
                if noted:
                    found.append((m["check"], int(m["line"]), m["msg"], set()))
            elif noted and here and int(m["line"]) != found[-1][1]:
                found[-1][3].add(int(m["line"]))
        if run.returncode:
            raise CensusError(run.stderr.strip())
        sites, scores = [], {}
        for check, line, msg, notes in found:
            if check == "readability-function-cognitive-complexity":
                name, score = SCORE.match(msg).groups()
                scores[name] = int(score)
                msg = score
            elif notes:
                msg += ": lines " + ", ".join(map(str, sorted(notes)))
            sites.append(Site(TIDY[check], self.function_at(line), line, msg))
        return sites, scores


def _no_op(role, to, frm, into, w):
    """Whether a cast to ``to`` of a value of ``frm`` used as ``role`` (converted to
    ``into``) is a no-op where ``int`` is ``w`` bits wide."""
    if to.holds(frm):
        return role != "promoted" or _promoted(frm, w) == _promoted(to, w)
    return role == "converted" and to.wraps and into.wraps and to.bits >= into.bits


def _tested(c):
    """The declarations expression ``c`` reads outside its stores."""
    if stored(c) is not None:
        return []
    if c.kind == K.DECL_REF_EXPR:
        return [c.referenced] if c.referenced else []
    return [v for k in c.get_children() for v in _tested(k)]


def _mentions(n, var):
    """Whether node ``n`` declares or names ``var``."""
    return n == var or (n.kind == K.DECL_REF_EXPR and same(n.referenced, var))


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


def _else_if(c, stack):
    """Whether ``if`` ``c`` is the ``else`` of the ``if`` holding it."""
    if not stack or stack[-1].kind != K.IF_STMT:
        return False
    sub = kids(stack[-1])
    return len(sub) == 3 and sub[2] == c


def _effect(n, f):
    """Whether node ``n`` of function ``f`` has an effect beyond ``f``'s own automatic
    variables: a store elsewhere, a volatile access or inline assembly."""
    if n.kind in (K.ASM_STMT, K.MS_ASM_STMT):
        return True
    if n.kind.is_expression() and n.type.is_volatile_qualified():
        return True
    lhs = stored(n)
    if lhs is None:
        return False
    var = target(lhs)
    return not (
        var is not None
        and var.kind in (K.VAR_DECL, K.PARM_DECL)
        and var.storage_class != ci.StorageClass.STATIC
        and var.semantic_parent == f
    )


def _argument(call, arg):
    """``("converted", type)`` of argument ``arg`` of a call to a prototyped function within
    its parameters, else ``(None, None)``."""
    args, decl = list(call.get_arguments()), call.referenced
    if arg not in args or decl is None or decl.type.kind != TK.FUNCTIONPROTO:
        return None, None
    params, n = list(decl.type.argument_types()), args.index(arg)
    return ("converted", _int(params[n])) if n < len(params) else (None, None)


def _step(n, var):
    """Whether store ``n`` adds a constant to ``var``: ``++``, ``--``, ``+=``, ``-=`` or
    ``var = var ± constant``."""
    if n.kind == K.UNARY_OPERATOR:
        return True
    op, (_, rhs) = binop(n), kids(n)
    if op in ("+=", "-="):
        return constant(rhs)
    rhs = value_of(rhs)
    if op != "=" or rhs.kind != K.BINARY_OPERATOR or binop(rhs) not in ("+", "-"):
        return False
    a, b = (value_of(x) for x in kids(rhs))
    if same(target(a), var) and constant(b):
        return True
    return binop(rhs) == "+" and same(target(b), var) and constant(a)


def _init(var, st):
    """``st`` if it sets ``var`` to a constant, else None."""
    if st.kind == K.DECL_STMT:
        for d in kids(st):
            if d == var:
                init = [k for k in kids(d) if k.kind.is_expression()]
                return st if init and constant(init[-1]) else None
        return None
    e = strip(st)
    if e.kind == K.BINARY_OPERATOR and binop(e) == "=":
        lhs, rhs = kids(e)
        if same(target(lhs), var) and constant(rhs):
            return st
    return None


def _preceding(var, before):
    """The last statement of ``before`` mentioning ``var``, if it sets ``var`` to a constant
    and no label lies after it."""
    for st in reversed(before):
        nodes = list(walk(st))
        if any(n.kind == K.LABEL_STMT for n in nodes):
            return None
        if any(_mentions(n, var) for n in nodes):
            return _init(var, st)
    return None


def _always(cond, step):
    """Whether ``step`` runs in every evaluation of ``cond`` that continues the loop: it lies
    under only ``&&``, commas, parentheses and conversions."""
    if cond == step:
        return True
    if cond.kind in (K.PAREN_EXPR, K.UNEXPOSED_EXPR, K.CSTYLE_CAST_EXPR) or (
        cond.kind == K.BINARY_OPERATOR and binop(cond) in ("&&", ",")
    ):
        return any(_always(k, step) for k in kids(cond))
    return False


def _switch_arms(c):
    """``(arms, sinkable)`` of a switch with a ``default`` whose arms but the last end in a
    jump: each arm its statements less a trailing ``break``, sinkable when every arm ends by
    ``break`` or by falling off the end. None for any other switch."""
    arms, default = [], False
    for st in statements(kids(c)[-1]):
        if st.kind not in (K.CASE_STMT, K.DEFAULT_STMT):
            if not arms:
                return None
            arms[-1].append(st)
            continue
        while st.kind in (K.CASE_STMT, K.DEFAULT_STMT):
            default |= st.kind == K.DEFAULT_STMT
            st = kids(st)[-1]
        arms.append([st])
    if not default or any(arm[-1].kind not in JUMPS for arm in arms[:-1]):
        return None
    sink = all(arm[-1].kind == K.BREAK_STMT for arm in arms[:-1]) and (
        arms[-1][-1].kind not in JUMPS[1:]
    )
    return [arm[:-1] if arm[-1].kind == K.BREAK_STMT else arm for arm in arms], sink


def census(path, include=(), args=()):
    """The ``Report`` of C file ``path``, its headers found in directories ``include``."""
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    args = [*(f"-I{d}" for d in include), *args]
    unit = Unit(path, args)
    sites, scores = unit.tidy(args)
    sites += unit.census()
    return Report(sorted(sites, key=lambda s: (s.line, s.cls, s.detail)), scores)


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
    ``path``, ``class`` and ``function``) and ``total`` (a file's complexity key)."""
    files = sorted({p for g in options["files"] for p in glob.glob(g, recursive=True)})
    if not files:
        raise ValueError(f"no files match {options['files']}")
    template = options.get("key", "{stem}:{class}")
    total = options.get("total", "{stem}:cognitive-complexity")
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
    return dict(out)
