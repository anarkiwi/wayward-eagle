"""Control flow and dataflow over statement lists: liveness, available copies, reachability,
and the rewrites they justify."""

from pycparser import c_ast

from conftest import LOCAL
from wayward_eagle.c import flow, stmt

INT = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["int"]))
TYPES = {v: INT for v in LOCAL}


def traps(st):
    """Whether ``st`` is a call of ``trap``."""
    return isinstance(st, c_ast.FuncCall) and st.name.name == "trap"


def test_live_variables_are_read_before_stored_on_some_path(cc):
    """Live into a list: read before any store on some path, through branches and loops;
    a path ending in a call that never returns reads nothing after it."""
    live = {
        "w(v1); v1 = 2; w(v1);": {"v1"},
        "v1 = 2; w(v1);": set(),
        "if (p) { v1 = 1; } w(v1);": {"v1", "p"},
        "while (p) { w(v1); v1 = q; }": {"v1", "p", "q"},
        "v1 += 1;": {"v1"},
        "goto L1; w(v1); L1:; v2 = 1;": set(),
        "switch (p) { case 1: v1 = 1; __attribute__((fallthrough)); default: w(v1); }": {
            "p",
            "v1",
        },
        "switch (p) { case 1: v1 = 1; break; default: v1 = 2; } w(v1);": {"p"},
        "do { v1 = 1; } while (p); w(v1);": {"p"},
        "for (v1 = 0; v1 < 3; v1++) { } w(v2);": {"v2"},
    }
    for src, want in live.items():
        assert flow.Graph(cc.items(src)).live_in() == want, src
    trapped = cc.items("if (p) { trap(1); w(v1); } v1 = 0;")
    assert flow.Graph(trapped, traps).live_in() == {"p"}


def test_leaving_a_list_counts_every_way_out(cc):
    """The variables live on leaving a nested list include those live where its jumps go."""
    items = cc.items("for (;;) { if (p) { v1 = 1; break; } v2 = 1; } w(v1);")
    graph = flow.Graph(items)
    arm = stmt.children(stmt.children(items[0])[0][0])[0]
    assert graph.leaving(arm) == {"v1"}
    loop = cc.items("for (;;) { w(v2); if (p) { v2 = 1; } }")
    arm = stmt.children(stmt.children(loop[0])[0][1])[0]
    assert "v2" in flow.Graph(loop).leaving(arm)


def test_copies_are_available_until_either_side_is_stored(cc):
    """``v = u`` is available until ``v`` or ``u`` is stored, and after a join only when
    it is on every path into it."""
    items = cc.items("v1 = v2; w(v1); v2 = 0; w(v1);")
    graph = flow.Graph(items)
    at = [graph.at[(id(items), i)] for i in range(4)]
    copies = graph.copies()
    assert [("v1", "v2") in copies[n] for n in at] == [False, True, True, False]
    joined = cc.items("if (p) { v1 = v2; } else { v1 = v2; } w(v1); if (q) { v1 = 3; } w(v1);")
    g = flow.Graph(joined)
    assert ("v1", "v2") in g.copies()[g.at[(id(joined), 1)]]
    assert ("v1", "v2") not in g.copies()[g.at[(id(joined), 3)]]


def test_copies_propagate_when_every_use_has_them(cc):
    """Each read of a copy ``v = u`` reads ``u`` when the copy is available at every read it
    reaches and both are of one type; the copy is then dead and goes."""
    got = flow.propagated(cc.items("v1 = p; v2 = v1; w(v2); w(v2 + 1);"), TYPES)
    assert cc.same(flow.unstored(got, LOCAL), "v1 = p; w(v1); w(v1 + 1);")
    ret = flow.propagated(cc.items("v2 = q; v1 = v2; return v1;", "int f(void)"), TYPES)
    assert cc.text(flow.unstored(ret, LOCAL)) == "{ v2 = q; return v2; }"
    for src in (
        "v2 = v1; v1 = 0; w(v2);",
        "v2 = v1; w(v2); if (p) { v2 = 3; } w(v2);",
        "v2 = v1; v2 += 1; w(v2);",
    ):
        items = cc.items(src)
        assert cc.text(flow.propagated(items, TYPES)) == cc.text(items), src
    narrow = {**TYPES, "v2": c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["char"]))}
    items = cc.items("v2 = v1; w(v2);")
    assert cc.text(flow.propagated(items, narrow)) == cc.text(items)


def test_dead_stores_of_pure_values_go(cc):
    """A store of a value of variables and constants to a local dead after it goes, a chain
    of them in turn; a store of a call's value, or to a global, stays."""
    got = flow.unstored(cc.items("v1 = p; v2 = v1; v1 = 3; w(v1); v3 = g(); p = 1;"), LOCAL)
    assert cc.same(got, "v1 = 3; w(v1); v3 = g(); p = 1;")
    looped = cc.items("v1 = 0; while (p) { w(v1); v1 = v1 + 1; }")
    assert cc.text(flow.unstored(looped, LOCAL)) == cc.text(looped)


def test_statements_never_reached_go(cc):
    """Statements after a call that never returns, a jump, or in no path from the entry go;
    a label a goto reaches stays."""
    got = flow.reachable(cc.items("if (p) { trap(1); return; } x(); return; y();"), traps)
    assert cc.same(got, "if (p) { trap(1); } x(); return;")
    jumped = cc.items("goto L1; x(); L1:; y();")
    assert cc.same(flow.reachable(jumped), "goto L1; L1:; y();")
    assert cc.same(flow.reachable(cc.items("for (;;) { x(); } y();")), "for (;;) { x(); }")


def test_killed_and_copies_of_expressions(cc):
    """Only ``=`` at the top of an expression, through commas, stores on every evaluation."""
    (comma,) = cc.items("v1 = 1, v2 = 2;")
    (cond,) = cc.items("p && (v1 = 1);")
    assert flow.killed(comma) == {"v1", "v2"} and flow.killed(cond) == set()
    assert flow.copy_of(cc.items("v1 = v2;")[0]) == ("v1", "v2")
    assert flow.copy_of(cc.items("v1 = v1;")[0]) is None
    assert flow.true(c_ast.Constant("int", "1")) and not flow.true(c_ast.Constant("int", "0"))
    assert not flow.true(c_ast.Constant("int", "1.5e"))
