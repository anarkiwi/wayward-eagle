"""Forward gotos by their reaching condition, and labelled tails cut again: what goes, what
stays, and the reduced functions behaving as the originals."""

import pytest

from conftest import behaves
from wayward_eagle.c import joins
from wayward_eagle.c.recut import measures, recut
from wayward_eagle.c import reduce as r

LOCALS = {"v1", "v2", "v3", "v4"}
SIG = "void f(uint8_t *R)"


def joined(cc, body):
    """``body`` with its labels joined (:func:`~wayward_eagle.c.joins.rejoined`), one line."""
    return cc.text(joins.rejoined(cc.items(body, SIG), LOCALS))


@pytest.mark.parametrize(
    "body, want",
    [
        (
            "if (v1 == 0) { v4 = v2; } else { if (v3) goto L1; v4 = v3; }"
            " R[0] = 0; R[1] = v4; L1:; x();",
            "if (v1 == 0) { v4 = v2; } else if (!v3) { v4 = v3; }"
            " if (v1 == 0 || !v3) { R[0] = 0; R[1] = v4; } x();",
        ),
        (
            "if (v4 != 0) { if (v4 == 0x3F) goto L4; if (v4 < 0x3F) goto L6; }"
            " v4 = R[0x4C]; L6:; w(v4); L4:; y();",
            "if (v4 == 0 || v4 != 0x3F) { if (v4 == 0 || v4 >= 0x3F) { v4 = R[0x4C]; }"
            " w(v4); } y();",
        ),
    ],
)
def test_gotos_go_by_their_reaching_condition(cc, body, want):
    """A label every goto to which ends a path through the ``if`` before it goes, the
    statements between guarded by the negated path condition."""
    assert joined(cc, body) == cc.text(cc.items(want, SIG))


@pytest.mark.parametrize(
    "body",
    [
        # The tested variable stored after its test.
        "if (v1) { v1 = 0; goto L; } y(); L:; z();",
        # A test reading memory a call after it may change.
        "if (R[0]) { x(); if (v2) goto L; } y(); L:; z();",
        # A test with an effect.
        "if (g()) goto L; y(); L:; z();",
        # Gotos to the label from two statements.
        "if (v1) goto L; x(); if (v2) goto L; y(); L:; z();",
        # A goto not last on its path.
        "if (v1) { if (v2) { goto L; } x(); } y(); L:; z();",
    ],
)
def test_gotos_whose_condition_does_not_hold_stay(cc, body):
    """A goto whose path condition could not be tested again after the ``if`` stays."""
    assert "goto" in joined(cc, body)


def test_held_paths(cc):
    """Paths through an ``if`` ending in the goto, each condition unchanged after it."""
    # pylint: disable=protected-access
    items = cc.items("if (v1) { if (v2) goto L; } else { goto L; } L:;")
    jn = joins._Joiner(LOCALS, set(), {"L": 2})
    assert len(jn._paths(joins._tailed(items[0], "L"), "L")) == 2
    held = cc.items("if (v1) { v1 = 0; goto L; } L:;")
    with pytest.raises(joins._Held):
        jn._paths(held[0], "L")


@pytest.mark.parametrize(
    "src",
    [
        "void f(void) { int a; if (c0) { a = p; } else { if (q) goto L1; a = k; }"
        " w(a); x(); L1:; y(); }",
        "void f(void) { int a = p, b = q; if (a) { if (b) goto L; x(); } else { goto L; }"
        " y(); L:; z(); }",
        "void f(void) { if (p) { if (q) { x(); goto L; } g(); } y(); L:; z(); }",
    ],
)
def test_joined_functions_behave(tmp_path, src):
    """Reduced functions whose labels join print what the originals do."""
    behaves(tmp_path, src)


def test_tails_cut_again(cc):
    """A function returning nothing with top-level labels is cut again where that leaves
    fewer gotos and a lower cognitive complexity; one with none stays."""
    # pylint: disable=protected-access
    fd = cc.fn(
        "if (p) { if (q) { x(); goto L4; } y(); goto L3; } if (k) goto L4; z(); return;"
        " L3:; if (c0) { y(); } else { z(); } L4:; if (c1) { x(); } else { w(p); }"
    )

    def again(f, names):
        return r._reduce(f, names, (frozenset(), None, None, False))

    got = recut([fd], again, None, set())
    assert len(got) > 1 and got[-1].decl.name == "f"
    assert measures(got, None) < measures([fd], None)
    plain = cc.fn("x(); y();")
    assert recut([plain], again, None, set()) == [plain]
