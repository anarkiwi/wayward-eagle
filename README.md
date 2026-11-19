# wayward-eagle

Lowers the cognitive complexity (SonarSource's metric, as clang-tidy's
`readability-function-cognitive-complexity` counts it) of C functions, as pycparser `c_ast`,
keeping what each function does. Each rewrite is made only where it
lowers the cost. `wayward-eagle reduce FILE.c [-o OUT.c] [-I DIR]` reduces every function of
a C file.

```python
from wayward_eagle import c

# fd: a pycparser c_ast.FuncDef, from c.load(...).functions or built by hand
pieces = c.reduce_function(fd, noreturn={"trap"}, status=None)
print(c.render(c.prototypes(pieces) + pieces))  # the static pieces first, fd's own last
```

- [C layer](docs/c.md): `wayward_eagle.c.load` reads C through a pinned clang into `c_ast`
  with clang's types, implicit casts, declarations and source ranges; queries, rendering
  and `reduce_function`.
- [Rewrites](docs/rewrites.md): what each rewrite does and when it applies.
- [Census](docs/census.md): `wayward-eagle census FILE.c... [-I DIR] [--json]` lists C's
  readability defects by class (clang-tidy's checks and checks over clang's AST: dead stores,
  extraneous variables and casts, obscured control flow, meaningless names, raw memory
  blocks and hardware addresses, ...), each function's local variables (and their definitions by why
  they exist), lines and statements, and the `wayward-census`
  virtuous-cycles adapter ratchets them.

Install: `pip install wayward-eagle`. Development: `pip install -e .[dev]`, then
`pytest -n auto --cov=wayward_eagle`.
