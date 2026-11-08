# Paper Format Translator

**Live app:** https://paper-format-translator.streamlit.app/
**Dataset:** arXiv-MFT v3 on Zenodo — **DOI
[10.5281/zenodo.24054291](https://doi.org/10.5281/zenodo.24054291)** (published 2026-11-08)

![status](https://img.shields.io/badge/status-research%20prototype-blue)
![licence-code](https://img.shields.io/badge/code-MIT-green)
![licence-data](https://img.shields.io/badge/dataset-CC--BY--4.0-yellow)
![python](https://img.shields.io/badge/python-3.11-blue)
![tests](https://img.shields.io/badge/tests-382%20passing-brightgreen)
![venues](https://img.shields.io/badge/target%20venues-IEEE%20%C2%B7%20ACM%20%C2%B7%20LNCS-lightgrey)

Cross-venue manuscript reformatting. One LaTeX manuscript goes in; **IEEE**, **ACM** and **Springer
LNCS** documents come out — rebuilt from structure, not relabelled, and with **no language model on
the document path**.

## What it does

```
source .tex (arXiv e-print)
   → SIR            a format-neutral JSON reading of the paper
   → Jinja2         per-class templates over one shared macro library
   → pdflatex       IEEEtran · acmart · llncs
   → PDF
```

The Structured Intermediate Representation (SIR) is the contract. It holds sections, paragraphs,
math spans, paper-local macros, figures with their panels, tables as raw `tabular`, and the
bibliography separately from any class-specific markup, so a venue is a template rather than a
translator. Adding a fourth venue is one template.

## Headline metrics

| | |
|---|---|
| Round-trip F1 (render → re-extract → compare) | **0.9934** over 2,430 paper × format pairs |
| SIR equivalence (source ↔ recompiled body) | **0.9948** mean; 802 / 810 papers at exact agreement |
| Render success | **2,430 / 2,430** |
| ReferenceExtractor entity F1 | **0.8902** [0.8879–0.8926] on 10,000 held-out references |
| Sustained compile rate | **86.7%** (26/30 out-of-corpus stratified sample; 9/30 clean) |
| Test suite | **382 passing** |
| Validator critical content findings | **382 → 15 → 6 → 0 → 0** across Sessions 14A-1 → 14D |
| Tracked content-integrity defects | **26 / 29 closed** |

## How to read these numbers honestly

This project's central finding is about verification, not conversion, and the README states it up
front rather than burying it.

For thirteen sessions the pipeline was verified by compile-error counts, byte-identical-body checks
and float-placement assertions. On that standard five papers were reported as passing. An
independent reader given the original and converted PDFs with no project context found roughly
twenty content-integrity defects per paper — figure bodies missing behind their own captions, sixty
pages of a System Card replaced by one line of literal text, author surnames deleted from the middle
of sentences, an appendix hierarchy renumbered into Roman sections. **None of those produced a
`pdflatex` error.**

The response was a four-gate verification model, and the full account is in
[`docs/report_final.md` §7.6](docs/report_final.md). Two consequences users of this code should
know:

1. **A zero critical score is not evidence of no content loss.** Three genuine losses — every
   `\texttt{}` argument deleted from prose, one panel of each two-panel figure destroyed, four author
   surnames dropped from textual citations — were all scored clean by the automated scanner and were
   found only by reading rendered pages.
2. **The scanner is not wired into the app.** `validate_pdf.py` exists and exits 0/1/2, but
   `app/pipeline.py` does not call it, so the UI can report "built (clean)" for a document the
   scanner would flag. Run the scanner yourself on anything you intend to trust.

## Limitations

Read [`docs/report_final.md` §10](docs/report_final.md) for the full L1–L12 list. The four that
affect what you will actually get:

- **L1 — 56 source-drawn figure bodies in one audited paper are annotated, not reproduced.** Figures
  whose graphic cannot be resolved keep their caption, number and label and carry a reader note:
  *"[Not reproduced by this template — see the original document for this figure.]"* The artwork and
  the `\fbox`/`\parbox` text bodies are absent. Recovery needs a schema change.
- **L2 — CJK content is folded, not typeset.** Chinese runs collapse to a `[CJK]` marker per run; one
  audited paper has 156 such placeholders across three tables. Needs an XeLaTeX/xeCJK migration.
- **L3 / L4 — author-attribution structure is flattened.** Names and counts are right; role headings,
  equal-contribution footnotes and employment qualifiers are not preserved as structure.
- **L7 — the deep audit covers five papers, not fifty.** Corpus-level metrics are real; content-fidelity
  claims are measured on 5 papers × 3 formats plus one 30-paper rate sample.

PDF-only sources are **refused with an explanation**, by design: this pipeline reformats LaTeX source,
not rendered pages.

## Try it

```bash
git clone https://github.com/CodeWithAdarsh007/paper-format-translator
cd paper-format-translator
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Or use the [live app](https://paper-format-translator.streamlit.app/) — three demo papers are cached
and render in under two seconds with no arXiv call and no local TeX. Cold start is ~13 s.

Input either an arXiv ID or an uploaded `.tex` (a `.tar.gz` source tree works best).

## Verification and reproduction

```bash
# Gate 1 — test suite (382 tests)
cd <canonical-skills-tree> && python -m pytest tests/ -q --basetemp=tmp/pytest_local
```

The suite lives in the canonical skills tree, **not** in this repository — `vendor/skills/` ships the
modules but not their tests. `--basetemp` is **required on Windows**: without it pytest reports every
test as passed and still exits 1, because its session-finish cleanup hits a `PermissionError` on a
stale temp junction. Details in [`tests/README.md`](tests/README.md).

```bash
# Gate 2 — automated content scan of a produced PDF
python vendor/skills/validate_pdf.py --pdf out.pdf --source-pages 75 --json
# exit 0 clean · 1 warnings only · 2 critical findings
```

`--source-pages` is required: the truncation check dereferences `None` without it.

Gate 3 and Gate 4 are audits by a reader that did not write the change — a defect checklist pass and a
panel-by-panel, cell-by-cell comparison against rendered page images. There is no script for these;
that is the point.

## Dataset

**arXiv-MFT v3** — 810 papers that each recompiled in all three target classes, drawn from 1,533
licence-cleared candidates and 1,419 downloaded source trees; 3,240 SIRs; 2,430 rendered documents;
80/10/10 split by paper ID.

Licence verification: **810 / 810 on the allowlist, 0 off-list** (736 CC-BY-4.0, 60 CC-BY-SA-4.0,
14 CC0-1.0). The release ships **metadata and arXiv identifiers, not LaTeX source**, so every paper
stays under its own licence and no derivative of a ShareAlike item is conveyed.

- Zenodo (**published 2026-11-08**): <https://doi.org/10.5281/zenodo.24054291> — 11 files (manifest,
  3 splits, schema + v2.2 addendum, datasheet, README, LICENSE, CITATION.cff, `verify_licence.py`),
  182 KB
- Membership caveat worth knowing: `manifest.json` stores licences as **URLs** but its own allowlist as
  short **tags**, so a literal membership test returns 0/810 and looks like an unlicensed corpus.
  `verify_licence.py` normalises before checking.

## Licensing — dual, intentionally

| What | Licence |
|---|---|
| **Code in this repository** | **MIT** — see [`LICENSE`](LICENSE) |
| **arXiv-MFT metadata compilation** | **CC-BY-4.0** — Zenodo DOI [10.5281/zenodo.24054291](https://doi.org/10.5281/zenodo.24054291) |
| **The 810 underlying papers** | **their own licences**, recorded per item in `manifest.json` |

The split exists because a code licence cannot carry data rights, and 60 corpus items are
ShareAlike. See `LICENSE` and the datasheet §6.3 for what each licence does and does not cover.

## Citing

```bibtex
@software{pandey_paper_format_translator,
  author       = {Pandey, Adarsh},
  title        = {Paper Format Translator},
  url          = {https://github.com/CodeWithAdarsh007/paper-format-translator},
  version      = {8fab304},
  year         = {2026}
}

@misc{pandey2026arxivmft,
  author       = {Pandey, Adarsh},
  title        = {arXiv-MFT v3: a licence-verified corpus of 810 arXiv papers with
                  all-three-format recompile status},
  year         = {2026},
  publisher    = {Zenodo},
  doi          = {10.5281/zenodo.24054291},
  url          = {https://doi.org/10.5281/zenodo.24054291}
}
```

Both are in [`CITATION.cff`](CITATION.cff).

## Repository layout

```
streamlit_app.py      UI
pipeline.py           extract → render → compile orchestration
vendor/skills/        the pipeline proper
  sir_extractor.py    .tex → SIR
  renderer.py         SIR → target-class .tex (Jinja2)
  render_templates/   paper_ieee.j2 · paper_acm.j2 · paper_llncs.j2 · _shared.j2
  validate_pdf.py     Gate 2 content scanner
packages.txt          apt packages for the image (names only — no comments, see below)
requirements.txt      Python dependencies
```

**Two traps for anyone deploying this.** (1) `packages.txt` is fed to `xargs apt-get install`, which has
no comment syntax — a `#` line becomes a package name, apt exits non-zero and the app shows "Oh no.
Error running app." Package names only. (2) A `packages.txt` or Dockerfile change takes effect only
after Streamlit rebuilds **and** recycles the container, so verify a build-marker (e.g. the footer
test count) before believing any live measurement. A previous session's "ACM LIVE" claim was measured
on the old image and was true of the container it measured and false of the commit it described.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Short version: changes to `vendor/skills/` must be mirrored to
the canonical skills tree, content-fidelity claims require a Gate 3 or Gate 4 pass rather than a green
test suite, and `packages.txt` takes package names only.

## Status

Research prototype, maintained as part of an undergraduate project (year 3 of 4). The year-4 scope —
figure-body recovery, xeCJK, schema v3 for attribution blocks, corpus re-extraction at 2.2, a
PDF-to-LaTeX model, and wiring Gate 2 into the pipeline — is in
[`docs/report_final.md` §11](docs/report_final.md).
