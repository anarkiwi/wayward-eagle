"""The rewrites over ``c_ast``, on C parsed by clang; then whole functions and files."""

from pycparser import c_ast

from conftest import LOCAL, behaves, deep
from wayward_eagle.c import listflow as flow, front, printer as render, stmt
from wayward_eagle.c import reduce as r


def test_a_label_no_local_crosses_cuts_the_body(cc):
    """What follows a label no local is used across and no goto crosses is a section."""
    body = cc.items("v1 = p; if (v1) goto L1; w(v1); L1:; v2 = p;")
    got = r.sections(body, LOCAL)
    (lab, first), (end, last) = got[0], got[-1]
    assert len(got) == 2
    assert (lab, end) == ("L1", None)
    assert cc.text(first) == cc.text(cc.items("v1 = p; if (v1) goto L1; w(v1); L1:;")[:-1])
    assert cc.same(last, "v2 = p;")


def test_a_local_used_across_a_label_keeps_one_section(cc):
    """A local read after the label it is set before keeps the body whole."""
    body = cc.items("v1 = p; L1:; w(v1);")
    assert len(r.sections(body, LOCAL)) == 1


def test_a_fall_into_a_case_before_a_label_keeps_one_section(cc):
    """A section whose exits include a fall into the next case is not cut off."""
    body = cc.items(
        "switch (p) { case 1: x(); __attribute__((fallthrough)); case 2: y(); } L1:; z();"
    )
    assert len(r.sections(body, set())) == 1


def test_a_goto_past_the_label_keeps_one_section(cc):
    """A goto from before a label to one after it, or back, keeps the body whole."""
    past = cc.items("if (p) goto L2; L1:; x(); L2:; y();")
    back = cc.items("L0:; x(); L1:; if (p) goto L0;")
    got = r.sections(past, set())
    assert len(got) == 2 and got[0][0] == "L2"
    assert len(r.sections(back, set())) == 1


def test_gotos_to_the_label_ending_a_section_return(cc):
    """Each goto to the label ending a section, nested ones included, is a return."""
    body = cc.items("if (p) goto L1; else goto L9; L1:; L9:;")[:1]
    got = r.returning_at(body, "L1")
    assert cc.text(got) == "{ if (p) { return; } else { goto L9; } }"


def test_a_last_if_is_a_guard_where_that_costs_less(cc):
    """A function's last ``if`` returns first where that lowers the cost; of one with an
    ``else``, the arm with more nested control after it; one holding none stays."""
    got = r.guarded(cc.items("x(); if (p) { z(); if (q) { y(); } }"))
    assert cc.same(got, "x(); if (!p) { return; } z(); if (q) { y(); }")
    both = cc.items("if (p) { x(); } else { if (q) { y(); if (q) { y(); } } }")
    assert cc.same(
        r.guarded(both), "if (p) { x(); return; } if (!q) { return; } y(); if (q) { y(); }"
    )
    flat = cc.items("if (p) { x(); }")
    assert cc.text(r.guarded(flat)) == cc.text(flat)


def test_an_arm_leaving_first_is_the_lighter(cc):
    """An ``if`` whose arm returns before the rest leaves first by the rest when its arm
    holds more nested control."""
    got = r.guarded(cc.items("if (p) { x(); if (q) { y(); } return; } z();"))
    assert cc.same(got, "if (!p) { z(); return; } x(); if (q) { y(); } return;")


def test_costs_follow_the_cognitive_complexity_rules(cc):
    """An ``if`` costs one plus its depth, an ``else if`` and an ``else`` one, a ``goto`` one."""
    chain = cc.items("if (p) { goto L1; } else if (q) { x(); } else { y(); } L1:;")[:1]
    assert stmt.cost(chain) == 1 + 1 + 1 + 1
    assert stmt.cost(cc.items("if (p) { if (q) { } }"), 2) == (1 + 2) + (1 + 3)


def test_an_else_ending_in_a_jump_is_tested_first(cc):
    """An ``if`` whose ``else`` ends in a jump and whose ``then`` does not tests the other
    way, nested ones included; one whose ``then`` jumps stays."""
    got = r.flattened(cc.items("if (p) { x(); if (q) { y(); } else { return; } } else { return; }"))
    assert cc.same(got, "if (!p) { return; } x(); if (!q) { return; } y();")
    kept = cc.items("if (p) { return; } else { return; }")
    assert cc.text(r.flattened(kept)) == cc.text(kept)


def test_a_loop_body_ending_in_an_if_continues(cc):
    """A loop body's last ``if`` holding nested control continues the loop when its
    condition fails; inside a ``switch`` arm the jumping arm leads."""
    got = r.flattened(cc.items("for (;;) { x(); if (p) { y(); if (q) { w(1); } } }"))
    assert cc.same(got, "for (;;) { x(); if (!p) { continue; } y(); if (q) { w(1); } }")
    sw = cc.items("switch (k) { case 1: if (p) { if (q) { w(1); } } else { return; } }")
    assert "case 1: { if (!p) { return; }" in cc.text(r.flattened(sw))


def test_a_label_the_rest_of_a_function_follows_is_a_tail(cc):
    """From the last top-level label back, each label nothing jumps back before starts a
    tail taking the variables it reads before setting them."""

    def make(t, ps):
        return stmt.call(f"t_{t}", ps)

    body = cc.items("v1 = p; v2 = p; if (p) goto L2; L1:; v2 = v1; L2:; w(v2);")
    head, chain = r.tails(body, LOCAL, make)
    assert cc.text(head) == cc.text(cc.items("v1 = p; v2 = p; if (p) goto L2; L2:;")[:-1])
    assert [(lab, ps) for lab, _, ps in chain] == [("L1", ["v1"]), ("L2", ["v2"])]
    assert cc.text(chain[0][1]) == "{ v2 = v1; }"
    back = cc.items("L1:; x(); if (p) goto L1;")
    assert r.tails(back, set(), make) == (back, [])


def test_a_function_above_the_threshold_has_its_deepest_saving_block_extracted(cc):
    """Above the threshold, the block that saves the most moves to a function of the
    variables it reads that are used outside it; under it nothing moves."""
    items = cc.items("v1 = p; if (p) { " + deep(6, "w(v1);") + " }")
    got, temps = r.extracted("f", ["p"], items, r.Function(["p"], LOCAL | {"p"}, True))
    assert stmt.cost(items) > r.THRESHOLD >= stmt.cost(got[0].items) and not temps
    assert sum(stmt.cost(x.items) for x in got) < stmt.cost(items)
    assert [(x.name, x.args, x.ret) for x in got[1:]] == [("f_x1", ["v1"], None)]
    assert "f_x1(v1);" in cc.text(got[0].items)
    small = cc.items("if (p) { " + deep(2, "x();") + " }")
    out, temps = r.extracted("g", [], small, r.Function([], set(), True))
    assert len(out) == 1 and out[0].items is small and not temps


def test_a_block_leaving_early_returns_its_exit(cc):
    """A block leaving by one kind of exit returns whether it took it; by several, a status
    its call stores and tests per exit; a return after a call that never returns is none."""

    def traps(st):
        return isinstance(st, c_ast.FuncCall) and st.name.name == "trap"

    fn = r.Function([], set(), True, traps)
    one, _ = r.extracted("f", [], cc.items("if (p) { " + deep(6, "x(); return;") + " } y();"), fn)
    assert one[1].ret == "status" and "return 1;" in cc.text(one[1].items)
    assert "if (f_x1()) { return; }" in cc.text(one[0].items)
    two = cc.items("for (;;) { if (p) { " + deep(6, "if (q) { break; } else { return; }") + " } }")
    got, temps = r.extracted("f", [], two, fn)
    assert temps == {"f_x1_k"} and got[1].ret == "status"
    trapped, _ = r.extracted(
        "f", [], cc.items("if (p) { " + deep(6, "trap(1); return;") + " } y();"), fn
    )
    assert trapped[1].ret is None


def test_a_block_setting_a_variable_used_after_returns_it(cc):
    """A block setting one variable used after it returns it, its call assigning it, and
    takes it too when some path through it leaves it unset; one that also leaves early,
    falls into the next case, or that a goto crosses, stays."""
    fn = r.Function([], LOCAL, True)
    got, _ = r.extracted("f", [], cc.items("if (p) { " + deep(6, "v1 = p;") + " } w(v1);"), fn)
    assert got[1].ret == "v1" and cc.text(got[1].items).endswith("return v1; }")
    assert "v1 = f_x1(v1);" in cc.text(got[0].items)
    every = cc.items("if (p) { v1 = q; x(); } w(v1);")
    chunk = stmt.children(every[0])[0]
    site, piece, _ = r._extraction(  # pylint: disable=protected-access
        "f_x1", chunk, stmt.swap(every, chunk, []), fn, frozenset(), flow.Graph(every)
    )
    assert cc.text(site) == "{ v1 = f_x1(); }" and piece.args == []
    both = cc.items("if (p) { " + deep(6, "v1 = p; return;") + " } w(v1);")
    fall = cc.items(
        "switch (k) { case 1: if (p) { "
        + deep(6, "x();")
        + " } __attribute__((fallthrough)); case 2: x(); }"
    )
    entered = cc.items("if (p) { " + deep(6, "L1:; x();") + " } goto L1;")
    left = cc.items("if (p) { " + deep(6, "x(); goto L1;") + " } L1:;")
    for items in (both, entered, left):
        assert len(r.extracted("f", [], items, fn)[0]) == 1
    assert len(r.extracted("f", [], fall, fn)[0]) <= 2
    assert all("fallthrough" not in cc.text(x.items) for x in r.extracted("f", [], fall, fn)[0][1:])


def test_a_function_reduces_to_sections_tails_and_extractions(cc):
    """A function returning nothing: its independent sections called in turn, a section's
    tail a function its gotos call."""
    body = cc.items(
        "v1 = p; if (v1) goto L1; w(v1); L1:; x(); L2:; v2 = p; if (v2) goto L3; w(v2);"
        " L3:; w(v2 + 1);"
    )
    pieces, secs, temps = r.reduced(body, "f", r.Function(["p"], LOCAL, True))
    assert secs == ["f_1", "f_2", "f_3"] and not temps
    assert [x.name for x in pieces] == ["f_1", "f_2", "f_3", "f_3_L3"]
    tail = {x.name: x for x in pieces}["f_3_L3"]
    assert tail.args == ["p", "v2"] and cc.text(tail.items) == "{ w(v2 + 1); }"
    kept, one, _ = r.reduced(
        cc.items("return 0;", "int g2(void)"), "g", r.Function([], set(), False)
    )
    assert one == ["g"] and len(kept) == 1 and kept[0].ret == "status"


def test_assignment_comes_before_every_read_on_every_path(cc):
    """Set before every read when each path sets it first: through both arms of an ``if``,
    not one, nor across a label; a switch with a default setting it in every arm."""
    both = cc.items("if (p) { v1 = p; } else { v1 = q; } w(v1);")
    one = cc.items("if (p) { v1 = p; } w(v1);")
    past = cc.items("v1 = p; L1:; w(v1);")
    loop = cc.items("while (v1) { v1 = p; }")
    sw = cc.items("switch (k) { default: v1 = p; } w(v1);")
    trapped = cc.items("if (p) { trap(1); } else { v1 = p; } w(v1);")
    assert stmt.assigned_first(both, "v1") and not stmt.assigned_first(one, "v1")
    assert not stmt.assigned_first(past, "v1") and not stmt.assigned_first(loop, "v1")
    assert stmt.assigned_first(sw, "v1")

    def stops(st):
        return isinstance(st, c_ast.FuncCall) and st.name.name == "trap"

    assert not stmt.assigned_first(trapped, "v1")
    assert stmt.assigned_first(trapped, "v1", stops)


def test_loops_and_switches_cost_one_plus_their_depth(cc):
    """A loop or ``switch`` costs one plus its depth, its body one level deeper."""
    loop = cc.items("for (;;) { if (p) { x(); } }")
    sw = cc.items("switch (k) { case 1: for (;;) { if (p) { x(); } } }")
    assert stmt.cost(loop) == 1 + 2 and stmt.cost(sw, 1) == 2 + 3 + 4


def test_writes_include_assignments_inside_expressions(cc):
    """A variable set inside a loop's condition or a comma expression is written, as an
    assigned, incremented or compound-assigned one is; a comparison writes nothing."""
    items = cc.items(
        "v3 = p; do { } while ((v1 > 2) && (v1 = v1 - 1, v2 = v2 - 1, 1));"
        " if (v4 == 1 && v5 <= 2) { w(v6 >= 1); } v4++; v5 += 1;"
    )
    assert stmt.writes(items) == {"v1", "v2", "v3", "v4", "v5"}
    assert stmt.reads_in(items) == {"p", "v1", "v2", "v4", "v5", "v6"}
    fn = r.Function([], LOCAL, True)
    got, _ = r.extracted(
        "f",
        [],
        cc.items("if (p) { " + deep(6, "do { } while (v1 && (v1 = v1 - 1, 1));") + " } w(v1);"),
        fn,
    )
    assert got[1].ret == "v1"


def test_a_block_reading_a_parameter_takes_it(cc):
    """A block reading a parameter nothing else reads still takes it."""
    fn = r.Function(["v1"], LOCAL, True)
    got, _ = r.extracted("f", ["v1"], cc.items("if (p) { " + deep(6, "w(v1);") + " }"), fn)
    assert got[1].args == ["v1"]


def test_an_exit_going_where_the_block_ends_is_its_end(cc):
    """A ``continue`` out of a loop body, a ``break`` out of a case body and a ``return``
    from the last arm of a function returning nothing go where falling off the block does."""
    fn = r.Function([], set(), True)
    body = cc.items("for (;;) { " + deep(6, "if (q) { continue; } x();") + " }")
    got, temps = r.extracted("f", [], body, fn)
    assert got[1].ret is None and not temps and "f_x1();" in cc.text(got[0].items)
    case = cc.items("switch (p) { case 1: " + deep(6, "if (q) { break; } x();") + " }")
    assert r.extracted("f", [], case, fn)[0][1].ret is None
    last = cc.items("y(); if (p) { " + deep(6, "if (q) { return; } x();") + " }")
    assert r.extracted("f", [], last, fn)[0][1].ret is None
    early = cc.items("if (p) { " + deep(6, "if (q) { return; } x();") + " } y();")
    assert r.extracted("f", [], early, fn)[0][1].ret == "status"


def test_extraction_outputs_are_what_runs_after_reads(cc):
    """A variable a block sets is an output only when live on leaving it: not when only
    statements before it read it, nor when each iteration of a loop around it sets it
    first; a read in the next iteration makes it one."""
    fn = r.Function([], LOCAL, True)
    got, _ = r.extracted(
        "f", [], cc.items("v1 = 0; if (p) { " + deep(6, "v1 = p; w(v1);") + " } y();"), fn
    )
    assert got[1].ret is None
    looped = cc.items("for (;;) { v1 = 0; if (p) { " + deep(6, "v1 = p; w(v1);") + " } }")
    assert [x.ret for x in r.extracted("f", [], looped, fn)[0][1:]] == [None]
    carried = cc.items("for (;;) { w(v1); if (p) { " + deep(6, "v1 = p; w(v1);") + " } }")
    assert [x.ret for x in r.extracted("f", [], carried, fn)[0][1:]] == ["v1"]


def render_all(fs, unit=None):
    """The formatted C of ``fs``."""
    return render.render(r.prototypes(fs) + fs, unit.macros if unit else None)


def test_reduce_function_declares_each_piece_and_keeps_its_signature(cc):
    """A reduced function keeps its own declaration last and voids the parameters it
    ignores; its pieces are static, take no parameter they ignore, and declare the locals
    they use where they are set."""
    f = cc.fn(
        "int v1 = p; if (v1) goto L1; w(v1); L1:; int v2 = q; if (v2) { "
        + deep(6, "w(v2);")
        + " }",
        "static void f(int a)",
    )
    got = r.reduce_function(f)
    assert got[-1].decl is f.decl and len(got) > 2
    text = render_all(got)
    assert "static void f_1(void);" in text and "(void)a;" in text and "f_1();" in text
    assert "int v1 = p;" in text


def test_reduce_function_leaves_locals_that_cannot_move(cc):
    """A function with a static or array local, or one whose address is taken, stays."""
    for body in ("static int s = 1; w(s);", "int a[2] = {1, 2}; w(a[0]);", "int v = 1; w(*&v);"):
        f = cc.fn(body)
        assert r.reduce_function(f) == [f]


def test_switch_arms_fall_through_as_written(cc):
    """An arm falling into the next still does once rendered; an arm whose ``break`` an
    extraction ends keeps leaving the switch."""
    f = cc.fn(
        "switch (k) { case 1: x(); case 2: y(); break; case 3: "
        + deep(7, "w(1);")
        + " break; default: z(); }"
    )
    text = render_all(
        r.reduce_function(f, status=c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["int"])))
    )
    assert "__attribute__((fallthrough));" in text
    body = text[text.index("case 3:") :]
    assert body.index("break;") < body.index("default:")


def test_reduce_unit_replaces_only_what_changes(cc, tmp_path):
    """The file's text outside the reduced functions is as written, macros by name."""
    path = tmp_path / "u.c"
    path.write_text(
        "#define K 3\nvoid w(int);\nint p, c0, c1, c2, c3, c4, c5, c6;\n/* kept */\n"
        "void f(void) { if (p) { " + deep(7, "w(K);") + " } }\n",
        encoding="utf-8",
    )
    unit = front.load(path)
    out = r.reduce_unit(unit)
    assert out.startswith(
        "#define K 3\nvoid w(int);\nint p, c0, c1, c2, c3, c4, c5, c6;\n/* kept */\n"
    )
    assert "w(K);" in out and "if (!p) {" in out
    assert r.reduce_unit(unit, functions=set()) == path.read_text(encoding="utf-8")
    del cc


def test_declare_places_each_local_where_it_is_set(cc):
    """A local set before each read in one arm is declared there; one read across arms is
    declared at the list's start; one first set by a statement is initialized there."""
    types = {v: c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["int"])) for v in LOCAL}
    items = cc.items(
        "if (p) { v1 = p; w(v1); } else { v2 = 1; } if (q) { v2 = 2; } w(v2); v3 = 3; w(v3);"
    )
    got = cc.text(r.declare(items, types))
    assert "if (p) { int v1 = p;" in got and got.startswith("{ int v2;") and "int v3 = 3;" in got


def test_negation_turns_comparisons_round(cc):
    """``!`` is removed, a comparison turned round, anything else negated."""
    (st,) = cc.items("if ((p < q) && !(p == 1)) { }")
    a, b = st.cond.left, st.cond.right
    assert render.text(stmt.negate(a)) == "p >= q"
    assert render.text(stmt.negate(b)) == "p == 1"
    assert render.text(stmt.negate(st.cond)).startswith("!(")


def test_any_coord_is_carried_and_never_compared(cc):
    """Statements may carry any object as ``coord`` (one neither hashable nor comparable):
    the reduced function renders as with plain coords and keeps them on what it moves."""

    class Mark:  # pylint: disable=too-few-public-methods
        """A provenance no rewrite may compare or hash."""

        __hash__ = None

        def __eq__(self, other):
            raise AssertionError("coord compared")

    src = "uint8_t v = 2; if (p) { x(); w(1); } else { y(); w(1); } for (;;) { w(v);"
    f = cc.fn(src + " if (v == 0) break; v = v - 1; }")
    plain = render_all(r.reduce_function(f))
    marks = set()
    for n in walk_all(f):
        n.coord = Mark()
        marks.add(id(n.coord))
    got = r.reduce_function(f)
    assert render_all(got) == plain
    kept = {id(n.coord) for g in got for n in walk_all(g.body)}
    assert len(kept & marks) > 5


def walk_all(n):
    """``n`` and every node under it."""
    yield n
    for _, c in n.children():
        yield from walk_all(c)


def test_a_status_piece_calls_its_join(cc, tmp_path):
    """A block leaving early by a return, its site in an ``if`` arm, calls what follows the
    site instead of returning whether to go on: it returns nothing, its site is the call and
    a return, and the join's live variable is a parameter; the reduced C does what the
    original does. One in a loop, or whose join holds code its caller keeps for another
    path, stays a status."""
    fn = r.Function([], LOCAL, True)
    body = "v1 = p; if (q) { if (p) { " + deep(6, "x(); return;") + " } } w(v1);"
    pieces, _ = r.extracted("f", [], cc.items(body), fn)
    got = r.joined(pieces, fn)
    assert pieces[1].ret == "status" and got[1].ret is None
    assert got[1].args == ["v1"] and "w(v1); return;" in cc.text(got[1].items)
    assert "return 1;" not in cc.text(got[1].items)
    assert "f_x1(v1); return;" in cc.text(got[0].items)
    src = (
        "void f(void) { int v1 = p; if (q) { if (p) { " + deep(6, "x(); return;") + " } } w(v1); }"
    )
    behaves(tmp_path, src)
    looped = cc.items("for (;;) { if (p) { " + deep(6, "x(); return;") + " } y(); }")
    kept, _ = r.extracted("f", [], looped, fn)
    assert r.joined(kept, fn)[1].ret == "status"
    code = cc.items(body.replace("w(v1);", "v1 = v1 + 1; w(v1);"))
    kept, _ = r.extracted("f", [], code, fn)
    assert r.joined(kept, fn)[1].ret == "status"
