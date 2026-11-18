"""The C layer: clang's AST as ``c_ast`` with its side table, the queries, and the renderer."""

import pytest
from pycparser import c_ast

from wayward_eagle import c
from wayward_eagle.cli import main

SRC = r"""#include <stdint.h>
#define BASE (buf + 2)
#define TWICE(x) ((x) + (x))
typedef struct pair { int a; uint8_t b; } pair;
union u { int i; char c; };
enum e { E1 = 1, E2 };
uint8_t buf[8] = {1, 2, 3};
volatile uint8_t port;
__attribute__((noreturn, leaf)) void trap(uint16_t k);
int __attribute__((pure)) look(int);
int look(int);
static __attribute__((noinline)) uint8_t f(uint8_t a, pair *p) {
  uint8_t v = (uint8_t)(a + 1), w = BASE[a];
  int n = sizeof(pair) + sizeof v;
  const char *s = "hi";
  pair q = (pair){1, 'x'};
  for (int i = 0; i < 3; i++) {
    v += p->a + q.b + s[0];
  }
  switch (v) {
  case 1:
  case 2:
    v = TWICE(v);
    __attribute__((fallthrough));
  default:
    break;
  }
  if (look(v) > E2 ? port : v)
    goto out;
  while (v--) {
    if (!v)
      continue;
    v = (v, w);
  }
  do v++; while (v < 3);
out:
  asm volatile("nop");
  return n ? (uint8_t)look(v) : (uint8_t)(int8_t)v;
}
void g(int k) {
  static void *t[] = {&&a};
  goto *t[k];
a:;
}
"""


@pytest.fixture(name="unit")
def fixture_unit(tmp_path):
    """The unit of :data:`SRC`."""
    path = tmp_path / "t.c"
    path.write_text(SRC, encoding="utf-8")
    return c.load(path)


def fn(unit, name):
    """The definition of function ``name``."""
    return next(f for f in unit.functions if f.decl.name == name)


def test_main_file_declarations_only(unit):
    """Only the file's own declarations convert; its functions are its definitions."""
    assert [f.decl.name for f in unit.functions] == ["f", "g"]
    kinds = [type(d).__name__ for d in unit.ast.ext]
    assert kinds[:5] == ["Decl", "Typedef", "Decl", "Decl", "Decl"]
    assert not any(getattr(d, "name", "") == "uint8_t" for d in unit.ast.ext)


def test_symbols_merge_redeclarations(unit):
    """A declaration's attributes, storage and whether it returns merge over its
    redeclarations; a local knows its function."""
    syms = {s.name: s for s in unit.symbols.values()}
    assert syms["trap"].noreturn and "LeafAttr" in syms["trap"].attrs
    assert "PureAttr" in syms["look"].attrs
    assert syms["f"].storage == "static" and syms["v"].function == syms["f"].id
    assert syms["buf"].function is None


def test_side_table_types_casts_and_references(unit):
    """Each expression has clang's type, the implicit casts applied to its value, and the
    declaration it names."""
    f = fn(unit, "f")
    ids = [n for n in c.walk(f) if isinstance(n, c_ast.ID) and n.name == "a"]
    info = unit.info(ids[0])
    assert info.type == c.Type("uint8_t", "unsigned char")
    assert [x.kind for x in info.casts] == ["LValueToRValue", "IntegralCast"]
    assert info.converted.canon == "int" and unit.symbol(ids[0]).kind == "ParmVarDecl"
    explicit = {(k, frm.canon, to.canon) for _, k, frm, to in c.casts(f, unit)}
    assert ("IntegralCast", "int", "unsigned char") in explicit
    implicit = {k for _, k, _, _ in c.casts(f, unit, implicit=True)}
    assert {"ArrayToPointerDecay", "FunctionToPointerDecay", "IntegralCast"} <= implicit
    assert "volatile" in unit.qualifiers(
        unit.info(next(n for n in c.walk(f) if getattr(n, "name", None) == "port")).type
    )


def test_source_ranges(unit):
    """A function's lines, byte range and source text are clang's."""
    f = fn(unit, "f")
    first, last = unit.span(f)
    assert SRC.splitlines()[first - 1].startswith("static") and SRC.splitlines()[last - 1] == "}"
    begin, end = unit.range(f)
    assert SRC.encode()[begin.offset : end.offset + end.toklen].decode() == unit.source(f)
    assert unit.source(f).endswith("(int8_t)v;\n}")
    macro = next(n for n, name in unit.macros.items() if name == "BASE")
    assert unit.source(macro) is None and unit.line(macro) == 13


def test_queries(unit):
    """Calls by callee, subscripts with base and index, jumps, labels, commas, reads and
    writes come from the nodes."""
    f = fn(unit, "f")
    assert sorted({c.callee(x) for x in c.calls(f)}) == ["look"]
    assert [c.arguments(x)[0].name for x in c.calls(f)] == ["v", "v"]
    subs = [(type(b).__name__, type(i).__name__) for _, b, i in c.subscripts(f)]
    assert ("BinaryOp", "ID") in subs and ("ID", "Constant") in subs
    assert c.labels(f) == {"out"} and c.targets(f) == {"out"}
    assert {type(j).__name__ for j in c.jumps(f)} == {"Goto", "Break", "Continue", "Return"}
    assert len(c.commas(f)) == 1
    assert {"v", "w", "n", "i", "q", "s"} <= c.writes(f)
    assert "a" in c.reads(f) and "look" not in c.reads(f)
    first = f.body.block_items[0]
    assert c.stored(first) is None and c.variable(c_ast.ID("x")).name == "x"
    assert not c.statements(None) and c.statements(first) == [first]
    assert c.kids(first) == [x for _, x in first.children()]


def test_equality_of_subtrees(unit):
    """Subtrees are equal by structure; with the unit, by declarations and types too."""
    f = fn(unit, "f")
    vs = [n for n in c.walk(f) if isinstance(n, c_ast.ID) and n.name == "v"]
    w = next(n for n in c.walk(f) if isinstance(n, c_ast.ID) and n.name == "w")
    assert c.equal(vs[0], vs[1]) and not c.equal(vs[0], w)
    assert c.key(vs[0], unit)[1][1] == c.key(vs[1], unit)[1][1] != c.key(w, unit)[1][1]
    assert c.key(vs[0], unit, True)[1][:2] == ("v", False)


def test_opaque_and_computed_goto(unit):
    """Inline assembly is opaque with its text; a computed goto names its expression."""
    f, g = fn(unit, "f"), fn(unit, "g")
    (asm,) = [n for n in c.walk(f) if isinstance(n, c.Opaque)]
    assert asm.kind == "GCCAsmStmt" and asm.text == 'asm volatile("nop")'
    (goto,) = c.jumps(g)
    assert isinstance(goto.name, c_ast.ArrayRef) and c.targets(g) == set()
    assert "goto *t[k];" in c.text(g) and "{&&a}" in c.text(g)


def test_render_round_trips(unit, tmp_path):
    """The rendered file parses to the same trees: GNU attributes, labels, switch arms,
    casts and the minimal parentheses all mean what they did."""
    out = tmp_path / "r.c"
    text = c.render(unit.ast.ext, unit.macros)
    assert "__attribute__((noinline)) static uint8_t f(" in text and "BASE[a]" in text
    out.write_text("#include <stdint.h>\n#define BASE (buf + 2)\n" + text, encoding="utf-8")
    again = c.load(out)
    for a, b in zip(unit.functions, again.functions):
        assert c.key(a) == c.key(b)


def test_line_markers_and_errors(unit, tmp_path):
    """A ``#line`` marker precedes a statement given one; C clang rejects is an error."""
    f = fn(unit, "g")
    goto = f.body.block_items[1]
    assert c.text(f, lines={goto: (7, "x.c")}).count('#line 7 "x.c"') == 1
    bad = tmp_path / "bad.c"
    bad.write_text("void f(void) { undeclared(); }\n", encoding="utf-8")
    with pytest.raises(c.CError):
        c.load(bad)
    assert c.command(c.NARROW)[-1] == "-nostdlibinc" and c.dump(bad.parent / "t.c")


def test_narrow_target(tmp_path):
    """The narrow target's ``int`` is 16 bits and its exact-width types are its own."""
    path = tmp_path / "n.c"
    path.write_text("#include <stdint.h>\nint32_t x;\nint y = sizeof(int);\n", encoding="utf-8")
    unit = c.load(path, target=c.NARROW)
    x = unit.ast.ext[0]
    assert unit.info(x).type.canon == "long" and unit.target == c.NARROW


def test_cli_reduce(tmp_path, capsys):
    """``wayward-eagle reduce`` prints or writes the file with its functions reduced."""
    path = tmp_path / "t.c"
    path.write_text(
        "void w(int);\nvoid f(int p) { if (p) { if (p > 1) { w(1); } } }\n", encoding="utf-8"
    )
    assert main(["reduce", str(path)]) == 0
    assert "if (!p) {" in capsys.readouterr().out
    out = tmp_path / "o.c"
    assert main(["reduce", str(path), "-o", str(out), "--status-type", "unsigned char"]) == 0
    assert "if (!p) {" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "body, score",
    [
        ("if (g) { if (g > 1) { g++; } } else if (g < 0) { g--; } else { g = 0; }", 5),
        ("while (g) { g = g ? 1 : 2; }", 1 + 2),
        ("for (;;) { switch (g) { case 1: break; } }", 1 + 2),
        ("do { g++; } while (g && g < 3 && g != 2 || g > 9);", 1 + 2),
        ("if (!(g && g > 1) && g) { }", 1 + 1),
        ("g = (g ? (g && g) : 2) && g;", 1 + 1),
        ("g = (g && g) || (g && g);", 3),
        ("g = f(g && g) && g;", 2),
        ("l:; if (g) { goto l; }", 2),
        ("l:; if (g) goto l; else goto l;", 2),
        ("l:; while (g) goto l;", 2),
        ("g = 1;", 0),
    ],
)
def test_cognitive_complexity(tmp_path, body, score):
    """SonarSource's rules as clang-tidy counts them: a bare ``goto`` arm of an ``if`` adds
    nothing, and a sequence of logical operators continues through any expression but a
    call."""
    path = tmp_path / "t.c"
    path.write_text(f"int g;\nint f(int);\nint h(void) {{ {body} return 0; }}\n", encoding="utf-8")
    (f,) = c.load(path).functions
    assert c.query.cognitive(f.body) == score


@pytest.mark.parametrize(
    "node,want",
    [
        (
            c_ast.BinaryOp("-", c_ast.BinaryOp("+", c_ast.ID("a"), c_ast.ID("b")), c_ast.ID("c")),
            "a + b - c",
        ),
        (
            c_ast.BinaryOp("-", c_ast.ID("a"), c_ast.BinaryOp("+", c_ast.ID("b"), c_ast.ID("c"))),
            "a - (b + c)",
        ),
        (
            c_ast.BinaryOp("<<", c_ast.BinaryOp(">>", c_ast.ID("a"), c_ast.ID("b")), c_ast.ID("c")),
            "a >> b << c",
        ),
        (
            c_ast.BinaryOp("==", c_ast.BinaryOp("!=", c_ast.ID("a"), c_ast.ID("b")), c_ast.ID("c")),
            "(a != b) == c",
        ),
        (
            c_ast.TernaryOp(
                c_ast.BinaryOp(">", c_ast.ID("v"), c_ast.ID("k")),
                c_ast.ID("x"),
                c_ast.TernaryOp(c_ast.ID("c"), c_ast.ID("d"), c_ast.ID("e")),
            ),
            "v > k ? x : c ? d : e",
        ),
        (
            c_ast.TernaryOp(
                c_ast.TernaryOp(c_ast.ID("c"), c_ast.ID("d"), c_ast.ID("e")),
                c_ast.Assignment("=", c_ast.ID("x"), c_ast.ID("y")),
                c_ast.ID("z"),
            ),
            "(c ? d : e) ? (x = y) : z",
        ),
    ],
)
def test_parentheses_where_precedence_or_readers_need_them(node, want):
    """Left operands of one precedence group associate unparenthesized; a conditional
    operator's operands are parenthesized only where they bind no tighter."""
    assert c.text(node) == want
