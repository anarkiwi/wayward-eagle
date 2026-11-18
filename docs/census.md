# Census

`wayward-eagle census FILE.c... [-I DIR] [-j N] [--json]` lists each readability defect of
each C file as a site `(class, function, line, detail)`, and counts sites per class. `--json`
gives, per file, `sites`, `counts` (every class) and `complexity` (each function's cognitive
complexity). In Python, `wayward_eagle.census.census(path, include)` returns a `Report`
(`sites`, `complexity`, `counts()`); `census_all(paths, include)` runs files in parallel.

Two engines find sites, both pinned as dependencies so results do not depend on the host:

- **clang-tidy** (`clang-tidy==23.1.4` wheel): one run per file with the checks below,
  each diagnostic mapped to a class. Configuration is passed inline, so no `.clang-tidy`
  file applies.
- **AST**: checks written here over clang's AST as the [C layer](c.md) holds it (the
  `ziglang` wheel's clang, its JSON AST as `c_ast`, clang's types, implicit casts and
  declarations in the side table).

Classes are keyed to the codes of Dramko et al., "A Taxonomy of C Decompiler Fidelity
Issues" (USENIX Security 2024), from its codebook ([Zenodo 8419614](https://zenodo.org/records/8419614)):
15 categories (`C0`..`C14`), 52 codes in all. A category without an issue code (`C7`) means
the class is an instance of the category that no issue of it names; `none` means the
codebook does not code it.

## Classes

| Class | Engine | Check | Taxonomy |
|---|---|---|---|
| `cognitive-complexity` | clang-tidy | `readability-function-cognitive-complexity`, threshold 0; one site per function, its score the detail | C7 (a metric, not an issue) |
| `branch-clone` | clang-tidy | `bugprone-branch-clone` | C2.a |
| `redundant-cast` | clang-tidy | `readability-redundant-casting` | C3.a |
| `redundant-expression` | clang-tidy | `misc-redundant-expression` | C2.a |
| `simplifiable-boolean` | clang-tidy | `readability-simplify-boolean-expr` | C2.a |
| `redundant-control-flow` | clang-tidy | `readability-redundant-control-flow` | none (a trailing `return;` is uncoded) |
| `else-after-jump` | clang-tidy | `readability-else-after-return` | C7 |
| `assignment-in-if` | clang-tidy | `bugprone-assignment-in-if-condition` | C7 |
| `inc-dec-in-condition` | clang-tidy | `bugprone-inc-dec-in-conditions` | C7 |
| `nested-conditional` | clang-tidy | `readability-avoid-nested-conditional-operator` | C7 |
| `misleading-indentation` | clang-tidy | `readability-misleading-indentation` | none (layout is uncoded) |
| `redundant-branch-condition` | clang-tidy | `bugprone-redundant-branch-condition` | C2.a |
| `dead-store` | clang-tidy | `clang-analyzer-deadcode.DeadStores` | C2.a |
| `common-arm-statements` | AST | below | C7 |
| `subsumed-cast` | AST | below | C3.a |
| `effect-in-condition` | AST | below | C7 |
| `uncounted-loop` | AST | below | C7.b |
| `goto` | AST | every `goto` and computed `goto` | C7 |
| `duplicate-code` | AST | below | C7.c |

Codes used: C2.a extraneous code, C3.a extraneous typecast, C7 obfuscating control-flow
refactoring, C7.b canonical for loop as while loop, C7.c inline function definition instead
of function call.

### `common-arm-statements`

The same statement, by AST equality (a reference equal by the identity of its declaration,
a literal by its spelling),
leads or ends every arm of an `if`/`else if`/`else` chain ending in `else`, or of a `switch`
with a `default` whose arms each end in a jump (the last may fall off the end). Arms all
alike are `branch-clone`'s. A declaration, label or empty statement is never moved.

- **Leads** (hoistable): the conditions have no effect; the statement stores to no variable
  they read; and when they read more than the function's own variables (parameters and
  automatic variables whose address is never taken), the statement stores only to those
  and calls nothing with an effect.
- **Ends** (sinkable): every arm reaches it; in a `switch`, every arm ends by `break` (which
  the comparison skips) or falls off the end.

### `subsumed-cast`

A cast between integer types (clang's cast kind `IntegralCast`), not to its operand's own
type, whose removal leaves the value of the expression it is in unchanged by C's conversion
rules, decided from the operand type `E`, the cast type `T` and how the cast's value is used,
in the data models of two parses of the file: the host's (`int` 32 bits on a 64-bit host)
and `avr`'s (`int` 16 bits). Each parse resolves typedefs (`int32_t` is `int` on one, `long`
on the other) and gives each standard type its width from that target's predefined width
macros (`__INT_WIDTH__`, ...); a site is one on both, matched by where the cast is spelled
and expands. Conversion to `_Bool` (`IntegralToBoolean`) never wraps. The type a context
converts the value to is the implicit cast clang applies to it:

- **converted** to a type `P` fixed by its use (an outer cast, assignment, initialisation,
  `return`, prototyped argument): `T` holds every value of `E`, or conversion to `T` and to
  `P` both wrap and `T` is at least as wide as `P`. `(int8_t)(uint8_t)(e)` is one: the outer
  cast truncates to 8 bits already.
- **promoted** as an arithmetic, bitwise, comparison or shift operand, or an arm of `?:`:
  `T` holds every value of `E` and `E` and `T` promote to one type.
- **value** read alone (a condition, an operand of `!`, `&&` or `||`, an array index): `T`
  holds every value of `E`.

### `effect-in-condition`

An assignment, increment or decrement, comma operator, or call that may have an effect in
the condition of an `if`, `while`, `do`/`while` or `for`. A call has none when its callee
is declared `pure` or `const`, or is defined in the file and neither stores outside its own
automatic variables, accesses a volatile object, runs inline assembly, calls through a
pointer, nor calls a function with an effect (a fixed point over the file's call graph).

### `uncounted-loop`

A loop and an automatic integer variable `v` whose address is never taken, where:

- the last statement before the loop in its block that mentions `v` sets it to a constant
  (no label lies between), or the loop is a `for` whose init does;
- `v` is stored to once in the loop, by adding or subtracting a constant (`++`, `--`, `+=`,
  `-=`, `v = v ± k`), and that store runs once per iteration: it is the `for`'s step, a
  top-level statement of the body with no `continue` before it, or in the condition under
  only `&&` and commas;
- `v` is read by the loop's condition or by the condition of a top-level `if (...) break;`;
- the loop is not a `for` with that init, a condition testing `v`, and that step.

Loops holding a `goto` or label are skipped, since a jump may bypass the step.

### `duplicate-code`

A maximal sequence of at least two consecutive statements of one block that recurs,
statement for statement, elsewhere in the same function: AST equality with references equal
by name and scope, so blocks declaring and using their own variables alike match, and
literals equal by their spelling wherever it is (a header's macro included). Statements are
clang's: one declaration statement, or a chain of `case` labels with its first statement, is
one. One site
per distinct sequence, its detail its length and the lines of every copy.

## Taxonomy codes not covered

Checkable on C alone, and not yet classes:

| Code | Issue | What a check would find |
|---|---|---|
| C1.b.i, C1.b.ii | pointer dereference or arithmetic to access an array member | `*(p + i)` and `*p` over an array object instead of `p[i]` |
| C1.a.i | pointer arithmetic to access a struct member | a dereference of a cast of pointer arithmetic, `*(T *)(p + k)` |
| C4.a.i | extra arguments | parameters no path reads (`misc-unused-parameters`) |
| C5.a.i | extraneous variable duplicating another | a copy `v = a` where neither changes while `v` is live |
| C6.c | very large positive decimal integer for a negative integer | a decimal literal that wraps on conversion to a signed type |
| C7.a | while loop as non-canonical for loop | a `for` not in OpenMP canonical form |
| C7.d | deconstructed ternary | an `if`/`else` whose arms each only assign one variable |
| C10.a | bitwise operators replaced with decompiler-specific macro | uses of a known set of such macros |
| C11 | abuse of memory layout | a store through a pointer cast to a type wider than its object |
| C12.a | return value for void function | a function whose every call discards its result |

Not checkable without the original source: C0, C2.b, C3.b, C4, C4.a.ii, C4.a.iii, C4.b,
C4.c, C5.a, C5.b, C6.a, C6.b, C6.d, C8, C9, C12.b, C13, C14.

## Ratchet

The `wayward-census` adapter of
[virtuous-cycles](https://github.com/anarkiwi/virtuous-cycles) (installed separately:
`pip install git+https://github.com/anarkiwi/virtuous-cycles`) holds site counts to budgets
that only tighten:

```toml
[[metric]]
name = "census"
adapter = "wayward-census"
budget = "budgets/census.json"
[metric.options]
files = ["out/census/*.c"]
include = ["out/census", "c64"]
key = "{stem}:{class}"
total = "{stem}:cognitive-complexity"
```

| Option | Default | Meaning |
|---|---|---|
| `files` | required | globs of C files, relative to the config's directory |
| `include` | `[]` | header directories |
| `key` | `{stem}:{class}` | `str.format` template over `stem`, `path`, `class`, `function`; values are site counts |
| `total` | `{stem}:cognitive-complexity` | template over `stem` and `path`; the value is the file's total cognitive complexity |

Without `{function}` in `key`, every class of every file is a key, at zero when it has no
site, so a class returning to a file is worse than its budget. `cognitive-complexity`
sites are counted only in the total.
