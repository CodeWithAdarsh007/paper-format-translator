# Contributing

Thanks for looking. This is a research prototype maintained alongside an undergraduate project, so the
bar is "does not break the measurement record", not "matches a style guide".

## Three rules that come from how this project failed before

1. **A green test suite is not evidence of content fidelity.** Sessions 9.5–13-FINAL all reported
   passing suites on documents an independent reader rejected. Gate 1 (`pytest` + compile-error counts
   + float placement) is necessary and insufficient. If your change touches what reaches the reader,
   say which higher gate you ran it through, or say that you did not.
2. **`validate_pdf.py` is not wired into the app**, so the UI can call a defective PDF "built (clean)".
   If you run a conversion you intend to trust, run the scanner yourself:
   ```bash
   python vendor/skills/validate_pdf.py --pdf out.pdf --source-pages <N> --json
   ```
   Read its findings as text-layer claims, not page claims — see below.
3. **Report the number, not the impression.** "26 of 29 tracked defects closed" and "56 figure bodies
   annotated, not reproduced" are the honest forms. Do not soften them to "almost all fixed" or "TikZ
   handled".

## Setup

```bash
git clone https://github.com/CodeWithAdarsh007/paper-format-translator
cd paper-format-translator
pip install -r requirements.txt
streamlit run streamlit_app.py
```

A TeX distribution is needed to compile. Without one you can still extract and render, and the test
suite still collects — the pdflatex-invoking tests are gated on a TeX binary being present.

## The mirror rule

`vendor/skills/` is the copy the app runs; this repository's Dockerfile is `COPY . .`, and in the
project's own working tree the app lives under `app/` with the same `vendor/skills/` layout. Some tests
import the canonical skills tree directly (`test_renderer.py` imports
`<canonical-tree>/renderer.py`), while `test_pipeline.py` resolves skills through `pipeline.py` to
`vendor/skills/`. **Edit both trees.** A one-sided edit produces a suite that passes against code that
is not shipping. Verify with `diff -q` over every file you touched.

## Tests

```bash
cd <canonical-skills-tree>   # this repo's vendor/skills/ has no tests/ in it — see tests/README.md
python -m pytest tests/ -q --basetemp=tmp/pytest_local
```

`--basetemp` is **required on Windows** (see `tests/README.md`). 382 tests pass at `8fab304`.

Add a test for anything you fix. Where the claim is about the *typeset page* rather than the emitted
`.tex` — that both panels of a figure survive, that the bibliography prints `[1] [2] [3]` rather than
`[1] [2] [2]` — write a test that compiles and inspects the PDF, gated on TeX availability. String
matching the `.tex` cannot establish those.

## Known false positives — do not chase these

The scanner reads an extracted text layer, so some of its findings are artefacts, not document defects:

- Repeated `1 1 1 1 …` strings and joined cells such as `Model Namenparams…` are PyMuPDF linearising
  typeset superscripts and math subscripts. Both were investigated and **do not reproduce** on the page.
- `blank_crossref_open` fires when a keyword ends one extracted line and its number begins the next, so
  its count moves when floats are repositioned (2402's IEEE count went 7 → 8 after a layout fix, in a
  document with zero real blanks). 85 such findings remain.
- LNCS ligature breakage (`articial`, `di erent`) is primarily a text-layer/searchability defect; the
  rendered glyph is not always wrong.

When in doubt, render the page and look at it. That is Gate 4 and it is the only gate that can settle
these.

## Pull requests

Small and single-purpose. Include:

- what changed and which defect class it addresses (cite the session report or issue),
- Gate 1 result (`pytest` output) and, if content-affecting, Gate 2 output before/after,
- the **Gate-1 error counts before and after** — unchanged counts while critical findings fall is the
  control that shows you reconnected text rather than suppressing errors,
- a statement of what you did **not** verify.

Do not change `packages.txt` to contain comment lines. Streamlit feeds it to `xargs apt-get install`,
which has no comment syntax: a `#` line becomes a package name, apt exits non-zero and the app fails to
start. Package names only, one per line. Also note a `packages.txt`/Dockerfile change only takes effect
after the image is rebuilt *and* the container recycled — confirm a build marker (e.g. the footer test
count) before reporting any live measurement.

## Issues

Bug reports are welcome. Content-loss reports are especially welcome and will be added to the tracked
ledger rather than closed as "compiles fine" — a defect that compiles is exactly the class this project
was blind to. Please include the arXiv ID, the target format, the page, and if possible the source
construct.

## What is deliberately out of scope

- Machine-learned `.tex` parsing. Determinism is the architectural claim; the one trained component
  (`refext`) is off the document path for papers with a `.bib`/`.bbl`.
- PDF-to-LaTeX. PDF-only sources are refused with an explanation. A generative path for them is
  year-4 scope.
- Vendoring `texlive-fonts-extra` (485 MB download / 1.35 GB installed) purely for ACM typeface
  fidelity. Until decided, ACM body text is Computer Modern rather than Libertine, and that is
  documented rather than fixed.
