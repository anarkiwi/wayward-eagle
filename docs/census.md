# Census

`wayward-eagle census FILE.c... [-I DIR] [-j N] [--json]` lists each readability defect of
each C file as a site `(class, function, line, detail)`, and counts sites per class. `--json`
gives, per file, `sites`, `counts` (every class), `complexity` (each function's cognitive
complexity), `locals` (each function's number of automatic local variables, parameters
and `static` or `extern` locals aside), `lines` and `statements` (each function's, by
[size](#size)), `names` (`opaque` and `declared`: the distinct identifiers the file names,
by [`opaque-name`](#opaque-name)) and `reasons` (each function's definitions of automatic
variables by [reason](#locals-by-reason)). In Python, `wayward_eagle.census.census(path, include)`
returns a `Report` (`sites`, `complexity`, `locals`, `lines`, `statements`, `names`,
`reasons`, `counts()`);
`census_all(paths, include)` runs files in parallel.

Two engines find sites, both pinned as dependencies so results do not depend on the host:

- **clang-tidy** (`clang-tidy==23.1.4` wheel): one run per file with the checks below,
  each diagnostic mapped to a class, read from the YAML it exports (`--export-fixes`), lines
  from the file offsets. Configuration is passed inline, so no `.clang-tidy` file applies.
- **AST**: checks written here over clang's AST as the [C layer](c.md) holds it (the
  `ziglang` wheel's clang, its JSON AST as `c_ast`, clang's types, implicit casts and
  declarations in the side table).

Each class names the readability issue it finds, as Dramko et al., "A Taxonomy of C
Decompiler Fidelity Issues" (USENIX Security 2024) describe it; the Ref. column cites its
code in their codebook ([Zenodo 8419614](https://zenodo.org/records/8419614)), `none` where
the codebook has no such issue.

## Classes

| Class | Engine | Check | Issue | Ref. |
|---|---|---|---|---|
| `cognitive-complexity` | AST | `wayward_eagle.c.query.cognitive`, SonarSource's rules as clang-tidy's `readability-function-cognitive-complexity` counts them (`tools/cognitive_check.py` compares the two); one site per function above zero, at its name, its score the detail | control flow too complex to follow (a metric, not an issue) | C7 |
| `branch-clone` | clang-tidy | `bugprone-branch-clone` | extraneous code: identical branches | C2.a |
| `redundant-cast` | clang-tidy | `readability-redundant-casting` | extraneous typecast | C3.a |
| `redundant-expression` | clang-tidy | `misc-redundant-expression` | extraneous code: an expression with a known value | C2.a |
| `simplifiable-boolean` | clang-tidy | `readability-simplify-boolean-expr` | extraneous code: a boolean expression longer than it needs | C2.a |
| `redundant-control-flow` | clang-tidy | `readability-redundant-control-flow` | a jump to where control goes anyway | none |
| `else-after-jump` | clang-tidy | `readability-else-after-return` | obscured control flow: an `else` after a jump | C7 |
| `assignment-in-if` | clang-tidy | `bugprone-assignment-in-if-condition` | obscured control flow: an assignment in a condition | C7 |
| `inc-dec-in-condition` | clang-tidy | `bugprone-inc-dec-in-conditions` | obscured control flow: an increment in a condition | C7 |
| `nested-conditional` | clang-tidy | `readability-avoid-nested-conditional-operator` | obscured control flow: nested `?:` | C7 |
| `misleading-indentation` | clang-tidy | `readability-misleading-indentation` | misleading layout | none |
| `redundant-branch-condition` | clang-tidy | `bugprone-redundant-branch-condition` | extraneous code: a condition already known | C2.a |
| `dead-store` | clang-tidy | `clang-analyzer-deadcode.DeadStores` | dead store | C2.a |
| `common-arm-statements` | AST | below | obscured control flow: a statement repeated in every arm | C7 |
| `subsumed-cast` | AST | below | extraneous typecast | C3.a |
| `effect-in-condition` | AST | below | obscured control flow: an effect in a condition | C7 |
| `status-call` | AST | below | none: a step's exit code tested at once is idiomatic | none |
| `uncounted-loop` | AST | below | counted loop written as a `while` loop | C7.b |
| `goto` | AST | every `goto` and computed `goto` | obscured control flow: a jump | C7 |
| `duplicate-code` | AST | below | inlined function: code repeated instead of called | C7.c |
| `copy-variable` | AST, [dataflow](#dataflow) | below | extraneous variable duplicating another | C5.a.i |
| `edge-copy` | AST, [dataflow](#dataflow) | below | obscured control flow: one value merged by copies on each edge | C7 |
| `label-after-jump` | AST, [dataflow](#dataflow) | below | obscured control flow: a label reached only by jumps | C7 |
| `opaque-name` | AST | below | meaningless name | C0.a, C0.c |
| `raw-memory` | AST | below | raw memory block standing in for variables | C8 |
| `raw-hardware` | AST | below | raw hardware address instead of a named symbol | C9 |


The checks of `opaque-name`, `raw-memory`, `raw-hardware`, `label-after-jump` and
[size](#size) are `wayward_eagle.legible`.

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
A condition that is a `status-call` is not a site of this class, nor one whose only effect
is one call of a reader (`io_read` by default; `--reader NAME`, the adapter's `readers`),
its arguments without effects: `while (io_read(0xD012) != 0xFC)` polls a device register,
reading it as it tests it.

### `status-call`

A condition with an effect that is only a call, under casts, `!` or a comparison with a
constant, of a function defined in the file that returns an integer and whose every
`return` returns a constant (an exit code), where no argument has an effect and no
automatic variable of the caller is addressed (by `&`, or as an array), so the callee
reaches none of the caller's objects. `if (step()) return;` is the site.

It is no issue: a call whose result is a status tested once, its effects invisible to the
caller's variables, is how C writes a step that may fail, not control flow refactored to
obscure it; its only obscurity is that the step is elsewhere, which is extraction (the
inverse of an inlined function), not a defect of the condition. Its effect is the call's
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

A deconstructed ternary is the `if`/`else` whose arms each only assign one variable; an edge
copy is the same merge of values spread over the control flow at any join and with arms
doing more, so it is obscured control flow in general.

### `label-after-jump`

A label whose join in the function's control-flow graph (`build(body, noreturn,
gotos=True)`, each named `goto` a vertex of its own) is reachable and entered only from
`goto` vertices: whatever precedes it returns, jumps, breaks, continues or calls a
`noreturn` function on every path, so its code is reached only through the label. One site
per label, its detail the label. A label nothing jumps to and nothing falls into is
unreachable and no site.

### `opaque-name`

An identifier no word of whose name carries meaning. The identifiers are those the file
names outside a macro's body (or in a macro's argument): variables, parameters, functions
and enumerators it declares or references, labels, and the object-like macros it expands.
A name splits into words at underscores, between letters and digits, and at camel case
(before an upper-case letter following a lower-case one, and before the last of a run of
upper-case letters followed by a lower-case one): `f12_1` is `f`, `12`, `1`; `B0c2` is
`B`, `0`, `c`, `2`; `readHWReg` is `read`, `HW`, `Reg`. A word carries meaning when it has
two letters or more and is not made only of hexadecimal digits; a single letter tags a
kind, and a number or hex tag (`AF`, `1FF`) an address. A name is opaque when no word of it
carries meaning and one is a number or hex tag: its meaning is only a number. So `v13`,
`S_108`, `K_AF`, `R1`, `c48`, `f12_1` and `x3` are opaque, and `g_freq_lo_2`, `freq_hi`,
`voice` and `ptr` are not: a number disambiguating a meaningful name is fine. A name of
single letters alone (`i`, `p`, `x_y`) is C's convention for an index or parameter, not a
number standing in for a name.

No word is treated specially: a generator's prefix is a word like any other, so
`cgen_play_x1` (`play`) and `CGEN_OBJ0_ORG` (`OBJ`, `ORG`) are not opaque. A word of hex
letters only that is meant as a word (`ADC` in `S_D5_ADC`) counts as a tag: spelling alone
cannot tell the two apart.

One site per distinct opaque name, where the file first names it, its detail the name;
`names` gives their number and the number of distinct names.

### `raw-memory`

Every subscript of a file-scope array that is a memory block: one array object standing in
for many variables. Each subscript's address, the array's and the subscript's terms added
through `+`, `-`, negation and casts to integer types (so a macro `#define R2 (C0 + 8)`
makes `R2[i]` a subscript of `C0` at offset 8), is the array, a constant offset, and an
index (the other terms, by AST equality, up to order). Subscripts holding an index touch
one object when they are joined by:

- a shared offset: one base, indexed two ways (`m[0x10 + i]`, `m[0x10 + j]`);
- one index with offsets one apart: neighbouring elements (`m[i]`, `m[i + 1]`).

An array whose indexed subscripts touch more than one object is a block (`m[0x10 + i]` and
`m[0x20 + i]` are two variables); each of its subscripts, constant ones included, is a site,
its detail the array and the offset. Constant subscripts decide nothing, since a real array's
elements may each be read by a constant (`t[0]`, `t[1]`), so an array read only by
constants is no block. A real array read at `a[i]` and `a[n - 1]`, with neither `a[i - 1]`
nor `a[n]`, is read as two objects.

Grouping offsets by index and joining groups that share any offset does not tell a block
apart: a block's first variable is commonly indexed by several indices at offset 0, which
joins every group through it.

### `raw-hardware`

An integer used as an address whose constant is a nonzero sum holding a literal spelled in
the file (not in a macro's body): converted to a pointer (`*(volatile uint8_t *)0xD020`),
or passed to an address parameter (`io_write(0xD401 + v, x)`), detail the callee (`*` for a
conversion), the constant, and whether an index is added. A parameter is an address when its
function converts it to a pointer, or passes it to an address parameter, whole (under casts
to integer types): a least fixed point over the file's functions, so wrappers of an accessor
defined in the file are found. A parameter added to a constant is an index, not an address,
so a call passing a register's offset (`voice(7)` where `voice` writes `0xD404 + o`) is no
site; the address inside is. A constant a macro names (`out(SID, 0)`) is named, not raw.

### Size

Each function's `lines` (from its first line to its last) and `statements`, as clang counts
them: an expression, `return`, jump, `if`, loop or `switch` is one, a declaration statement
one however many variables it declares, and blocks, labels and empty statements none.

### Locals by reason

Each definition (an initialisation or assignment, `++`, `--` or compound assignment) of an
automatic variable a function owns (see [dataflow](#dataflow); parameters aside) is counted
under the first reason of this order that holds of it, so a definition both used twice and
clobbered is `clobbered`:

| Reason | Holds when | Issue | Ref. |
|---|---|---|---|
| `unused` | no use of the variable reads it (its value is overwritten or dies) | dead store | C2.a |
| `check-only` | every use is in the condition of an `if` a branch of which surely ends in a call that never returns (`if ((uint16_t)(v - ORG) > LIM) trap(0xA);`): every path from the branch reaches a vertex without successors | extraneous code: it feeds only a check the translation added | C2.a |
| `clobbered` | on some path from it to a use, something may change what its value reads: an assignment to a variable it reads (its own, for `v = v + 1`), or a store or call that may write memory it reads; or, for a value with effects of its own, something reads or writes what it writes | none: the variable holds a value the program no longer has | none |
| `merged` | another definition of the variable reaches one of its uses too (a join of assignments on several paths, or a store to part of it) | none: assignment on several paths is how C merges values (`edge-copy` reports the extraneous ones) | none |
| `multi-use` | its value is read more than once (counting each occurrence in a statement) | none: a value named for its several uses | none |
| `single-use` | its value is read once, and printing its defining expression there reads the same values | extraneous variable: the expression could stand at its use | C5.a |

`single-use` is the removable class: a temporary the expression it holds could replace.
`multi-use` definitions could be recomputed at each use too (nothing between changes what
they read), at the cost of repeating the expression.

Clobbering is a forward may problem over definitions, along the reaching definitions: a
definition is generated where something conflicts with its value (at its own vertex when its
value reads its variable), and killed where its variable is surely stored. At the use, the
statement's evaluations other than the use's ancestors (which act after reading it, such as
the store of `V[i] = v` or the call of `io_write(a, v)`) conflict as well.

Two evaluations conflict when one writes a location the other reads or writes. Locations are
owned variables, other variables, elements of an array variable with a constant subscript
(`R1[0x122]` and `R1[0x131]` apart; a variable subscript is the whole array), memory
through a pointer (overlapping every variable not owned) and the device: a volatile access
both reads and writes the device and no memory, so a volatile store through an address
(`*(volatile uint8_t *)a = v`) changes no program variable. An object declared `const` is
no location. Operands of `sizeof` are not evaluated.

What a call reads and writes is its callee's definition's, when the file defines it: the
locations its body touches beyond its own automatic variables, and those of the calls it
makes, a least fixed point over the file's calls. So an accessor defined as a volatile
store (`void io_write(uint16_t a, uint8_t v) { *(volatile uint8_t *)a = v; }`) touches the
device only, and a value read from a variable before it is used after it. Otherwise a
function declared `const` touches nothing, one declared `pure` reads memory, and one that
never returns (`trap`) touches the device only: no code after it runs, and stopping is
ordered with device accesses. Any other call, and a call through a pointer, may read and
write everything. Where the other classes ask whether a call has an effect, it has one when
it may write any location.

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
one. A call and a `return;` alone is no sequence: it calls code its callers share
instead of copying it. One site per distinct sequence, its detail its length and the lines of
every copy.

## Issues not covered

Checkable on C alone, and not yet classes:

| Issue | What a check would find | Ref. |
|---|---|---|
| pointer dereference or arithmetic to access an array member | `*(p + i)` and `*p` over an array object instead of `p[i]` | C1.b.i, C1.b.ii |
| pointer arithmetic to access a struct member | a dereference of a cast of pointer arithmetic, `*(T *)(p + k)` | C1.a.i |
| extra arguments | parameters no path reads (`misc-unused-parameters`) | C4.a.i |
| very large positive decimal integer for a negative integer | a decimal literal that wraps on conversion to a signed type | C6.c |
| while loop as non-canonical for loop | a `for` not in OpenMP canonical form | C7.a |
| deconstructed ternary | an `if`/`else` whose arms each only assign one variable | C7.d |
| bitwise operators replaced with decompiler-specific macros | uses of a known set of such macros | C10.a |
| abuse of memory layout | a store through a pointer cast to a type wider than its object | C11 |
| return value for a void function | a function whose every call discards its result | C12.a |

The codebook's other issues need the original source to tell: names that are wrong rather
than meaningless, missing code, typecasts and variables, wrong arguments, extraneous
variables beyond single-use temporaries, literals in the wrong representation, and global
variables and expanded symbols beyond memory blocks and raw addresses.

## Dataflow

`wayward_eagle.c.flow` holds the control flow and dataflow `copy-variable` and `edge-copy`
read, for any analysis of a function body:

| API | Result |
|---|---|
| `build(body, noreturn=..., gotos=False)` | `CFG` of a `FuncDef`'s body: `nodes[i]` the expression, declaration, condition, `return` or computed `goto` vertex `i` evaluates, or None for a join of statement `origin[i]`; `succ`, `pred` (reachable), `entry`, `exit`, `order` (reverse postorder), `reachable`, `joins()`. A top-level comma splits into vertices; a statement calling a function `noreturn` holds of has no successor; a computed `goto` enters every label; with `gotos`, each named `goto` is a vertex entering its label |
| `solve(cfg, gen, kill, universe=None, forward=True)` | `(into, out)` bit sets per vertex of a worklist bit-vector problem: may (union), or must (intersection, starting from `universe`); forward or backward |
| `footprint(unit, n, calls, skip=())` | `(reads, writes)`: the locations evaluating `n` may read and write (symbol ids, `(id, index)` elements, `MEMORY`, `DEVICE`), `calls(call)` a call's |
| `Flow(unit, fd, effect=None, copyable=None, calls=None)` | the function's owned variables `own` (automatic variables and parameters neither array, volatile, nor addressed by `&` or inline assembly), each vertex's `facts` (reads, stores, whether it may write memory), and `defs` (each definition, a parameter's on entry, whether it surely stores, and what a copy copies); `calls(call)` the locations a call reads and writes (default everything) |
| `Flow.reaching()`, `Flow.available()`, `Flow.liveness()` | reaching definitions, available copies, live variables per vertex |
| `Flow.copies()`, `Flow.edge_copies()` | copies whose source could replace their variable at every use; distinct copies reaching a join on its edges |
| `Flow.temporaries()` | `(def, reason)` of each definition but a parameter's or a bare declaration, by [reason](#locals-by-reason) |

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
reasons = "{stem}:locals:{reason}"
lines = "{stem}:lines"
statements = "{stem}:statements"
```

| Option | Default | Meaning |
|---|---|---|
| `files` | required | globs of C files, relative to the config's directory |
| `include` | `[]` | header directories |
| `key` | `{stem}:{class}` | `str.format` template over `stem`, `path`, `class`, `function`; values are site counts |
| `total` | `{stem}:cognitive-complexity` | template over `stem` and `path`; the value is the file's total cognitive complexity |
| `locals` | `{stem}:locals` | template over `stem` and `path`; the value is the file's total of automatic local variables (`locals` of `--json`); empty for no key |
| `reasons` | `{stem}:locals:{reason}` | template over `stem`, `path` and `reason`; the value is the file's definitions of automatic variables of that [reason](#locals-by-reason), every reason a key; empty for none |
| `lines`, `statements` | `{stem}:lines`, `{stem}:statements` | templates over `stem` and `path`; the value is the most lines, or statements, of a function of the file; empty for no key |
| `combine` | `{}` | `{name = {component = weight, ...}}`; each component a class or a per-file measure (`lines`, `statements`, `locals`, `locals:<reason>`); each name is a key of `key` with `class` the name (and `function` empty for measures), its value the weighted sum of those classes' site counts and the measures' per-file values, which have no keys of their own |

Without `{function}` in `key`, every class of every file is a key, at zero when it has no
site, so a class returning to a file is worse than its budget. `cognitive-complexity`
sites are counted only in the total.
