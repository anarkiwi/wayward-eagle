"""Rewrites of a C statement tree: sections, tails, early exits, flattened arms, extraction."""

from wayward_eagle import (
    Function,
    Piece,
    assigned_first,
    reduced,
    CONTINUE,
    RETURN,
    THRESHOLD,
    cost,
    extracted,
    flattened,
    guarded,
    returning,
    sections,
    tails,
    walk,
)


def a(name, *reads):
    """An assignment of ``name`` reading ``reads``."""
    return ("a", name, " + ".join(reads) or "0", frozenset(reads))


def s(text, *reads):
    """A statement reading ``reads``."""
    return ("s", text, frozenset(reads))


def goto(name):
    """A jump to label ``name``."""
    return ("j", f"goto {name};", frozenset())


def label(name):
    """The label ``name``."""
    return ("l", f"{name}:;", frozenset())


def iff(cond, then, els=(), neg=None):
    """An ``if`` of ``cond`` (``neg`` its negation)."""
    return ("if", cond, frozenset(), list(then), list(els), neg or f"!({cond})")


def test_a_label_no_local_crosses_cuts_the_body():
    """What follows a label no local is used across and no goto crosses (but into it from
    before) is a section of its own."""
    body = [a("v1", "p"), iff("v1", [goto("L1")]), s("w(v1);", "v1"), label("L1"), a("v2", "p")]
    assert sections(body, {"v1", "v2"}) == [("L1", body[:3]), (None, body[4:])]


def test_a_local_used_across_a_label_keeps_one_section():
    """A local read after the label it is set before keeps the body whole."""
    body = [a("v1", "p"), label("L1"), s("w(v1);", "v1")]
    assert sections(body, {"v1"}) == [(None, body)]


def test_a_goto_past_the_label_keeps_one_section():
    """A goto from before the label to one after it, or back from after it, keeps the body
    whole."""
    past = [iff("p", [goto("L2")]), label("L1"), s("x();"), label("L2"), s("y();")]
    back = [label("L0"), s("x();"), label("L1"), iff("p", [goto("L0")])]
    assert len(sections(past, set())) == 2 and sections(past, set())[0][0] == "L2"
    assert len(sections(back, set())) == 1


def test_gotos_to_the_label_ending_a_section_return():
    """Each goto to the label ending a section, nested ones included, is a return."""
    got = returning([iff("p", [goto("L1")], [goto("L9")])], "L1")
    assert got[0][3] == [RETURN] and got[0][4] == [goto("L9")]


def test_a_last_if_is_a_guard_where_that_costs_less():
    """A function's last ``if`` returns first and holds its statements after it where that
    lowers the cognitive complexity (it holds nested control); of one with an ``else``, the
    arm with more nested control is after it; a last ``if`` holding none stays."""
    inner = iff("q", [s("y();")], neg="!q")
    got = guarded([s("x();"), iff("p", [s("z();"), inner], neg="!p")])
    assert got == [s("x();"), ("if", "!p", frozenset(), [RETURN], [], "p"), s("z();"), inner]
    both = [iff("p", [s("x();")], [iff("q", [s("y();"), inner], neg="!q")], neg="!p")]
    assert guarded(both) == [
        ("if", "p", frozenset(), [s("x();"), RETURN], [], "!p"),
        ("if", "!q", frozenset(), [RETURN], [], "q"),
        s("y();"),
        inner,
    ]
    flat = [iff("p", [s("x();")], neg="!p")]
    assert guarded(flat) == flat


def test_an_arm_leaving_first_is_the_lighter():
    """An ``if`` whose arm returns before the rest of a function holds more nested control
    than the rest leaves first by the rest, the arm after it."""
    heavy = [s("x();"), iff("q", [s("y();")], neg="!q"), RETURN]
    got = guarded([iff("p", heavy, neg="!p"), s("z();")])
    assert got == [("if", "!p", frozenset(), [s("z();"), RETURN], [], "p"), *heavy]


def test_costs_follow_the_cognitive_complexity_rules():
    """An ``if`` costs one plus its depth, an ``else if`` and an ``else`` one, a ``goto`` one."""
    chain = iff("p", [goto("L1")], [iff("q", [s("x();")], [s("y();")])])
    assert cost([chain]) == 1 + 1 + 1 + 1
    assert cost([iff("p", [iff("q", [])])], 2) == (1 + 2) + (1 + 3)


def test_an_else_ending_in_a_jump_is_tested_first():
    """An ``if`` whose ``else`` ends in a jump and whose ``then`` does not tests the other
    way, the jump first and the ``then`` after it, nested ones included; one whose ``then``
    jumps stays."""
    got = flattened(
        [iff("p", [s("x();"), iff("q", [s("y();")], [RETURN], neg="!q")], [RETURN], neg="!p")]
    )
    assert got == [
        ("if", "!p", frozenset(), [RETURN], [], "p"),
        s("x();"),
        ("if", "!q", frozenset(), [RETURN], [], "q"),
        s("y();"),
    ]
    kept = [iff("p", [RETURN], [RETURN])]
    assert flattened(kept) == kept


def test_a_loop_body_ending_in_an_if_continues():
    """A loop body's last ``if`` holding nested control continues the loop when its
    condition fails, its statements after it."""
    inner = iff("q", [s("w();")], neg="!q")
    loop = ("loop", "for", "", frozenset(), [s("x();"), iff("p", [s("y();"), inner], neg="!p")])
    body = flattened([loop])[0][4]
    assert body == [s("x();"), ("if", "!p", frozenset(), [CONTINUE], [], "p"), s("y();"), inner]
    switch = ("switch", "k", frozenset(), [((1,), [iff("p", [inner], [RETURN], neg="!p")])])
    assert flattened([switch])[0][3][0][1][0][3] == [RETURN]


def test_a_label_the_rest_of_a_function_follows_is_a_tail():
    """From the last top-level label back, each label nothing after it jumps back before
    starts a tail; its parameters are the variables it reads before setting them, the next
    tail's included, and a variable it sets first is none."""

    def call(t, ps):
        return ("s", f"t_{t}({', '.join(ps)});", frozenset(ps))

    body = [
        a("v1", "p"),
        a("v2", "p"),
        iff("p", [goto("L2")]),
        label("L1"),
        a("v2", "v1"),
        label("L2"),
        s("w(v2);", "v2"),
    ]
    head, chain = tails(body, {"v1", "v2"}, call)
    assert head == body[:3]
    assert chain == [("L1", [body[4]], ["v1"]), ("L2", [body[6]], ["v2"])]
    back = [label("L1"), s("x();"), iff("p", [goto("L1")])]
    assert tails(back, set(), call) == (back, [])


def traps(st):
    """Whether ``st`` is a call of ``trap``, which never returns."""
    return st[0] == "s" and st[1].startswith("trap(")


def deep(n, *body):
    """``n`` nested ``if`` statements around ``body``."""
    out = list(body)
    for k in range(n):
        out = [iff(f"c{k}", out)]
    return out


def test_a_function_above_the_threshold_has_its_deepest_saving_block_extracted():
    """Above :data:`THRESHOLD`, the nested block that saves the most moves to a function of
    the variables it reads that are used outside it; under it nothing moves."""
    items = [a("v1", "p"), iff("p", deep(6, s("w(v1);", "v1")))]
    got, temps = extracted("f", ["p"], items, Function(["p"], {"v1", "p"}, True))
    assert cost(items) > THRESHOLD >= cost(got[0].items) and got[0].args == ["p"] and not temps
    assert sum(cost(x.items) for x in got) < cost(items)
    assert [(x.name, x.args, x.ret) for x in got[1:]] == [("f_x1", ["v1"], None)]
    assert ("s", "f_x1(v1);", frozenset({"v1"})) in walk(got[0].items)
    small = [iff("p", deep(2, s("x();")))]
    assert extracted("g", [], small, Function([], set(), True)) == ([Piece("g", [], small)], set())


def test_a_block_leaving_early_returns_its_exit():
    """A block leaving by one kind of exit returns whether it took it, its call tested; by
    several, a status its call stores and tests per exit; a return after a call that never
    returns is no exit."""
    fn = Function([], set(), True, traps)
    one, _ = extracted("f", [], [iff("p", deep(6, s("x();"), RETURN))], fn)
    assert one[1].ret == "status" and ("j", "return 1;", frozenset()) in walk(one[1].items)
    assert ("if", "f_x1()", frozenset(), [RETURN], [], "!f_x1()") in walk(one[0].items)
    brk = ("j", "break;", frozenset())
    two = [("loop", "for", "", frozenset(), [iff("p", deep(6, iff("q", [brk], [RETURN])))])]
    got, temps = extracted("f", [], two, fn)
    assert temps == {"f_x1_k"} and got[1].ret == "status"
    trapped, _ = extracted("f", [], [iff("p", deep(6, s("trap(1);"), RETURN))], fn)
    assert trapped[1].ret is None


def test_a_block_setting_a_variable_used_after_returns_it():
    """A block setting one variable used after it returns it, its call assigning it; one
    that also leaves early, falls into the next case, or that a goto outside enters, stays."""
    fn = Function([], {"v1"}, True)
    got, _ = extracted("f", [], [iff("p", deep(6, a("v1", "p"))), s("w(v1);", "v1")], fn)
    assert got[1].ret == "v1" and got[1].items[-1] == ("j", "return v1;", frozenset())
    assert ("a", "v1", "f_x1(v1)", frozenset({"v1"})) in walk(got[0].items)
    both = [iff("p", deep(6, a("v1", "p"), RETURN)), s("w(v1);", "v1")]
    fall = [iff("p", deep(6, s("x();"), ("j", "__attribute__((fallthrough));", frozenset())))]
    entered = [iff("p", deep(6, label("L1"), s("x();"))), goto("L1")]
    for items in (both, fall, entered):
        assert len(extracted("f", [], items, fn)[0]) == 1


def test_a_function_reduces_to_sections_tails_and_extractions():
    """A function returning nothing: its independent sections called in turn, a section's
    tail a function its gotos call, and each piece flattened."""
    body = [
        a("v1", "p"),
        iff("v1", [goto("L1")]),
        s("w(v1);", "v1"),
        label("L1"),
        s("x();"),
        label("L2"),
        a("v2", "p"),
        iff("v2", [goto("L3")]),
        s("y(v2);", "v2"),
        label("L3"),
        s("z(v2);", "v2"),
    ]
    pieces, sections_, temps = reduced(body, "f", Function(["p"], {"v1", "v2"}, True))
    assert sections_ == 3 and not temps
    assert [x.name for x in pieces] == ["f_1", "f_2", "f_3", "f_3_L3"]
    tail = {x.name: (x.args, x.items) for x in pieces}["f_3_L3"]
    assert tail == (["p", "v2"], [s("z(v2);", "v2")])
    kept, one, _ = reduced([RETURN], "g", Function([], set(), False))
    assert one == 1 and kept == [Piece("g", [], [RETURN], "status")]


def test_assignment_comes_before_every_read_on_every_path():
    """A variable is set before every read when each path sets it first: through both arms
    of an ``if``, not through one, nor across a label control may reach from anywhere."""
    both = [iff("p", [a("v1", "p")], [a("v1", "q")]), s("w(v1);", "v1")]
    one = [iff("p", [a("v1", "p")]), s("w(v1);", "v1")]
    past = [a("v1", "p"), label("L1"), s("w(v1);", "v1")]
    loop = [("loop", "while", "c", frozenset({"v1"}), [a("v1", "p")])]
    switch = [("switch", "k", frozenset(), [((None,), [a("v1", "p")])]), s("w(v1);", "v1")]
    assert assigned_first(both, "v1") and not assigned_first(one, "v1")
    assert not assigned_first(past, "v1") and not assigned_first(loop, "v1")
    assert assigned_first(switch, "v1")


def test_loops_and_switches_cost_one_plus_their_depth():
    """A loop or ``switch`` costs one plus its depth, its body one level deeper."""
    loop = ("loop", "for", "", frozenset(), [iff("p", [s("x();")])])
    switch = ("switch", "k", frozenset(), [((1,), [loop])])
    assert cost([loop]) == 1 + 2 and cost([switch], 1) == 2 + 3 + 4
