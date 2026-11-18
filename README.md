# wayward-eagle

Lowers the cognitive complexity (SonarSource's metric, as clang-tidy's
`readability-function-cognitive-complexity` counts it) of C functions, as pycparser `c_ast`
or as statement trees, keeping what each function does. Each rewrite is made only where it
lowers the cost. `wayward-eagle reduce FILE.c [-o OUT.c] [-I DIR]` reduces every function of
a C file.

```python
from wayward_eagle import Function, reduced

pieces, sections, temps = reduced(body, "f", Function(params=["a"], local={"v1", "v2"}, void=True))
for piece in pieces:
    ...  # print each function: piece.name, piece.args, piece.items, piece.ret
```

- [C layer](docs/c.md): `wayward_eagle.c.load` reads C through a pinned clang into `c_ast`
  with clang's types, implicit casts, declarations and source ranges; queries, rendering
  and `reduce_function`.
- [Statement trees](docs/trees.md): the tuple input and output of `reduced`.
- [Rewrites](docs/rewrites.md): what each rewrite does and when it applies.
- [Census](docs/census.md): `wayward-eagle census FILE.c... [-I DIR] [--json]` lists C's
  readability defects by class (clang-tidy's checks and checks over clang's AST, keyed to a
  decompiler-fidelity taxonomy) and each function's local variables, and the `wayward-census`
  virtuous-cycles adapter ratchets them.

Install: `pip install wayward-eagle`. Development: `pip install -e .[dev]`, then
`pytest -n auto --cov=wayward_eagle`.
