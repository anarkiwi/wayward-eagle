# Census

`wayward-eagle census FILE.c... [-I DIR] [-j N] [--json]` lists each readability defect of
each C file as a site `(class, function, line, detail)`, and counts sites per class. `--json`
gives, per file, `sites`, `counts` (every class), `complexity` (each function's cognitive
complexity) and `locals` (each function's number of automatic local variables, parameters
and `static` or `extern` locals aside). In Python, `wayward_eagle.census.census(path,
include)` returns a `Report` (`sites`, `complexity`, `locals`, `counts()`);
`census_all(paths, include)` runs files in parallel.

Two engines find sites, both pinned as dependencies so results do not depend on the host:

- **clang-tidy** (`clang-tidy==23.1.4` wheel): one run per file with the checks below,
  each diagnostic mapped to a class, read from the YAML it exports (`--export-fixes`), lines
  from the file offsets. Configuration is passed inline, so no `.clang-tidy` file applies.
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
| `cognitive-complexity` | AST | `wayward_eagle.c.query.cognitive`, SonarSource's rules as clang-tidy's `readability-function-cognitive-complexity` counts them (`tools/cognitive_check.py` compares the two); one site per function above zero, at its name, its score the detail | C7 (a metric, not an issue) |
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
| `status-call` | AST | below | none (idiomatic: a step's exit code tested at once) |
| `uncounted-loop` | AST | below | C7.b |
| `goto` | AST | every `goto` and computed `goto` | C7 |
| `duplicate-code` | AST | below | C7.c |
| `copy-variable` | AST, [dataflow](#dataflow) | below | C5.a.i |
| `edge-copy` | AST, [dataflow](#dataflow) | below | C7 |

Codes used: C2.a extraneous code, C3.a extraneous typecast, C5.a.i extraneous variable
duplicating another, C7 obfuscating control-flow refactoring, C7.b canonical for loop as
while loop, C7.c inline function definition instead of function call.

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
on the other) and gives each standard type the width clang evaluates for it on that target
(`sizeof(T) * __CHAR_BIT__` enumerators of a probe translation unit read from its AST); a site is one on both, matched by where the cast is spelled
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
A condition that is a `status-call` is not a site of this class.

### `status-call`

A condition with an effect that is only a call, under casts, `!` or a comparison with a
constant, of a function defined in the file that returns an integer and whose every
`return` returns a constant (an exit code), where no argument has an effect and no
automatic variable of the caller is addressed (by `&`, or as an array), so the callee
reaches none of the caller's objects. `if (step()) return;` is the site.

The taxonomy codes no such issue: C7 codes control flow refactored to obscure it, and a call
whose result is a status tested once, its effects invisible to the caller's variables, is
how C writes a step that may fail; its only obscurity is that the step is elsewhere, which
is extraction (the inverse of C7.c), not a defect of the condition. Its effect is the call's
reason to exist, unlike a store, comma or increment folded into a condition, which stay
`effect-in-condition`.

### `copy-variable`

An assignment or initialisation `v = u` of an automatic variable `v` (not a parameter)
owned by the function (see [dataflow](#dataflow)), `u` a variable of `v`'s type, qualifiers
aside, or a constant (an expression reading no object) converted to `v`'s integer type,
such that `v` could be replaced by `u` at every use it reaches, and it reaches one: at each
such use no other definition of `v` reaches, the copy is available, and the statement reads
`v` before anything in it changes the copy. One site per copy, its detail `v copies u`.

Available copies is the forward must analysis (intersection where paths meet, nothing on
entry): a copy is generated by its assignment and killed by any store to `v` or `u`, and,
when `u` is not owned, by any statement that may write memory (a call with an effect as
`effect-in-condition` decides it, a store through a pointer, inline assembly). A store
through subscripts of an array object or `.` members writes only that object.

### `edge-copy`

At a join of the control-flow graph (a vertex two or more reachable ones enter: after an
`if` or `switch`, a loop head entered from before the loop and its back edges, a label, a
`case`), two or more distinct copies of one owned variable (assignments of a variable or
constant, as `copy-variable` reads them) reaching it on its incoming edges, each the last
store to the variable on every path through its edge with no read of it since: what a
translation out of SSA leaves for a phi. One site per join and variable, at the statement
making the join, its detail the variable and the lines of the copies. A single assignment
reaching every edge dominates the join and is no site; a loop's step (`v = v + 1`) is no
copy.

The analysis is a forward must problem over copies: a copy generates itself and every store
to or read of its variable kills it.

Copies that are all statements `common-arm-statements` reports (the same statement leading
or ending every arm of an `if`/`switch`) are that class's alone: hoisting or sinking that one
statement leaves a single assignment that dominates the join, so the one defect is one site.
Arms ending in different copies (`v = a` and `v = 2`) are only `edge-copy`.

C7.d (deconstructed ternary) codes the `if`/`else` whose arms each only assign one
variable; an edge copy is the same merge of values spread over the control flow at any join
and with arms doing more, so it takes the category, C7.

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
| C6.c | very large positive decimal integer for a negative integer | a decimal literal that wraps on conversion to a signed type |
| C7.a | while loop as non-canonical for loop | a `for` not in OpenMP canonical form |
| C7.d | deconstructed ternary | an `if`/`else` whose arms each only assign one variable |
| C10.a | bitwise operators replaced with decompiler-specific macro | uses of a known set of such macros |
| C11 | abuse of memory layout | a store through a pointer cast to a type wider than its object |
| C12.a | return value for void function | a function whose every call discards its result |

Not checkable without the original source: C0, C2.b, C3.b, C4, C4.a.ii, C4.a.iii, C4.b,
C4.c, C5.a, C5.b, C6.a, C6.b, C6.d, C8, C9, C12.b, C13, C14.

## Dataflow

`wayward_eagle.c.flow` holds the control flow and dataflow `copy-variable` and `edge-copy`
read, for any analysis of a function body:

| API | Result |
|---|---|
| `build(body, noreturn=...)` | `CFG` of a `FuncDef`'s body: `nodes[i]` the expression, declaration, condition, `return` or computed `goto` vertex `i` evaluates, or None for a join of statement `origin[i]`; `succ`, `pred` (reachable), `entry`, `exit`, `order` (reverse postorder), `reachable`, `joins()`. A top-level comma splits into vertices; a statement calling a function `noreturn` holds of has no successor; a computed `goto` enters every label |
| `solve(cfg, gen, kill, universe=None, forward=True)` | `(into, out)` bit sets per vertex of a worklist bit-vector problem: may (union), or must (intersection, starting from `universe`); forward or backward |
| `Flow(unit, fd, effect=None, copyable=None)` | the function's owned variables `own` (automatic variables and parameters neither array, volatile, nor addressed by `&` or inline assembly), each vertex's `facts` (reads, stores, whether it may write memory), and `defs` (each definition, a parameter's on entry, whether it surely stores, and what a copy copies) |
| `Flow.reaching()`, `Flow.available()`, `Flow.liveness()` | reaching definitions, available copies, live variables per vertex |
| `Flow.copies()`, `Flow.edge_copies()` | copies whose source could replace their variable at every use; distinct copies reaching a join on its edges |

A store under the right operand of `&&`/`||` or an arm of `?:`, or to part of a variable,
may store and kills no definition.

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
locals = "{stem}:locals"
```

| Option | Default | Meaning |
|---|---|---|
| `files` | required | globs of C files, relative to the config's directory |
| `include` | `[]` | header directories |
| `key` | `{stem}:{class}` | `str.format` template over `stem`, `path`, `class`, `function`; values are site counts |
| `total` | `{stem}:cognitive-complexity` | template over `stem` and `path`; the value is the file's total cognitive complexity |
| `locals` | `{stem}:locals` | template over `stem` and `path`; the value is the file's total of automatic local variables (`locals` of `--json`); empty for no key |

Without `{function}` in `key`, every class of every file is a key, at zero when it has no
site, so a class returning to a file is worse than its budget. `cognitive-complexity`
sites are counted only in the total.
