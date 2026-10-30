---
title: Paper Format Translator
emoji: 📄
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: apache-2.0
---

# Paper Format Translator

Cross-venue manuscript reformatting. One LaTeX manuscript goes in; IEEE, ACM and
Springer LNCS documents come out, rebuilt from structure rather than relabelled.

The pipeline parses the source into a Structured Intermediate Representation
(SIR), re-renders a complete document from SIR content through venue templates,
and compiles it with pdflatex. Round-trip fidelity (render, then re-extract) is
**0.9934 F1 over 810 papers**, i.e. the rebuilt paper still reads back as the
same paper.

Math is preserved end to end (SIR schema v2): `$...$`, `$$...$$`, `\(...\)`,
`\[...\]` and `\ensuremath{...}` spans are lifted out of the source before text
flattening and re-emitted inside their original delimiters, and paper-local
preamble macros travel with the SIR. A math-heavy paper that failed to build at
all (477 pdflatex errors) now compiles with 0.

## How to use it

- **Demo papers** (sidebar): three pre-rendered papers served from cache. Instant,
  no network, no TeX required - this is what to use on demo/viva day.
- **arXiv ID tab**: fetches the source from arXiv, reformats it live (30-60 s).
- **Upload tab**: pick a `.tex` file. Figures are not uploaded, so figure links in
  the rebuilt document may break; text, sections, tables and references survive.

Each format gets a PDF download and the generated `.tex` download. The parse
report lists sections, figures, references, compile status and LaTeX errors.

## Notes and limitations (Session 9.5)

- **Math-mode + figure-heavy papers**: 1709.08492 (73 figures, 51 sections,
  math.DG) previously failed 3/3 formats with a 0 MB PDF. After Session 9.5
  (Unicode fallback + figure path handling) it compiles to valid PDFs with 0
  errors in all three formats. Full 100%-clean compile for math/physics papers is
  future work - see the residual macro-capture limitation.
- **Paper-local macros** (`\DeclareMathOperator`, custom `\def`): partial capture
  via the SIR's `macros` field. Undefined control sequences surface as warnings,
  not hard fails (ReAct's residual 22 errors are all this class: `\reactA`,
  `\actA`, ...).
- **Target corpus**: cs.CL / cs.LG / cs.CV papers compile cleanly. math.DG,
  physics, and custom-`.cls` papers have warning-tier output with documented
  error classes.
- Math symbols that pylatexenc emits as Unicode outside `$...$` are mapped back to
  LaTeX (~120 symbols). Anything unmapped is reported as a warning and left as-is.
- Figures named by the SIR are copied into each build tree at their original
  relative paths; a figure file the source does not contain is replaced by a
  visible placeholder instead of aborting the compile. "Tables" whose body is
  TikZ drawing code are replaced with a placeholder too, since the target classes
  cannot render them.
- PDF upload is rejected on purpose: the pipeline reformats LaTeX source, not
  rendered pages.
- The ReferenceExtractor model (`models/refext/best`, 743 MB) is **not** bundled:
  reference parsing happens in `sir_extractor` from the LaTeX source, so the model
  has no serving role. Torch/transformers are therefore not installed either.

## Run locally

```bash
pip install -r requirements.txt
# LaTeX: MiKTeX or TeX Live must provide pdflatex, IEEEtran.cls, acmart.cls, llncs.cls
streamlit run streamlit_app.py
```

On this machine the validated pipeline lives in `skills/`; the app vendors
read-only copies in `vendor/skills/` so it is self-contained. Set `PFT_SKILLS_DIR`
to point at a different checkout, `PDFLATEX` to point at a different binary.

## Credits

arXiv papers used in the demo are CC-BY-4.0; their ids and titles are shown in the
sidebar and in `cached_outputs/<id>/report.json`.
