"""Shared helpers: C function bodies parsed by clang in the reducer's statement-list form,
and C programs built with the pinned zig and run, to compare what they print."""

import subprocess

import pytest

from wayward_eagle.c import front, printer as render, stmt
from wayward_eagle.c import reduce as r

HEAD = (
    "typedef unsigned char uint8_t; typedef signed char int8_t;\n"
    "typedef unsigned short uint16_t; typedef short int16_t;\n"
    "void x(void); void y(void); void z(void); void w(int); int g(void);\n"
    "__attribute__((noreturn)) void trap(int);\n"
    "int p, q, k, c0, c1, c2, c3, c4, c5, c6;\n"
)
LOCALS = "int v1, v2, v3, v4, v5, v6;\n"


class C:
    """C function bodies parsed by clang, in the reducer's statement-list form."""

    def __init__(self, tmp_path):
        self.dir, self.n = tmp_path, 0

    def unit(self, src):
        """The unit of C ``src`` after the common declarations."""
        self.n += 1
        path = self.dir / f"t{self.n}.c"
        path.write_text(HEAD + src + "\n", encoding="utf-8")
        return front.load(path)

    def fn(self, body, sig="void f(void)"):
        """The ``FuncDef`` of ``sig`` with ``body``."""
        return self.unit(f"{sig} {{ {body} }}").functions[-1]

    def items(self, body, sig="void f(void)"):
        """The statement list of ``body``, its locals hoisted."""
        return r._normal(  # pylint: disable=protected-access
            stmt.block(self.fn(LOCALS + body, sig).body), {}
        )

    def text(self, items):
        """``items`` as one line of C."""
        return " ".join(render.text(stmt.braced(list(items))).split())

    def same(self, items, body):
        """Whether ``items`` are the statements of C ``body``."""
        return self.text(items) == self.text(self.items(body))


@pytest.fixture(name="cc")
def fixture_cc(tmp_path):
    """A :class:`C` writing under ``tmp_path``."""
    return C(tmp_path)


def deep(n, body):
    """``n`` nested ``if`` statements around C ``body``."""
    for k in range(n):
        body = f"if (c{k}) {{ {body} }}"
    return body


LOCAL = {"v1", "v2", "v3", "v4", "v5", "v6"}

#: Definitions of :data:`HEAD`'s functions and a ``main`` running ``f`` over the globals'
#: values, printing every call and the globals after each run (a ``trap`` ends a run).
DRIVER = """
#include <setjmp.h>
#include <stdio.h>
static jmp_buf env;
void x(void) { printf("x "); }
void y(void) { printf("y "); }
void z(void) { printf("z "); }
void w(int a) { printf("w%d ", a); }
int g(void) { return k++ & 3; }
void trap(int a) { printf("trap%d ", a); longjmp(env, 1); }
int main(void) {
  for (int i = 0; i < 64; i++) {
    p = i & 3; q = (i >> 2) & 3; k = i >> 4;
    c0 = i & 1; c1 = (i >> 1) & 1; c2 = (i >> 2) & 1; c3 = (i >> 3) & 1;
    c4 = (i >> 4) & 1; c5 = (i >> 5) & 1; c6 = i % 3 == 0;
    if (!setjmp(env)) {
      RUN;
    }
    printf("| %d %d %d\\n", p, q, k);
  }
  return 0;
}
"""


def run(path, src):
    """What C program ``src``, written to ``path`` and built for the host with zig's musl,
    prints."""
    path.write_text(src, encoding="utf-8")
    exe = path.with_suffix("")
    cmd = [str(front.ZIG / "zig"), "cc", "-target", "native-linux-musl", "-w", "-o", str(exe)]
    subprocess.run([*cmd, str(path)], check=True, capture_output=True)
    return subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout


def behaves(tmp_path, src, call="f()"):
    """The C of function ``f`` of C ``src`` (after :data:`HEAD`) reduced, asserting that a
    program running it over :data:`DRIVER`'s inputs prints what one running ``src`` does."""
    path = tmp_path / "orig.c"
    text = HEAD + src + "\n" + DRIVER.replace("RUN", call)
    path.write_text(text, encoding="utf-8")
    got = r.reduce_unit(front.load(path), functions={"f"})
    assert run(tmp_path / "reduced.c", got) == run(path, text)
    return got
