# Tests

**Status of this directory: documentation only — the suite is not vendored in this repository.**

The 382 tests that back the footer claim *"382 tests passing (Session 14D)"* live in the project's
canonical skills tree, next to the modules they exercise:

```
D:/OpenCode/skills/tests/
  test_renderer.py          renderer + template behaviour
  test_sir_extractor.py     extraction behaviour
  test_pipeline.py          extract → render orchestration
  test_session14a1.py       Gate-2 scanner wiring + Priority-0 classes (67 tests)
  test_session14c.py        figure panels, `\texttt` class, escaper (14 tests)
  test_session14c_bib.py    bibliography numbering (5 tests)
  validate_pdf tests        the Gate-2 detector table (23 tests)
```

`vendor/skills/` in this repository holds the modules (`sir_extractor.py`, `renderer.py`,
`render_templates/`, `validate_pdf.py`) but not their tests. Two files import the canonical tree
directly — `test_renderer.py` imports `D:/OpenCode/skills/renderer.py`, while `test_pipeline.py`
resolves skills through `app/pipeline.py` to `vendor/skills/` — which is why the mirror rule in
[`CONTRIBUTING.md`](../CONTRIBUTING.md) exists: a one-sided edit gives you a green suite over code that
is not shipping.

## Running the suite

From the canonical tree:

```bash
cd D:/OpenCode/skills
python -m pytest tests/ -q --basetemp=tmp/pytest_local
```

Expected at commit `8fab304`: **382 passed**.

### The Windows quirk — why `--basetemp` is not optional

Without `--basetemp`, this host reports:

```
382 passed, 1 warning in ...
Exit code: 1
```

The tests pass; **pytest's session-finish cleanup fails**, because the default temp root
`%TEMP%\pytest-of-Hp` contains a stale junction that raises
`PermissionError: [WinError 5]` on `pytest-current`. Pointing `--basetemp` at a fresh workspace path
sidesteps it.

This is not cosmetic. A CI job or a viva demo that reads the exit code will report a green suite as a
failure — and an earlier session recorded exactly this as "71 passed but exits 1 … a Windows temp-dir
artefact, not a test failure". Two consequences:

1. Always pass `--basetemp` on this platform, and keep the path inside the workspace.
2. Before concluding the suite is broken from a non-zero exit, check whether the summary line says
   `failed` or `passed`.

## TeX-gated tests

Some tests invoke `pdflatex`, because the claim they check cannot be established by string-matching the
emitted `.tex`. Two examples of that class, both added during Session 14C:

- that **both panels** of a two-panel figure reach the typeset page, and
- that the bibliography prints `[1] [2] [3]` rather than `[1] [2] [2]`.

Those tests are **gated on a TeX binary being present**, so the suite still *collects* on a machine
without one. On a host with no TeX engine you therefore get 382 passing with some of them skipped
internally — which is why the development host's own verification at Session 14D was render-level
(`SIR → IEEE .tex`, asserted on the emitted document) with the compile and scanner runs deferred to the
deployed container.

## Adding a test

1. Put it beside the module it exercises in the canonical tree.
2. If the assertion is about the reader's page, compile and inspect the PDF; if it is about the emitted
   document, say so in the test name. Do not let a `.tex` string check stand in for a page check — that
   substitution is the whole content of the project's methodology finding.
3. Do not loosen an existing assertion to make a change pass. Session 14D had to update two Session
   14C tests because they encoded the exact construct its D2 fix changes, and it recorded *why*: the
   content invariant both charts survive was preserved while the merge behaviour those tests had
   pinned was superseded.
