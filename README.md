# Paper Format Translator

Cross-venue manuscript reformatting: one LaTeX paper in, IEEE / ACM / Springer
LNCS documents out.

A paper's source is parsed into a Structured Intermediate Representation (SIR),
rebuilt from structure through venue-specific Jinja2 templates, and compiled with
pdflatex. Round-trip fidelity is **0.9915 F1 over 810 papers** (rebuilt documents
re-extracted and compared field by field), with 48 tests passing.

## Run the web app

```bash
cd app
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open http://localhost:8501.

- **Demo papers** in the sidebar load from `app/cached_outputs/` - instant, no
  network, no TeX required. Use these on demo day.
- **arXiv ID** fetches the source from arXiv and reformats it live (30-60 s).
- **Upload** takes a `.tex` file. Figures are not uploaded, so figure links may
  break; text, sections, tables and references survive.

LaTeX needs `pdflatex` plus the IEEEtran / acmart / llncs classes. The classes are
vendored in `app/vendor/texmf/`; on Debian/Ubuntu the remaining packages come from
`texlive-latex-base texlive-latex-recommended texlive-latex-extra
texlive-fonts-recommended texlive-science`.

## Live demo

[https://paper-format-translator.streamlit.app](https://paper-format-translator.streamlit.app)

Hosted on Streamlit Community Cloud. Click a demo paper in the sidebar for the
instant cached path, or paste an arXiv id / upload a `.tex` for a live run.

GitHub: https://github.com/CodeWithAdarsh007/paper-format-translator (public,
`main` branch, repo root is the app root).

HuggingFace was tried first and is not available for this account: new Spaces
accept only `gradio`/`docker`/`static` SDKs, and Docker Spaces on the free
`cpu-basic` tier return `402 Payment Required` (PRO required). A static Space
cannot run Streamlit or pdflatex. The Docker package is kept in `app/Dockerfile`
for a future PRO deploy.

Live hosting therefore uses **Streamlit Community Cloud**, which runs the same
repo for free and installs TeX via `packages.txt`.

Deploy: https://share.streamlit.io -> New app -> repo
`CodeWithAdarsh007/paper-format-translator`, branch `main`, main file
`streamlit_app.py`.

The reference model (743 MB) is deliberately not bundled: references are parsed
from LaTeX source by `sir_extractor`, so the model has no serving role.

## Layout

```
app/                     Streamlit app, pipeline wrapper, vendored skills, demo cache
  streamlit_app.py       UI
  pipeline.py            process_arxiv / process_tex / process_paper_dir
  cached_outputs/        3 demo papers x 3 PDFs + report.json
  demo_papers/           the 3 demo sources (CC-BY-4.0)
  vendor/skills/         read-only copies of sir_extractor, renderer, templates
  vendor/texmf/          IEEEtran.cls, acmart.cls, llncs.cls
results/session8_report.md   what was built, tested and why
results/screenshots/         4 UI screenshots
skill sources live in D:/OpenCode/skills (read-only for this session)
```

## Notes

- Math-in-text and paper-local macros are not reconstructed, so a rebuilt PDF can
  carry non-fatal pdflatex warnings; the app reports compile status per format
  instead of hiding it.
- PDF upload is rejected on purpose: the pipeline reformats LaTeX source, not
  rendered pages.
