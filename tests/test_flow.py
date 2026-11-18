"""Control flow and dataflow of C function bodies over the C layer."""

import pytest
from pycparser import c_ast

from wayward_eagle import c
from wayward_eagle.c import flow

HEAD = "int g;\nvoid tick(void);\n__attribute__((noreturn)) void die(void);\n"


def load(tmp_path, body):
    """The unit of C ``body`` after the common declarations, and its last function."""
    path = tmp_path / "t.c"
    path.write_text(HEAD + body + "\n", encoding="utf-8")
    unit = c.load(path)
    return unit, unit.functions[-1]


def vertex(unit, g, text):
    """The vertex whose node is spelled ``text``."""
    return next(i for i, n in enumerate(g.nodes) if n is not None and unit.source(n) == text)


def names(unit, ids):
    """The names of symbol ids ``ids``."""
    return {unit.symbols[i].name for i in ids}


def test_if_else_joins(tmp_path):
    """Both arms of an if/else enter the join after it, which goes on to the next statement."""
    unit, f = load(tmp_path, "void f(int c) { if (c) { tick(); } else { g = 1; } g = 2; }")
    g = flow.build(f.body)
    (join,) = g.joins()
    assert isinstance(g.origin[join], c_ast.If)
    assert {unit.source(g.nodes[p]) for p in g.pred[join]} == {"tick()", "g = 1"}
    assert g.succ[join] == [vertex(unit, g, "g = 2")]


@pytest.mark.parametrize(
    "body, back",
    [
        ("void f(int c) { while (c) { tick(); } }", "tick()"),
        ("void f(int c) { do { tick(); } while (c); }", "c"),
        ("void f(int c) { for (c = 0; c < 3; c++) { tick(); } }", "c++"),
        ("void f(void) { for (;;) { if (g) { break; } tick(); } }", "tick()"),
        ("void f(int c) { while (c) { if (g) { continue; } tick(); } }", "tick()"),
    ],
)
def test_loop_heads_join_entry_and_back_edges(tmp_path, body, back):
    """A loop's head is a join of the code before it and its back edges."""
    unit, f = load(tmp_path, body)
    g = flow.build(f.body)
    heads = [j for j in g.joins() if isinstance(g.origin[j], c.query.LOOPS)]
    assert len(heads) == 1
    assert back in {unit.source(g.nodes[p]) for p in g.pred[heads[0]] if g.nodes[p] is not None}


def test_switch_cases_and_fallthrough(tmp_path):
    """A switch enters each case; a case without ``break`` falls into the next; without a
    ``default`` the switch may skip every case."""
    unit, f = load(
        tmp_path, "void f(int c) { switch (c) { case 1: tick(); case 2: g = 1; break; } g = 2; }"
    )
    g = flow.build(f.body)
    test = vertex(unit, g, "c")
    cases = [s for s in g.succ[test] if isinstance(g.origin[s], c_ast.Case)]
    assert len(cases) == 2 and len(g.succ[test]) == 3
    assert cases[1] in g.succ[vertex(unit, g, "tick()")]


def test_gotos_noreturn_and_unreachable(tmp_path):
    """A goto enters its label, a computed goto every label, a noreturn call nothing; code
    after a return is unreachable."""
    unit, f = load(
        tmp_path,
        "void f(int c) { void *p = &&a; if (c) { goto *p; } if (g) { die(); } goto b;"
        " a: tick(); b: g = 1; return; g = 2; }",
    )
    g = flow.build(f.body, lambda call: unit.symbol(call).noreturn)
    computed = next(i for i, n in enumerate(g.nodes) if isinstance(n, c_ast.Goto))
    labels = {g.origin[s].name for s in g.succ[computed]}
    assert labels == {"a", "b"}
    assert g.succ[vertex(unit, g, "die()")] == []
    assert vertex(unit, g, "g = 2") not in g.reachable


def test_reaching_and_liveness(tmp_path):
    """Definitions reach uses along paths no other definition of their variable cuts; a
    variable is live where a path reads it before a store."""
    unit, f = load(
        tmp_path, "int f(int a, int c) { int v = a; if (c) { v = 2; } a = 0; return v; }"
    )
    fl = flow.Flow(unit, f)
    ret = next(i for i, n in enumerate(fl.cfg.nodes) if isinstance(n, c_ast.Return))
    got = [fl.defs[i] for i in flow.bits(fl.reaching()[ret])]
    reaching = {unit.source(d.node) if d.node else unit.symbols[d.var].name for d in got}
    assert reaching == {"int v = a", "v = 2", "a = 0", "c"}
    live = fl.liveness()
    assert names(unit, live[ret]) == {"v"}
    assert names(unit, live[vertex(unit, fl.cfg, "c")]) == {"c", "v"}
    assert names(unit, live[fl.cfg.entry]) == {"a", "c"}


def test_owned_variables(tmp_path):
    """A function owns its automatic scalars and parameters, not statics, arrays, volatiles
    or variables whose address is taken."""
    unit, f = load(
        tmp_path,
        "void f(int a, int b) { int x = 0, y = 1; static int s; int r[2] = {0};"
        " volatile int w = 0; int *p = &b; g = a + x + y + s + r[0] + w + *p; }",
    )
    assert names(unit, flow.Flow(unit, f).own) == {"a", "x", "y", "p"}


def test_available_copies_meet_at_joins(tmp_path):
    """A copy made on every path into a join is available after it; on one path, not."""
    unit, f = load(
        tmp_path,
        "int f(int a, int c) { int v, w; if (c) { v = a; w = a; } else { v = a; } return v + w; }",
    )
    fl = flow.Flow(unit, f)
    pairs = fl.pairs()
    ret = next(i for i, n in enumerate(fl.cfg.nodes) if isinstance(n, c_ast.Return))
    into, _ = fl.available(pairs)
    held = {
        (unit.symbols[v].name, unit.symbols[s[1]].name)
        for (v, s), b in pairs.items()
        if into[ret] >> b & 1
    }
    assert held == {("v", "a")}


def test_solve_backward_may(tmp_path):
    """A backward may problem flows facts from uses up to the entry."""
    unit, f = load(tmp_path, "void f(int c) { tick(); if (c) { g = 1; } }")
    g = flow.build(f.body)
    gen = [0] * len(g.nodes)
    gen[vertex(unit, g, "g = 1")] = 1
    into, out = flow.solve(g, gen, [0] * len(g.nodes), forward=False)
    assert into[g.entry] == 1 and into[vertex(unit, g, "tick()")] == 1
    assert out[vertex(unit, g, "g = 1")] == 0
