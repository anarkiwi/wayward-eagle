"""The readability rewrites: trailing jumps, loop shapes, counted loops, common arm statements;
on statement lists, and on whole functions built and run before and after reducing."""

from pycparser import c_ast

from conftest import LOCAL, behaves, deep
from wayward_eagle.c import printer, refine, stmt
from wayward_eagle.c import reduce as r

INT = c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["int"]))


def body(text):
    """The reduced C of function ``f`` in ``text`` (from ``f``'s definition on)."""
    return " ".join(text[text.index("\nvoid f(") :].split())


def reduced(cc, src, sig="void f(void)"):
    """The rendered C of ``src``'s function reduced (not run: it may not end)."""
    got = r.reduce_function(cc.fn(src, sig), {"trap"})
    return " ".join(printer.render(r.prototypes(got) + got).split())


def test_trailing_return_and_continue_are_trimmed(cc):
    """A ``return;`` ending a function returning nothing, or ending each arm of an ``if``
    ending it, goes, as does a ``continue;`` ending a loop body; one before the end stays,
    and an ``if`` left with no arms goes when its condition has no effect."""
    got = refine.trimmed(cc.items("x(); if (p) { y(); return; } else { z(); return; }"), True)
    assert cc.same(got, "x(); if (p) { y(); } else { z(); }")
    loop = cc.items("for (;;) { if (p) { x(); continue; } y(); continue; }")
    assert cc.same(refine.trimmed(loop, False), "for (;;) { if (p) { x(); continue; } y(); }")
    assert cc.same(refine.trimmed(cc.items("x(); if (p) { return; }"), True), "x();")
    assert cc.same(refine.trimmed(cc.items("if (g()) { return; }"), True), "if (g()) { }")
    assert cc.same(refine.trimmed(cc.items("return; x();"), True), "return; x();")


def test_no_reduced_function_ends_in_return(tmp_path):
    """Sections, tails and extracted pieces end without a redundant ``return;``."""
    got = behaves(
        tmp_path,
        "void f(void) { int v1 = p; if (v1) goto L1; w(v1); if (q) goto L2; x(); return;"
        " L1:; y(); L2:; w(q); return; }",
    )
    assert "return;\n}" not in got and "f_L2();\n}" in got


def test_a_return_before_a_label_keeps_one_section(tmp_path):
    """A return that would skip the rest keeps the function from being cut at a label."""
    got = behaves(tmp_path, "void f(void) { if (p) { return; } x(); L1:; y(); }")
    assert "f_1" not in got


def test_exit_tests_become_loop_conditions(cc):
    """``for (;;)`` starting with ``if (c) break;`` tests ``!c`` first; ending with it, and
    with no ``continue``, it tests ``!c`` last; a ``continue`` keeps the loop as it is."""
    head = refine.shaped(cc.items("for (;;) { if (p) break; x(); }")[0])
    tail = refine.shaped(cc.items("for (;;) { x(); if (p == 1) break; }")[0])
    kept = cc.items("for (;;) { if (q) continue; x(); if (p) break; }")
    stepped = refine.shaped(cc.items("for (v1 = 0;; v1++) { if (v1 == p) break; x(); }")[0])
    assert cc.same([head], "while (!p) { x(); }")
    assert cc.same([tail], "do { x(); } while (p != 1);")
    assert refine.shaped(kept[0]) is kept[0]
    assert cc.same([stepped], "for (v1 = 0; v1 != p; v1++) { x(); }")


def test_conditions_with_effects_become_statements(cc):
    """A loop condition with a store or comma is evaluated by statements in ``for (;;)``,
    breaking when false; a ``do`` loop holding a ``continue`` keeps its condition."""
    do = cc.items("do { x(); } while (v1 > 0 && (v1 = v1 - 1, 1));")[0]
    assert cc.same([refine.shaped(do)], "for (;;) { x(); if (v1 <= 0) break; v1 = v1 - 1; }")
    whilst = cc.items("while ((v1 = g(), v1)) { x(); }")[0]
    assert cc.same([refine.shaped(whilst)], "for (;;) { v1 = g(); if (!v1) break; x(); }")
    skip = cc.items("do { if (p) continue; x(); } while (v1 > 0 && (v1 = v1 - 1, 1));")[0]
    assert refine.shaped(skip) is skip


def test_while_loops_with_a_counter_are_for_loops(cc):
    """dewolf's rules: a variable the condition reads, set last before the loop (other
    statements between that leave it be), stored by the same last statement of every path
    that continues and nowhere else in the body."""
    got = refine.refined(cc.items("v1 = 0; x(); while (v1 < p) { w(v1); v1 = v1 + 1; }"), LOCAL)
    assert cc.same(got, "x(); for (v1 = 0; v1 < p; v1 = v1 + 1) { w(v1); }")
    skips = cc.items("v1 = 0; while (v1 < p) { if (q) { v1++; continue; } w(v1); v1++; }")
    assert cc.same(
        refine.refined(skips, LOCAL), "for (v1 = 0; v1 < p; v1++) { if (q) { continue; } w(v1); }"
    )
    arms = cc.items("v1 = 0; while (v1 < p) { if (q) { x(); v1++; } else { v1++; } }")
    assert cc.same(refine.refined(arms, LOCAL), "for (v1 = 0; v1 < p; v1++) { if (q) { x(); } }")


def test_while_loops_that_are_not_counted_stay(cc):
    """No ``for`` when the body stores the variable elsewhere, a path continues without the
    step, the init reads what the statements after it store, or a label intervenes."""
    for src in (
        "v1 = 0; while (v1 < p) { v1 = v1 + 2; w(v1); v1 = v1 + 1; }",
        "v1 = 0; while (v1 < p) { if (q) { v1++; } }",
        "v1 = v2; v2 = 1; while (v1 < p) { v1++; }",
        "v1 = 0; L1:; while (v1 < p) { v1++; }",
        "v1 = 0; while (v1 < p) { if (q) { continue; } v1++; }",
        "while (v1 < p) { v1++; }",
    ):
        items = cc.items(src)
        assert cc.text(refine.refined(items, LOCAL)) == cc.text(items), src


def test_an_exit_test_before_the_step_moves_to_the_head(tmp_path):
    """``v = 2; for (;;) { ...; if (v == 0) break; v = v - 1; }``: ``v`` runs 2, 1, 0 and
    would be 255 after another step, so the head tests ``v != 0xFF``."""
    got = behaves(
        tmp_path,
        "void f(void) { uint8_t v = 2; for (;;) { w(v); if (v == 0) break; v = v - 1; } }",
    )
    assert "for (uint8_t v = 2; v != 0xFF; v = v - 1) { w(v); }" in body(got)


def test_a_counter_tested_through_a_cast_keeps_its_cast(tmp_path):
    """Statements common to both arms move after the ``if``; the ``do`` loop's comma becomes
    statements; the head tests the exit condition's operand, ordered as it falls."""
    got = behaves(
        tmp_path,
        "void f(void) { uint8_t v; if (p) { v = 2; } else { x(); v = 2; }"
        " do { if (g()) { return; } w(v); } while ((int8_t)v > 0 && (v = v - 1, 1)); }",
    )
    assert "if (!p) { x(); } for (uint8_t v = 2; (int8_t)v >= 0; v = v - 1)" in body(got)


def test_a_counter_that_wraps_to_its_start_is_widened(tmp_path):
    """``v`` runs 0, 2, ..., 0xFE and would wrap to 0: no test of its ``uint8_t`` value
    tells, so when nothing else mentions it, it is an ``int`` (each read the same value)."""
    got = behaves(
        tmp_path,
        "void f(void) { uint8_t v = 0; for (;;) { x(); if (p == 3) break; w(v);"
        " if (v == 0xFE) break; v = v + 2; } }",
    )
    assert "for (int v = 0; v <= 0xFE; v = v + 2) { x(); if (p == 3) { break; }" in body(got)


def test_a_counter_read_after_the_loop_gets_its_last_value(tmp_path):
    """The head form leaves the counter one step further; when it is read after the loop
    and nothing else leaves it, its last value is stored after it."""
    got = behaves(
        tmp_path,
        "void f(void) { uint8_t v = 2; for (;;) { w(v); if (v == 0) break; v = v - 1; } w(v); }",
    )
    assert "for (v = 2; v != 0xFF; v = v - 1) { w(v); } v = 0; w(v);" in body(got)


def test_loops_that_do_not_rotate_stay(cc):
    """No rotation when the counter is read after a loop another ``break`` leaves, a
    ``continue`` skips the test, the body stores it, its init is no constant, the test
    reads another variable, or a 32-bit ``int`` gives other values (here: never leaves)."""
    tail = "if (v == 0) break; v = v - 1; }"
    for src in (
        "uint8_t v = 2; for (;;) { x(); if (g()) break; w(v); " + tail + " w(v);",
        "uint8_t v = 2; for (;;) { if (g()) { continue; } " + tail,
        "uint8_t v = 2; for (;;) { v = v + q; " + tail,
        "uint8_t v = p; for (;;) { w(v); " + tail,
        "uint8_t v = 2; for (;;) { w(v); if (v == q) break; v = v - 1; }",
        "uint16_t v = 0; for (;;) { w(1); if (v + 1 == 0) break; v = v + 1; }",
    ):
        assert "for (;;)" in reduced(cc, src), src


def test_statements_ending_every_arm_move_after_it(tmp_path):
    """The statement ending every arm of an ``if``/``else`` chain follows it once."""
    got = behaves(
        tmp_path,
        "void f(void) { if (p) { x(); w(1); } else if (q) { y(); w(1); } else { w(1); } }",
    )
    assert "if (p) { x(); } else if (q) { y(); } w(1); }" in body(got)


def test_statements_leading_every_arm_move_before_it(tmp_path):
    """The statement leading every arm runs first when the conditions have no effect,
    read only the function's own variables, and it stores none they read."""
    got = behaves(
        tmp_path,
        "void f(int a) { int v1; if (a == 1) { v1 = 3; w(v1); } else { v1 = 3; w(v1 + 1); } }",
        "f(p)",
    )
    assert "int v1 = 3; if (a != 1) { w(v1 + 1); return; } w(v1);" in " ".join(got.split())


def test_switch_arms_ending_alike_end_after_it(tmp_path):
    """A ``switch`` with a ``default``, each arm ending in the same statement and ``break``,
    runs it after the ``switch``; an arm leaving early by ``break`` keeps it."""
    got = behaves(
        tmp_path,
        "void f(void) { switch (p) { case 0: x(); w(1); break; case 1: y(); w(1); break;"
        " default: w(1); } }",
    )
    assert "case 1: { y(); break; } default: { } } w(1); }" in body(got)
    early = behaves(
        tmp_path,
        "void f(void) { switch (p) { case 0: if (q) break; x(); w(1); break;" " default: w(1); } }",
    )
    assert "x(); w(1); break; } default: { w(1); } } }" in body(early)


def test_common_statements_stay_where_moving_them_may_change_what_runs(cc):
    """Nothing moves before conditions with an effect, nor a call before conditions reading
    memory, nor a store to a variable the conditions read."""
    for src in (
        "if (g()) { x(); y(); } else { x(); z(); }",
        "if (p) { x(); y(); } else { x(); z(); }",
        "if (a) { a = 0; w(1); } else { a = 0; w(2); }",
    ):
        st = cc.items(src, "void f(int a)")[0]
        lead, kept, tail = refine.common(st, LOCAL | {"a"})
        assert not lead and not tail and kept is st, src
    same = cc.items("if (p) { x(); } else { x(); }")[0]
    assert refine.common(same, LOCAL)[1] is same


def test_extracted_names_avoid_names_taken(cc):
    """A piece is named past the names given as taken."""
    fn = r.Function([], LOCAL, True, taken=frozenset({"f_x1"}))
    got, _ = r.extracted("f", [], cc.items("if (p) { " + deep(6, "w(v1);") + " }"), fn)
    assert [x.name for x in got] == ["f", "f_x2"]


def test_steps_of_other_variables_are_the_for_step(cc):
    """Steps after the exit test that store other variables dead after the loop join the
    ``for`` step; one live after it keeps the loop."""
    src = "uint8_t v = 0x62, u = 2; do { w(u); } while (v >= 0x31 && (v = v - 0x31, u = u - 1, 1));"
    assert "for (uint8_t v = 0x62; v != 0xCF; v = v - 0x31, u = u - 1)" in reduced(cc, src)
    assert "for (;;)" in reduced(cc, src + " w(u);")


def test_types_of_counted_loops(cc):
    """Each counter keeps its own type tree; a widened one is ``int``."""
    items = cc.items("v1 = 0; for (;;) { w(v1); if (v1 == 3) break; v1 = v1 + 1; }")
    types = {v: INT for v in LOCAL}
    got = refine.Rotation(types)(items)
    assert cc.same(got, "for (v1 = 0; v1 <= 3; v1 = v1 + 1) { w(v1); }")
    assert stmt.cost(got) == stmt.cost(items) - 2
