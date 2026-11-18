# wayward-eagle

Lowers the cognitive complexity (SonarSource's metric, as clang-tidy's
`readability-function-cognitive-complexity` counts it) of C function bodies held as statement
trees, keeping what each function does. Each rewrite is made only where it lowers the cost.

```python
from wayward_eagle import Function, reduced

pieces, sections = reduced(body, "f", Function(params=["a"], local={"v1", "v2"}, void=True))
for name, args, items in pieces:
    ...  # print each function: its name, parameters and statements
```

- [Statement trees](docs/trees.md): the input and output.
- [Rewrites](docs/rewrites.md): what each rewrite does and when it applies.

Install: `pip install wayward-eagle`. Development: `pip install -e .[dev]`, then
`pytest -n auto --cov=wayward_eagle`.
