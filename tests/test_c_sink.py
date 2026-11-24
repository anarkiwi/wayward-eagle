"""Forward gotos sunk into the paths falling to their label (:mod:`wayward_eagle.c.sink`)."""

from wayward_eagle.c import sink, stmt


def test_a_goto_past_one_statement_guards_it(cc):
    """A ``goto`` past the statement before its label: that statement on the path the test
    does not take, the label gone."""
    got = sink.sunk(cc.items("if (p) { x(); } else { y(); if (q) goto L1; } z(); L1:; w(1);"))
    assert cc.same(got, "if (p) { x(); z(); } else { y(); if (!q) { z(); } } w(1);")


def test_a_goto_in_a_case_breaks_and_the_breaks_take_the_tail(cc):
    """A ``goto`` in a case past what follows the ``switch`` breaks; the other cases' breaks
    and the last case's end run that first."""
    got = sink.sunk(
        cc.items(
            "switch (p) { case 0: break; case 1: v1 = 1; goto L2; default: v1 = 2; }"
            " z(); L2:; w(v1);"
        )
    )
    want = "switch (p) { case 0: z(); break; case 1: v1 = 1; break; default: v1 = 2; z(); } w(v1);"
    assert cc.same(got, want)


def test_a_goto_left_in_place(cc):
    """A label also reached from after it, a ``goto`` out of a loop, and a rewrite that
    would cost more stay as they are."""
    back = cc.items("L1:; x(); if (p) goto L1;")
    loop = cc.items("while (p) { if (q) goto L1; x(); } y(); L1:; z();")
    dear = cc.items("if (p) goto L1; if (q) { if (k) { x(); } } L1:; z();")
    for items in (back, loop, dear):
        assert cc.same(sink.sunk(items), cc.text(items))


def test_a_tail_of_code_stays_in_one_place(cc):
    """A tail with a statement of code sinks onto the one path falling to its label, but a
    ``goto`` whose tail of code would go on two paths stays; a call of variables goes on
    both."""
    once = sink.sunk(cc.items("if (q) goto L1; v1 = p + 1; L1:; w(v1);"))
    assert cc.same(once, "if (!q) { v1 = p + 1; } w(v1);")
    twice = cc.items("if (p) { x(); } else { if (q) goto L1; y(); } v1 = p + 1; L1:; w(v1);")
    assert cc.same(sink.sunk(twice), cc.text(twice))
    arms = cc.items(
        "switch (p) { case 0: break; case 1: goto L2; default: v1 = 2; } v1 = q + 1; L2:; w(v1);"
    )
    assert cc.same(sink.sunk(arms), cc.text(arms))
    calls = sink.sunk(cc.items("if (p) { x(); } else { if (q) goto L1; y(); } w(v1); L1:; z();"))
    assert cc.same(calls, "if (p) { x(); w(v1); } else { if (!q) { y(); w(v1); } } z();")


def test_sites_hold_no_code(cc):
    """Jumps, returns of a variable or constant and calls of variables and constants (their
    value possibly assigned) hold no code; other statements and a ``goto`` do."""
    sites = cc.items("for (;;) { break; } v1 = g(); w(v1); w(2); return;")
    assert all(map(stmt.is_site, [sites[0].stmt.block_items[0], *sites[1:]]))
    code = cc.items("v1 = p + 1; w(v1 + 1); L1:; goto L1;")
    assert not any(map(stmt.is_site, code))
