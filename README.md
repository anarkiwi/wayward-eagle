# wayward-eagle

Lowers the cognitive complexity (SonarSource's metric, as clang-tidy's
`readability-function-cognitive-complexity` counts it) of C function bodies held as statement
trees, keeping what each function does. Each rewrite is made only where it lowers the cost.

```python
from wayward_eagle import Function, reduced

pieces, sections, temps = reduced(body, "f", Function(params=["a"], local={"v1", "v2"}, void=True))
for piece in pieces:
    ...  # print each function: piece.name, piece.args, piece.items, piece.ret
```

- [Statement trees](docs/trees.md): the input and output.
- [Rewrites](docs/rewrites.md): what each rewrite does and when it applies.
- [Census](docs/census.md): `wayward-eagle census FILE.c... [-I DIR] [--json]` lists C's
  readability defects by class (clang-tidy's checks and checks over clang's AST, keyed to a
  decompiler-fidelity taxonomy), and the `wayward-census` virtuous-cycles adapter ratchets them.

Install: `pip install wayward-eagle`. Development: `pip install -e .[dev]`, then
`pytest -n auto --cov=wayward_eagle`.
