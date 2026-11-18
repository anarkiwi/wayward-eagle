# C layer

`wayward_eagle.c` reads a C file through clang and holds it as pycparser's
[`c_ast`](https://github.com/eliben/pycparser), with clang's semantics in a side table.
The census, the reducer and other C-reading tools share it.

- **Front end**: clang's JSON AST (`-fsyntax-only -Xclang -ast-dump=json`). clang reads any
  C with its real headers, macros and target, and answers semantic questions (types, implicit
  conversions, which declaration a name refers to).
- **IR**: `c_ast`, a maintained, dependency-free node set with a C generator. pycparser's own
  parser never reads a file; it parses only the type spellings clang prints, into `c_ast`
  type trees.
- **Loading**: `orjson`; the census runs files in a process pool.

## clang

The pinned `ziglang` wheel (`ziglang==0.17.1`) bundles clang 22. It runs as `zig clang`
(`wayward_eagle.c.command(target)`):

- `-resource-dir` is the wheel's `lib`, which holds clang's builtin headers;
- `--target=<machine>-linux-gnu` (`HOST`), so system headers resolve as they do for the
  pinned clang-tidy;
- `NARROW` (`avr`) with `-nostdlibinc` parses for a data model whose `int` is 16 bits; its
  `<stdint.h>` is clang's own.

`zig cc` is not used: it wraps clang in zig's compile cache, which fails `-fsyntax-only`
runs. A file clang rejects raises `CError`.

## Units

```python
from wayward_eagle import c

unit = c.load("tune.c", include=["inc"], args=["-DX=1"], target=c.HOST)
unit.ast        # c_ast.FileAST of the declarations the main file spells
unit.functions  # its c_ast.FuncDef nodes
```

Only declarations spelled in the main file are converted; header declarations still give
their `Symbol`s. clang's JSON omits a location's file and line when they repeat the one
printed before, so the importer walks the dump in order and tracks them.

| Conversion | `c_ast` |
|---|---|
| parentheses, implicit casts, `ConstantExpr` | no node; folded into the `Info` of the node under them |
| `a, b` | `ExprList` (a call's arguments are an `ExprList` too: `commas(n)` tells them apart) |
| `x++`, `x--` | `UnaryOp("p++")`, `UnaryOp("p--")`, as pycparser spells them |
| `=`, `+=`, ... | `Assignment` |
| `goto *e;` | `Goto` whose `name` is the expression |
| `__attribute__((x))` | `AttributeSpecifier([Attribute("x")])` on the declaration or statement |
| a literal | `Constant` holding its source spelling (`0x5D`, `'a'`) |
| `case 1: case 2: s; t;` | `Case(1, [])`, `Case(2, [s, t])`, as pycparser groups them |
| anything else (inline assembly, GNU statement expressions) | `Opaque(kind, text, nodes)`: clang's kind, the source text, converted children |

### Side table

Nodes are keyed by identity; build new nodes rather than changing imported ones.

- `unit.sema[node]` / `unit.info(node)`: an `Info`
  - `kind`: clang's node kind (`"CStyleCastExpr"`, `"DeclRefExpr"`, ...);
  - `type`: `Type(qual, canon)`, the type as written and fully desugared;
  - `casts`: the implicit conversions clang applies to the node's value, innermost first,
    each `Cast(kind, type)` with clang's cast kind (`"IntegralCast"`, `"LValueToRValue"`);
    `converted` is the type after them;
  - `ref`: the canonical id of the declaration referenced (an `ID`, a call) or declared;
  - `begin`, `end`: `Loc(offset, line, col, toklen, file, macro, arg)`, at the macro
    expansion when the token comes from one; `spelling`: where the first token is spelled;
  - `cast`: an explicit cast's kind; `value`: a literal's or constant expression's value;
  - `group`: for a declaration, the start of the declaration statement declaring it, shared
    by `int a, b;`.
- `unit.symbols[id]` / `unit.symbol(node)`: a `Symbol` (all redeclarations merged): `kind`,
  `name`, `type`, `storage`, `attrs` (clang attribute kinds, `"PureAttr"`), `noreturn`,
  `function` (the id of the function it is local to, None at file scope), `node`.
- `unit.macros`: `{node: name}` of each outermost expansion of an object-like macro, so the
  renderer prints `R2` rather than its expansion.

`Unit` methods: `line(node)`, `span(node)` (first and last line), `range(node)` (begin and
end `Loc`; the end token runs to `end.offset + end.toklen`), `source(node)` (the text as
written, macro invocations included, or None when the node begins or ends in a macro),
`tree(type)` (a `c_ast` type tree), `qualifiers(type)` (`["volatile"]`), `text` (the file's
bytes).

## Queries

`wayward_eagle.c` (from `wayward_eagle.c.query`) works on bare `c_ast`; given a unit,
equality uses clang's declarations and types.

| Query | Result |
|---|---|
| `walk(n, stop=())`, `kids(n)`, `statements(n)` | preorder nodes; children; a block's statements |
| `calls(n)`, `callee(call)`, `arguments(call)` | calls; the called name (None through a pointer); argument expressions |
| `subscripts(n)` | `(node, base, index)` of each `a[i]` |
| `casts(n, unit, implicit=False)` | `(node, kind, from, to)` of each explicit cast, and with `implicit` each conversion clang applies |
| `stored(n)`, `variable(e)` | the lvalue an assignment or increment stores to; an lvalue that is a plain name |
| `reads(n)`, `writes(n)`, `names(n)` | variable names read, stored to, either |
| `jumps(n)`, `labels(n)`, `targets(n)` | `goto`/`break`/`continue`/`return`; label names; `goto` targets |
| `commas(n)` | comma expressions |
| `cognitive(n)` | SonarSource's cognitive complexity as clang-tidy counts it (a bare `goto` arm of an `if` adds nothing; a sequence of logical operators continues through any expression but a call) |
| `key(n, unit=None, by_name=False)`, `equal(a, b, unit=None)` | structural key and equality; with `unit`, names by declaration (`by_name`: by name and scope) and expressions by canonical type and implicit casts |

```python
from pycparser import c_ast
from wayward_eagle import c

unit = c.load("tune.c", include=["inc"])
for f in unit.functions:
    first, last = unit.span(f)                 # function source lines
    begin, end = unit.range(f)                 # byte offsets: begin.offset, end.offset + end.toklen
    for call in c.calls(f):                    # calls by callee
        sym = unit.symbol(call)                # None for a call through a pointer
        print(f.decl.name, c.callee(call), sym and sym.noreturn, unit.line(call))
    for node, base, index in c.subscripts(f):  # array subscripts
        print(unit.source(base), unit.source(index), unit.info(index).converted)
    for node, kind, frm, to in c.casts(f, unit, implicit=True):
        if kind == "IntegralCast":
            print(unit.line(node), frm.canon, "->", to.canon)
    print(sorted(c.reads(f.body)), sorted(c.writes(f.body)))

a, b = unit.functions[:2]
c.equal(a.body, b.body, unit)                  # equal trees, each name the same declaration
c.key(a.body, unit, True) == c.key(b.body, unit, True)  # equal but for which locals they name
```

## Rendering

`render(nodes, macros=None, lines=None)` prints declarations and functions with
`Generator`, then lays the text out with the pinned clang-format (`clang-format==23.1.5`,
LLVM style); `text(node, ...)` is the unformatted generator output, `format_c(source)` the
formatter alone. `Generator` subclasses pycparser's `CGenerator`:

- attributes as GNU `__attribute__((...))`;
- each node of `macros` as its macro's name;
- a `#line N "file"` marker before each statement of `lines` (`{node: (line, file)}`);
- parentheses only where C needs them or `-Wparentheses` asks for them;
- `goto *e;` and each `Opaque` node as its source text.

## Reducing

`reduce_function(fd, noreturn=frozenset(), status=None)` rewrites a `FuncDef`
([rewrites](rewrites.md)) into `[FuncDef]`, the pieces (`static`) first and `fd`'s own last;
`noreturn` names functions that never return, `status` is the type tree of exit codes
(`int` by default). `prototypes(functions)` gives the pieces' declarations.
`reduce_unit(unit, status=None, functions=None)` returns the file's text with each function
(or each named in `functions`) replaced by its pieces and their prototypes, the rest as
written.

```
wayward-eagle reduce FILE.c [-o OUT.c] [-I DIR] [--status-type T]
```
