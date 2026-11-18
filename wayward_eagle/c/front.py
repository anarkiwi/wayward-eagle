"""clang's JSON AST of a C file as pycparser ``c_ast``, with clang's semantics in a side table.

The pinned clang (the ``ziglang`` wheel's) dumps the translation unit; only declarations
spelled in the main file are converted. Parentheses, implicit casts and constant-expression
wrappers are no nodes: each node's :class:`Info` holds what clang knows of them.
"""

from __future__ import annotations

import copy
import functools
import pathlib
import platform
import subprocess
from typing import NamedTuple

import orjson
import ziglang
from pycparser import c_ast, c_parser

ZIG = pathlib.Path(ziglang.__file__).parent
#: The targets :func:`dump` parses for: the host, and one whose ``int`` is 16 bits wide.
HOST = f"{platform.machine()}-linux-gnu"
NARROW = "avr"


class CError(RuntimeError):
    """C the compiler rejects."""


class Loc(NamedTuple):
    """A source location: byte offset, line, column, token length and file, at the macro
    expansion when the token comes from one (``macro`` then)."""

    offset: int
    line: int
    col: int
    toklen: int
    file: str
    macro: bool = False
    arg: bool = False


class Type(NamedTuple):
    """An expression's or declaration's type as clang spells it, and fully desugared."""

    qual: str
    canon: str


class Cast(NamedTuple):
    """A conversion clang applies: its cast kind and the type it converts to."""

    kind: str
    type: Type


class Info:  # pylint: disable=too-few-public-methods
    """What clang knows of a node: its clang kind, type, the implicit casts applied to its
    value (innermost first), the canonical id of the declaration it references or declares,
    its source range (expansion locations), the spelling location of its first token, an
    explicit cast's kind, a literal's or constant expression's value, and a declaration's
    declaration statement (the begin of the ``DeclStmt`` declaring several at once)."""

    __slots__ = (
        "kind",
        "type",
        "casts",
        "ref",
        "begin",
        "end",
        "spelling",
        "cast",
        "value",
        "group",
    )

    def __init__(self, kind, typ, begin, end, spelling):
        self.kind, self.type, self.begin, self.end, self.spelling = kind, typ, begin, end, spelling
        self.casts, self.ref, self.cast, self.value, self.group = (), None, None, None, None

    @property
    def converted(self):
        """The type of the node's value after its implicit casts."""
        return self.casts[-1].type if self.casts else self.type

    def __repr__(self):
        return f"Info({self.kind}, {self.type}, casts={self.casts}, ref={self.ref})"


class Symbol:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """A declaration, its redeclarations merged: clang kind, name, type, storage class,
    attribute kinds, whether it never returns, the canonical id of the function it is local
    to (None at file scope), and its ``c_ast`` node when the main file declares it."""

    __slots__ = ("id", "kind", "name", "type", "storage", "attrs", "noreturn", "function", "node")

    def __init__(self, ident, kind, name, typ):
        self.id, self.kind, self.name, self.type = ident, kind, name, typ
        self.storage, self.attrs, self.noreturn = None, set(), False
        self.function, self.node = None, None

    def __repr__(self):
        return f"Symbol({self.kind} {self.name}: {self.type})"


class Opaque(c_ast.Node):
    """A construct ``c_ast`` has no node for (inline assembly, a computed ``goto``, a GNU
    statement expression, ...): clang's kind, its source text, and its converted children."""

    __slots__ = ("kind", "text", "nodes", "coord", "__weakref__")
    attr_names = ("kind", "text")

    def __init__(self, kind, text, nodes, coord=None):
        self.kind, self.text, self.nodes, self.coord = kind, text, nodes, coord

    def children(self):
        return tuple((f"nodes[{i}]", n) for i, n in enumerate(self.nodes))

    def __iter__(self):
        return iter(self.nodes)


#: clang's postfix increments as pycparser spells them.
POSTFIX = {"++": "p++", "--": "p--"}
LITERALS = {
    "IntegerLiteral": "int",
    "CharacterLiteral": "char",
    "FloatingLiteral": "double",
    "FixedPointLiteral": "double",
}
#: Kinds that are their only child as far as ``c_ast`` is concerned.
TRANSPARENT = frozenset(("ParenExpr", "ConstantExpr", "FullExpr", "ExprWithCleanups"))
#: How clang's type printer appends a function type's attributes.
ATTRIBUTE = " __attribute__(("
LABELS = (c_ast.Case, c_ast.Default)
#: Type names clang prints that pycparser does not know, parsed as typedef names.
BUILTIN = ("bool", "_Float16", "__fp16", "__bf16", "_Float128", "__float128")


def command(target=HOST):
    """The pinned clang's command line, for ``target`` (:data:`HOST` or :data:`NARROW`)."""
    out = [str(ZIG / "zig"), "clang", f"--target={target}", "-resource-dir", str(ZIG / "lib")]
    return out + (["-nostdlibinc"] if target != HOST else [])


def dump(path, args=(), target=HOST):
    """clang's JSON AST of C file ``path`` (bytes); :class:`CError` when clang rejects it."""
    run = subprocess.run(
        [*command(target), "-fsyntax-only", "-Xclang", "-ast-dump=json", *args, str(path)],
        capture_output=True,
        check=False,
    )
    if run.returncode:
        err = run.stderr.decode(errors="replace")
        raise CError("\n".join(x for x in err.splitlines() if " error: " in x) or err.strip())
    return run.stdout


@functools.cache
def _parser():
    return c_parser.CParser()


class Types:
    """C type spellings clang prints, as ``c_ast`` type trees parsed by pycparser as type
    names with the translation unit's typedef names declared; a spelling pycparser cannot
    parse (a GNU extension type) is an ``IdentifierType`` of it."""

    def __init__(self):
        self.typedefs, self.cache = list(BUILTIN), {}

    def tree(self, spelling):
        """A fresh ``c_ast`` type tree of ``spelling`` with no declarator name."""
        got = self.cache.get(spelling)
        if got is None:
            heads = "".join(f"typedef int {t};\n" for t in self.typedefs)
            try:
                ast = _parser().parse(f"{heads}void __t(void) {{ sizeof({spelling}); }}")
                got = ast.ext[-1].body.block_items[0].expr
            except c_parser.ParseError:
                got = None
            if isinstance(got, c_ast.Typename):
                got = got.type
            else:
                got = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType([spelling]))
            self.cache[spelling] = got
        return copy.deepcopy(got)


def named(tree, name):
    """``tree`` (a type from :meth:`Types.tree`) declaring ``name``."""
    t = tree
    while not isinstance(t, c_ast.TypeDecl):
        t = t.type
    t.declname = name
    return tree


def _quals(tree):
    """The qualifiers of the outermost level of a type tree."""
    return list(getattr(tree, "quals", None) or [])


class Unit:
    """A C file as ``c_ast``: ``ast`` the ``FileAST`` of its own declarations, ``functions``
    its function definitions, ``sema`` each node's :class:`Info`, ``symbols`` each
    declaration's :class:`Symbol` by canonical id, ``target`` the target parsed for."""

    def __init__(self, path, ast, sema, symbols, target, macros=None, types=None):
        self.path, self.ast, self.sema, self.symbols, self.target = path, ast, sema, symbols, target
        self.macros, self.types = macros or {}, types or Types()
        self.functions = [d for d in ast.ext if isinstance(d, c_ast.FuncDef)]
        self._text = None

    @property
    def text(self):
        """The file's bytes."""
        if self._text is None:
            self._text = pathlib.Path(self.path).read_bytes()
        return self._text

    def tree(self, typ):
        """A fresh ``c_ast`` type tree of a :class:`Type` (or spelling), attributes dropped."""
        return self.types.tree((typ if isinstance(typ, str) else typ.qual).split(ATTRIBUTE)[0])

    def qualifiers(self, typ):
        """The qualifiers of the outermost level of a :class:`Type` (``volatile``, ...)."""
        return _quals(self.tree(typ))

    def info(self, node):
        """The :class:`Info` of ``node``, None for a node clang has no counterpart of."""
        return self.sema.get(node)

    def symbol(self, node):
        """The :class:`Symbol` ``node`` references or declares, or None."""
        info = self.sema.get(node)
        return self.symbols.get(info.ref) if info is not None and info.ref else None

    def line(self, node):
        """The line ``node`` begins on (at its macro expansion)."""
        return self.sema[node].begin.line

    def span(self, node):
        """``(first line, last line)`` of ``node``."""
        info = self.sema[node]
        return info.begin.line, info.end.line

    def source(self, node):
        """The source text of ``node``, macro invocations as written, or None when it does
        not begin and end outside macros in this file."""
        info = self.sema.get(node)
        if info is None or info.begin is None or info.end is None:
            return None
        b, e = info.begin, info.end
        if b.macro or e.macro or b.file != self.path or e.file != self.path:
            return None
        return self.text[b.offset : e.offset + e.toklen].decode()


class _Import:  # pylint: disable=too-many-public-methods,too-many-instance-attributes
    """One pass over clang's JSON in dump order, tracking the file and line each location
    omits when it repeats the one before (as clang's JSON dumper elides them)."""

    def __init__(self, path):
        self.path, self.file, self.line = path, "", 0
        self.sema, self.symbols, self.canon, self.types = {}, {}, {}, Types()
        self.texts, self.labels, self.gotos, self.function = {}, {}, [], None
        self.macros = {}

    # Locations.

    def _bare(self, b, macro=False):
        f = b.get("file")
        if f is not None:
            self.file = f
        line = b.get("line")
        if line is not None:
            self.line = line
        return Loc(b["offset"], self.line, b["col"], b["tokLen"], self.file, macro)

    def _loc(self, d):
        """``(expansion, spelling)`` of a JSON location."""
        if not d:
            return None, None
        if "spellingLoc" in d:
            s = self._bare(d["spellingLoc"], True)
            x = d["expansionLoc"]
            return self._bare(x, True)._replace(arg=x.get("isMacroArgExpansion", False)), s
        b = self._bare(d)
        return b, b

    def _range(self, j):
        """``(begin, end, spelling of begin)`` of JSON node ``j``, its location first."""
        if "loc" in j:
            self._loc(j["loc"])
        r = j.get("range")
        if not r:
            return None, None, None
        b, s = self._loc(r.get("begin"))
        e, _ = self._loc(r.get("end"))
        return b, e, s

    def skip(self, j):
        """Track the locations of a JSON subtree not converted; record its declarations."""
        if not j:
            return
        self._range(j)
        kind = j.get("kind")
        if kind == "TypedefDecl":
            self.types.typedefs.append(j["name"])
        elif kind in ("FunctionDecl", "VarDecl", "ParmVarDecl", "EnumConstantDecl"):
            self._symbol(j)
        for k in j.get("inner", ()):
            self.skip(k)

    def _attrs(self, j):
        """``(attribute JSON nodes with their ranges, other children)`` of ``j``."""
        attrs, rest = [], []
        for k in j.get("inner", ()):
            if k.get("kind", "").endswith("Attr"):
                attrs.append((k, self._range(k)))
                for x in k.get("inner", ()):
                    self.skip(x)
            else:
                rest.append(k)
        return attrs, rest

    def token(self, loc):
        """The text of the token at spelling location ``loc``, or None outside a file."""
        if loc is None or not loc.file or loc.file.startswith("<"):
            return None
        text = self.texts.get(loc.file)
        if text is None:
            try:
                text = self.texts[loc.file] = pathlib.Path(loc.file).read_bytes()
            except OSError:
                return None
        return text[loc.offset : loc.offset + loc.toklen].decode(errors="replace")

    # Declarations.

    def _symbol(self, j):
        """The merged :class:`Symbol` of declaration ``j``, created or updated."""
        ident = j["id"]
        prev = j.get("previousDecl")
        root = self.canon.get(prev, prev) if prev else ident
        self.canon[ident] = root
        sym = self.symbols.get(root)
        if sym is None:
            sym = self.symbols[root] = Symbol(root, j["kind"], j.get("name", ""), _type(j))
            sym.function = self.function
        if "storageClass" in j:
            sym.storage = j["storageClass"]
        sym.attrs |= {k["kind"] for k in j.get("inner", ()) if k.get("kind", "").endswith("Attr")}
        sym.noreturn |= ATTRIBUTE + "noreturn" in j.get("type", {}).get("qualType", "")
        return sym

    def ref(self, j):
        """The canonical id of the declaration JSON node ``j`` references."""
        d = j.get("referencedDecl") or {}
        ident = d.get("id")
        if ident is None:
            return None
        root = self.canon.get(ident, ident)
        if root not in self.symbols:
            self.symbols[root] = Symbol(root, d.get("kind"), d.get("name", ""), _type(d))
        return root

    # Nodes.

    def info(self, node, j, rng):
        """Record and return the :class:`Info` of ``node`` from JSON ``j`` at ``rng``."""
        b, e, s = rng
        info = self.sema[node] = Info(j["kind"], _type(j), b, e, s)
        self._outermost(node, _expansion(info))
        if b is not None:
            node.coord = c_parser.Coord(b.file, b.line, b.col)
        return info

    def _outermost(self, node, outer):
        """Record each child of ``node`` that is the whole expansion of an object-like macro
        ``node`` is not inside (``outer`` the expansion ``node`` is, if any) by its name."""
        for _, c in node.children():
            ci = self.sema.get(c)
            if ci is None:
                self._outermost(c, outer)
                continue
            x = _expansion(ci)
            if x is None or x == outer or c in self.macros:
                continue
            name = self.token(ci.begin)
            if name and not self._function_like(ci.begin):
                self.macros[c] = name

    def _function_like(self, loc):
        """Whether the macro named at ``loc`` is invoked with arguments."""
        text = self.texts[loc.file]
        return text[loc.offset + loc.toklen :].lstrip()[:1] == b"("

    def unit(self, data):
        """The ``c_ast`` declarations of the JSON translation unit ``data`` that its main
        file spells."""
        ext = []
        for j in data.get("inner", ()):
            if self._main(j):
                ext += self.external(j)
            else:
                self.skip(j)
        return ext

    def _main(self, j):
        """Whether top-level declaration ``j`` is spelled in the main file."""
        d = j.get("loc") or {}
        d = d.get("expansionLoc", d)
        if "file" in d:
            return d["file"] == self.path and "includedFrom" not in d
        return self.file == self.path and bool(d) and not j.get("isImplicit")

    def external(self, j):
        """The ``c_ast`` declarations of top-level JSON declaration ``j``."""
        kind = j["kind"]
        if kind == "FunctionDecl":
            return [self.function_decl(j)]
        if kind == "VarDecl":
            return [self.var(j)]
        if kind == "TypedefDecl":
            rng = self._range(j)
            for k in j.get("inner", ()):
                self.skip(k)
            tree = named(self.types.tree(_type(j).qual), j["name"])
            self.types.typedefs.append(j["name"])
            node = c_ast.Typedef(j["name"], [], ["typedef"], tree)
            self.info(node, j, rng)
            return [node]
        if kind in ("RecordDecl", "EnumDecl"):
            return [c_ast.Decl(None, [], None, [], [], self.tag(j), None, None)]
        self.skip(j)
        return []

    def tag(self, j):
        """A struct, union or enum declaration's ``c_ast`` type."""
        rng = self._range(j)
        name = j.get("name")
        kind = "EnumConstantDecl" if j["kind"] == "EnumDecl" else "FieldDecl"
        members = []
        for k in j.get("inner", ()):
            if k.get("kind") != kind:
                self.skip(k)
                continue
            r = self._range(k)
            if kind == "FieldDecl":
                for x in k.get("inner", ()):
                    self.skip(x)
                tree = named(self.types.tree(_type(k).qual), k.get("name"))
                m = c_ast.Decl(k.get("name"), _quals(tree), None, [], [], tree, None, None)
                self.info(m, k, r)
            else:
                sym = self._symbol(k)
                inner = k.get("inner", ())
                m = sym.node = c_ast.Enumerator(k["name"], self.expr(inner[0]) if inner else None)
                self.info(m, k, r).ref = sym.id
            members.append(m)
        if j["kind"] == "EnumDecl":
            node = c_ast.Enum(name, c_ast.EnumeratorList(members) if members else None, None)
        else:
            cls = c_ast.Union if j.get("tagUsed") == "union" else c_ast.Struct
            node = cls(name, members if j.get("completeDefinition") else None)
        self.info(node, j, rng)
        return node

    def attributes(self, attrs):
        """``c_ast`` attribute specifiers of JSON attribute nodes, as spelled in the source."""
        out = [
            c_ast.Attribute(self.token(rng[2]) or a["kind"][: -len("Attr")].lower(), None, None)
            for a, rng in attrs
        ]
        return [c_ast.AttributeSpecifier(out)] if out else []

    def function_decl(self, j):
        """A function's ``c_ast``: a ``FuncDef`` when it has a body, else a ``Decl``."""
        rng = self._range(j)
        sym = self._symbol(j)
        ftype = self.types.tree(_type(j).qual.split(ATTRIBUTE)[0])
        if not isinstance(ftype, c_ast.FuncDecl):
            ftype = c_ast.FuncDecl(None, ftype)
        self.function, self.labels, self.gotos = sym.id, {}, []
        params, body = [], None
        attrs, rest = self._attrs(j)
        for k in rest:
            if k.get("kind") == "ParmVarDecl":
                params.append(self.var(k))
            elif k.get("kind") == "CompoundStmt":
                body = self.stmt(k)
            else:
                self.skip(k)
        for goto, label in self.gotos:
            goto.name = self.labels.get(label, goto.name)
        self.function = None
        if params:
            ftype.args = c_ast.ParamList(params)
        named(ftype, j["name"])
        specs = self.attributes(attrs)
        if sym.noreturn and not any(a.name == "noreturn" for s in specs for a in s.attributes):
            specs = specs or [c_ast.AttributeSpecifier([])]
            specs[0].attributes.append(c_ast.Attribute("noreturn", None, None))
        decl = c_ast.Decl(
            j["name"],
            [],
            None,
            [j["storageClass"]] if "storageClass" in j else [],
            ["inline"] if j.get("inline") else [],
            ftype,
            None,
            None,
            attributes=specs,
        )
        info = self.info(decl, j, rng)
        info.ref = sym.id
        if body is None:
            sym.node = sym.node or decl
            return decl
        node = sym.node = c_ast.FuncDef(decl, None, body)
        self.sema[node] = info
        node.coord = decl.coord
        return node

    def var(self, j):
        """A variable or parameter declaration's ``Decl``."""
        rng = self._range(j)
        sym = self._symbol(j)
        sym.function = self.function
        tree = named(self.types.tree(_type(j).qual), j.get("name"))
        attrs, rest = self._attrs(j)
        init = self.expr(rest[-1]) if rest else None
        node = c_ast.Decl(
            j.get("name"),
            _quals(tree),
            None,
            [j["storageClass"]] if "storageClass" in j else [],
            [],
            tree,
            init,
            None,
            attributes=self.attributes(attrs),
        )
        self.info(node, j, rng).ref = sym.id
        sym.node = node
        return node

    # Statements.

    def stmt(self, j):
        """A statement's ``c_ast`` node (an expression statement is the expression)."""
        if not j:
            return None
        kind = j["kind"]
        method = getattr(self, "s_" + kind, None)
        if method is not None:
            return method(j, self._range(j))
        if kind.endswith(("Expr", "Operator", "Literal")):
            return self.expr(j)
        return self.opaque(j, self._range(j))

    def block(self, j):
        """The block items of compound-statement JSON ``j``, each declaration its own."""
        out = []
        for k in j.get("inner", ()):
            if k.get("kind") != "DeclStmt":
                out.append(self.stmt(k))
                continue
            group = self._range(k)[0]
            for d in k.get("inner", ()):
                kind = d.get("kind")
                if kind == "VarDecl":
                    out.append(self.var(d))
                    self.sema[out[-1]].group = group
                elif kind in ("RecordDecl", "EnumDecl", "TypedefDecl"):
                    out += self.external(d)
                else:
                    self.skip(d)
        return out

    def s_CompoundStmt(self, j, rng):  # pylint: disable=invalid-name
        """``{ ... }``"""
        node = c_ast.Compound(self.block(j) or None)
        self.info(node, j, rng)
        return node

    def s_IfStmt(self, j, rng):  # pylint: disable=invalid-name
        """``if``"""
        inner = j["inner"]
        node = c_ast.If(self.expr(inner[0]), self.stmt(inner[1]), None)
        if len(inner) > 2:
            node.iffalse = self.stmt(inner[2])
        self.info(node, j, rng)
        return node

    def s_WhileStmt(self, j, rng):  # pylint: disable=invalid-name
        """``while``"""
        node = c_ast.While(self.expr(j["inner"][0]), None)
        node.stmt = self.stmt(j["inner"][1])
        self.info(node, j, rng)
        return node

    def s_DoStmt(self, j, rng):  # pylint: disable=invalid-name
        """``do``/``while``"""
        body = self.stmt(j["inner"][0])
        node = c_ast.DoWhile(self.expr(j["inner"][1]), body)
        self.info(node, j, rng)
        return node

    def s_ForStmt(self, j, rng):  # pylint: disable=invalid-name
        """``for``: clang's five children, init, condition variable, condition, step, body,
        absent ones empty."""
        init, condvar, cond, step, body = j["inner"]
        if init.get("kind") == "DeclStmt":
            self._range(init)
            first = c_ast.DeclList([self.var(d) for d in init.get("inner", ())])
        else:
            first = self.expr(init) if init else None
        self.skip(condvar)
        node = c_ast.For(first, self.expr(cond) if cond else None, None, None)
        node.next = self.expr(step) if step else None
        node.stmt = self.stmt(body)
        self.info(node, j, rng)
        return node

    def s_SwitchStmt(self, j, rng):  # pylint: disable=invalid-name
        """``switch``, its body's statements grouped under their case labels as pycparser
        groups them."""
        cond, body = self.expr(j["inner"][0]), j["inner"][1]
        if body.get("kind") != "CompoundStmt":
            node = c_ast.Switch(cond, self.stmt(body))
            self.info(node, j, rng)
            return node
        brng = self._range(body)
        items = []
        for k in body.get("inner", ()):
            got = self.stmt(k)
            if isinstance(got, LABELS) or not items or not isinstance(items[-1], LABELS):
                items.append(got)
            else:
                _deepest(items[-1]).stmts.append(got)
        flat = [x for it in items for x in _unchain(it)]
        stmt = c_ast.Compound(flat or None)
        self.info(stmt, body, brng)
        node = c_ast.Switch(cond, stmt)
        self.info(node, j, rng)
        return node

    def s_CaseStmt(self, j, rng):  # pylint: disable=invalid-name
        """``case``: its value, a GNU range's end when present, then its statement."""
        inner = j["inner"]
        node = c_ast.Case(self.expr(inner[0]), [])
        if len(inner) > 2:
            self.skip(inner[1])
        sub = self.stmt(inner[-1])
        node.stmts = [sub] if sub is not None else []
        self.info(node, j, rng)
        return node

    def s_DefaultStmt(self, j, rng):  # pylint: disable=invalid-name
        """``default``"""
        sub = self.stmt(j["inner"][0])
        node = c_ast.Default([sub] if sub is not None else [])
        self.info(node, j, rng)
        return node

    def s_LabelStmt(self, j, rng):  # pylint: disable=invalid-name
        """A label."""
        self.labels[j["declId"]] = j["name"]
        node = c_ast.Label(j["name"], None)
        node.stmt = self.stmt(j["inner"][0])
        self.info(node, j, rng)
        return node

    def s_GotoStmt(self, j, rng):  # pylint: disable=invalid-name
        """``goto``, its label named once the function's labels are known."""
        node = c_ast.Goto(None)
        self.gotos.append((node, j["targetLabelDeclId"]))
        self.info(node, j, rng)
        return node

    def s_IndirectGotoStmt(self, j, rng):  # pylint: disable=invalid-name
        """``goto *e;``, a ``Goto`` whose name is the expression."""
        node = c_ast.Goto(self.expr(j["inner"][0]))
        self.info(node, j, rng)
        return node

    def s_ReturnStmt(self, j, rng):  # pylint: disable=invalid-name
        """``return``"""
        inner = j.get("inner")
        node = c_ast.Return(self.expr(inner[0]) if inner else None)
        self.info(node, j, rng)
        return node

    def s_BreakStmt(self, j, rng):  # pylint: disable=invalid-name
        """``break``"""
        node = c_ast.Break()
        self.info(node, j, rng)
        return node

    def s_ContinueStmt(self, j, rng):  # pylint: disable=invalid-name
        """``continue``"""
        node = c_ast.Continue()
        self.info(node, j, rng)
        return node

    def s_NullStmt(self, j, rng):  # pylint: disable=invalid-name
        """``;``"""
        node = c_ast.EmptyStatement()
        self.info(node, j, rng)
        return node

    def s_AttributedStmt(self, j, rng):  # pylint: disable=invalid-name
        """A statement under attributes (``__attribute__((fallthrough));``)."""
        attrs, rest = self._attrs(j)
        node = c_ast.AttributedStatement(self.attributes(attrs), None)
        node.stmt = self.stmt(rest[0]) if rest else c_ast.EmptyStatement()
        self.info(node, j, rng)
        return node

    def opaque(self, j, rng):
        """An :class:`Opaque` node of a construct ``c_ast`` has none for."""
        b, e, _ = rng
        nodes = [x for x in (self.stmt(k) for k in j.get("inner", ())) if x is not None]
        text = None
        if b is not None and e is not None and not b.macro and not e.macro:
            whole = Loc(b.offset, b.line, b.col, e.offset + e.toklen - b.offset, b.file)
            text = self.token(whole)
        node = Opaque(j["kind"], text, nodes)
        self.info(node, j, rng)
        return node

    # Expressions.

    def expr(self, j):
        """An expression's ``c_ast`` node; parentheses and implicit casts fold into the
        :class:`Info` of the node under them."""
        kind = j["kind"]
        if kind == "ImplicitCastExpr":
            self._range(j)
            node = self.expr(j["inner"][0])
            info = self.sema.get(node)
            if info is not None:
                info.casts += (Cast(j["castKind"], _type(j)),)
            return node
        if kind in TRANSPARENT:
            self._range(j)
            node = self.expr(j["inner"][0])
            if "value" in j and node in self.sema:
                self.sema[node].value = j["value"]
            return node
        rng = self._range(j)
        method = getattr(self, "e_" + kind, None)
        if method is not None:
            return method(j, rng)
        if kind in LITERALS:
            node = c_ast.Constant(LITERALS[kind], self.token(rng[2]) or j.get("value", ""))
            self.info(node, j, rng).value = j.get("value")
            return node
        return self.opaque(j, rng)

    def _exprs(self, j):
        return [self.expr(k) for k in j.get("inner", ())]

    def e_StringLiteral(self, j, rng):  # pylint: disable=invalid-name
        """A string literal."""
        node = c_ast.Constant("string", j["value"])
        self.info(node, j, rng).value = j["value"]
        return node

    def e_DeclRefExpr(self, j, rng):  # pylint: disable=invalid-name
        """A name."""
        node = c_ast.ID(j["referencedDecl"]["name"])
        self.info(node, j, rng).ref = self.ref(j)
        return node

    def e_BinaryOperator(self, j, rng):  # pylint: disable=invalid-name
        """A binary operator; ``=`` an ``Assignment``, ``,`` an ``ExprList``."""
        a, b = self._exprs(j)
        op = j["opcode"]
        if op == ",":
            node = c_ast.ExprList([a, b])
        else:
            node = c_ast.Assignment(op, a, b) if op == "=" else c_ast.BinaryOp(op, a, b)
        self.info(node, j, rng)
        return node

    def e_CompoundAssignOperator(self, j, rng):  # pylint: disable=invalid-name
        """``+=`` and its kind."""
        a, b = self._exprs(j)
        node = c_ast.Assignment(j["opcode"], a, b)
        self.info(node, j, rng)
        return node

    def e_UnaryOperator(self, j, rng):  # pylint: disable=invalid-name
        """A unary operator, postfix increments ``p++`` as pycparser spells them."""
        (a,) = self._exprs(j)
        op = j["opcode"]
        node = c_ast.UnaryOp(POSTFIX.get(op, op) if j.get("isPostfix") else op, a)
        self.info(node, j, rng)
        return node

    def e_CallExpr(self, j, rng):  # pylint: disable=invalid-name
        """A call, referencing its callee's declaration when it names one."""
        callee, *args = self._exprs(j)
        node = c_ast.FuncCall(callee, c_ast.ExprList(args) if args else None)
        info = self.info(node, j, rng)
        if isinstance(callee, c_ast.ID):
            info.ref = self.sema[callee].ref
        return node

    def e_ArraySubscriptExpr(self, j, rng):  # pylint: disable=invalid-name
        """``a[i]``"""
        a, b = self._exprs(j)
        node = c_ast.ArrayRef(a, b)
        self.info(node, j, rng)
        return node

    def e_MemberExpr(self, j, rng):  # pylint: disable=invalid-name
        """``s.f`` and ``p->f``."""
        (a,) = self._exprs(j)
        node = c_ast.StructRef(a, "->" if j.get("isArrow") else ".", c_ast.ID(j["name"]))
        self.info(node, j, rng)
        return node

    def e_CStyleCastExpr(self, j, rng):  # pylint: disable=invalid-name
        """``(T)e``, its cast kind recorded."""
        (a,) = self._exprs(j)
        tree = self.types.tree(_type(j).qual)
        node = c_ast.Cast(c_ast.Typename(None, _quals(tree), None, tree), a)
        self.info(node, j, rng).cast = j["castKind"]
        return node

    def e_ConditionalOperator(self, j, rng):  # pylint: disable=invalid-name
        """``c ? a : b``"""
        c, a, b = self._exprs(j)
        node = c_ast.TernaryOp(c, a, b)
        self.info(node, j, rng)
        return node

    def e_UnaryExprOrTypeTraitExpr(self, j, rng):  # pylint: disable=invalid-name
        """``sizeof`` and ``_Alignof`` of a type or an expression."""
        if "argType" in j:
            tree = self.types.tree(j["argType"]["qualType"])
            arg = c_ast.Typename(None, _quals(tree), None, tree)
        else:
            (arg,) = self._exprs(j)
        node = c_ast.UnaryOp(j["name"], arg)
        self.info(node, j, rng)
        return node

    def e_InitListExpr(self, j, rng):  # pylint: disable=invalid-name
        """``{a, b}``"""
        exprs = [
            self.expr(k) for k in j.get("inner", ()) if k.get("kind") != "ImplicitValueInitExpr"
        ]
        for k in j.get("array_filler", ()):
            self.skip(k)
        node = c_ast.InitList(exprs)
        self.info(node, j, rng)
        return node

    def e_CompoundLiteralExpr(self, j, rng):  # pylint: disable=invalid-name
        """``(T){...}``"""
        (init,) = self._exprs(j)
        tree = self.types.tree(_type(j).qual)
        node = c_ast.CompoundLiteral(c_ast.Typename(None, _quals(tree), None, tree), init)
        self.info(node, j, rng)
        return node


def _type(j):
    t = j.get("type")
    if not t:
        return None
    q = t["qualType"]
    return Type(q, t.get("desugaredQualType", q))


def _expansion(info):
    """The offset of the macro invocation all of ``info``'s node expands from, or None."""
    b, e = info.begin, info.end
    if b is None or e is None or not (b.macro and e.macro) or b.arg or e.arg:
        return None
    return b.offset if b.offset == e.offset and b.file == e.file else None


def _deepest(label):
    """The innermost label of a chain ``case 1: case 2: ...``."""
    while label.stmts and isinstance(label.stmts[-1], LABELS):
        label = label.stmts[-1]
    return label


def _unchain(st):
    """``case 1: case 2: s`` as pycparser holds it: ``Case(1, [])``, ``Case(2, [s])``."""
    out = []
    while isinstance(st, LABELS):
        out.append(st)
        if len(st.stmts) == 1 and isinstance(st.stmts[0], LABELS):
            st.stmts, st = [], st.stmts[0]
        else:
            break
    return out or [st]


def load(path, include=(), args=(), target=HOST):
    """The :class:`Unit` of C file ``path``, its headers in directories ``include``, parsed
    by the pinned clang for ``target`` with further compiler ``args``."""
    path = str(pathlib.Path(path).resolve())
    data = orjson.loads(dump(path, [*(f"-I{d}" for d in include), *args], target))
    imp = _Import(path)
    ext = imp.unit(data)
    return Unit(path, c_ast.FileAST(ext), imp.sema, imp.symbols, target, imp.macros, imp.types)
