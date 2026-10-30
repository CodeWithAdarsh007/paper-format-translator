#!/usr/bin/env python3
"""Paper Format Translator - Streamlit front end (Session 8).

Two ways in (arXiv id, .tex upload) and one way out (PDF per venue). The demo
papers in the sidebar are served from pre-rendered cached_outputs, so the viva
works even when arXiv or the TeX installation is unavailable.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
# The repo root has an unrelated `pipeline/` package; pin the app dir first.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
import pipeline  # noqa: E402

CACHE_DIR = APP_DIR / "cached_outputs"

DEMO_FALLBACK = {
    "2207.01917": "GLANCE: Global to Local Architecture-Neutral Concept-based Explanations",
    "2507.23194": "arXiv 2507.23194",
    "2503.17439": "arXiv 2503.17439",
}

GITHUB_URL = os.environ.get("PFT_GITHUB_URL", "")
DATASET_DOI = os.environ.get("PFT_DATASET_DOI", "")

st.set_page_config(
    page_title="Paper Format Translator - Cross-Venue Manuscript Reformatting",
    page_icon="P",
    layout="wide",
)


def demo_ids() -> list[str]:
    ids = sorted(p.name for p in CACHE_DIR.iterdir() if p.is_dir()) if CACHE_DIR.is_dir() else []
    return ids or sorted(DEMO_FALLBACK)


def demo_title(pid: str) -> str:
    report = CACHE_DIR / pid / "report.json"
    if report.is_file():
        try:
            title = json.loads(report.read_text(encoding="utf-8")).get("title")
            if title:
                return title
        except Exception:
            pass
    return DEMO_FALLBACK.get(pid, pid)


@st.cache_data(show_spinner=False)
def _load_cached(pid: str, stamp: float) -> dict:
    """stamp is the report.json mtime: a cached demo refreshed by a deploy must not serve the
    previous PDFs out of Streamlit's memo, which survives a hot reload of this file."""
    base = CACHE_DIR / pid
    report = {}
    if (base / "report.json").is_file():
        report = json.loads((base / "report.json").read_text(encoding="utf-8"))
    papers = {}
    for fmt in pipeline.FORMATS:
        pdf_path = base / f"{fmt}.pdf"
        tex_path = base / f"{fmt}.tex"
        if not pdf_path.is_file():
            papers[fmt] = {"status": "failed", "pdf": None, "tex": None,
                           "pdf_bytes": 0, "pages": 0, "errors": ["not cached"],
                           "warnings": [], "error_count": 0}
            continue
        papers[fmt] = {
            "status": (report.get("compile_status") or {}).get(fmt, "built"),
            "pdf": pdf_path.read_bytes(),
            "tex": tex_path.read_text(encoding="utf-8") if tex_path.is_file() else None,
            "pdf_bytes": pdf_path.stat().st_size,
            "pages": (report.get("pages") or {}).get(fmt, 0),
            "errors": [],
            "warnings": [],
            "error_count": (report.get("error_count") or {}).get(fmt, 0),
        }
    report.setdefault("paper_id", pid)
    report.setdefault("title", demo_title(pid))
    report["source"] = f"cached demo: {pid}"
    report["extracted_refs"] = report.get("extracted_refs", 0)
    return {"success": any(p["pdf"] for p in papers.values()),
            "papers": papers, "report": report}


def load_cached(pid: str) -> dict:
    report = CACHE_DIR / pid / "report.json"
    stamp = report.stat().st_mtime if report.is_file() else 0.0
    return _load_cached(pid, stamp)


def clear_demo_marker() -> None:
    st.session_state.pop("loaded_demo", None)


def run_job(fn, *args, **kwargs) -> None:
    st.session_state.pop("loaded_demo", None)
    try:
        with st.spinner("Extracting the manuscript, re-rendering it for each venue, "
                        "and compiling the PDFs. This usually takes 30-60 seconds..."):
            st.session_state["result"] = fn(*args, **kwargs)
    except Exception as exc:  # a broken paper must not take the page down
        st.session_state["result"] = pipeline._fail(f"unexpected error: {exc}")


def status_word(entry: dict) -> str:
    return {"clean": "compiled cleanly", "built": "PDF built (warnings)",
            "failed": "compile failed", "unavailable": "PDF unavailable"}.get(
        entry.get("status"), "unknown")


def render_results(result: dict, formats) -> None:
    report = result.get("report") or {}
    if not result.get("success") and not any(
            (result.get("papers") or {}).get(f, {}).get("tex") for f in formats):
        st.error(report.get("error") or "Could not process this paper.")
        return

    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.subheader(report.get("title") or report.get("paper_id") or "Result")
        st.caption(f"source: {report.get('source', 'n/a')} - "
                   f"{report.get('sections', 0)} sections, "
                   f"{report.get('figures', 0)} figures, "
                   f"{report.get('extracted_refs', 0)} references - "
                   f"{report.get('elapsed_s', 0)} s")
    with head_r:
        if report.get("pdf_bytes"):
            total = sum(report["pdf_bytes"].values()) / (1024 * 1024)
            st.metric("PDF output", f"{total:.1f} MB", "3 venues")

    cols = st.columns(len(formats))
    for col, fmt in zip(cols, formats):
        entry = (result.get("papers") or {}).get(fmt) or {}
        with col:
            st.markdown(f"**{pipeline.FORMAT_LABELS[fmt]}**")
            status = entry.get("status")
            if status == "clean":
                st.success(status_word(entry))
            elif status == "built":
                st.warning(status_word(entry))
            else:
                st.error(status_word(entry))
            if entry.get("pdf"):
                st.download_button(
                    "Download PDF", data=entry["pdf"],
                    file_name=f"{report.get('paper_id') or 'paper'}_{fmt}.pdf",
                    mime="application/pdf", use_container_width=True, key=f"dl_pdf_{fmt}")
                n_err = entry.get("error_count", 0)
                st.caption(f"{entry.get('pdf_bytes', 0) // 1024} kB - "
                           f"{entry.get('pages', 0)} pages - "
                           f"{n_err} LaTeX error{'s' if n_err != 1 else ''}")
            if entry.get("tex"):
                st.download_button(
                    "Download .tex", data=entry["tex"].encode("utf-8"),
                    file_name=f"{report.get('paper_id') or 'paper'}_{fmt}.tex",
                    mime="text/plain", use_container_width=True, key=f"dl_tex_{fmt}")
            for err in (entry.get("errors") or [])[:2]:
                st.caption(f"! {err}")

    with st.expander("Parse report"):
        st.json({k: report.get(k) for k in
                 ("paper_id", "source", "formats", "extracted_refs", "sections",
                  "figures", "compile_status", "pages", "error_count", "parse_warnings")})


demo_list = demo_ids()
with st.sidebar:
    st.header("Demo papers")
    st.caption("Pre-rendered and cached, loads instantly without touching arXiv.")
    for pid in demo_list:
        title = demo_title(pid)
        if len(title) > 40:
            title = title[:37].rstrip(" ,;:-") + "..."
        if st.button(f"{pid} - {title}", key=f"demo_{pid}", use_container_width=True):
            st.session_state["result"] = load_cached(pid)
            st.session_state["loaded_demo"] = pid
    if st.session_state.get("loaded_demo"):
        st.caption(f"loaded from cache: {st.session_state['loaded_demo']}")
    st.divider()
    st.metric("Round-trip F1", "0.9934", "810-paper corpus")
    st.caption("Rebuilt documents read back as the same paper (field-level F1 after "
               "render -> re-extract), not just a PDF that looks right.")
    st.divider()
    with st.expander("Pipeline", expanded=False):
        st.markdown(
            "1. arXiv source or uploaded `.tex`\n"
            "2. `sir_extractor` -> Structured Intermediate Representation\n"
            "3. Jinja2 templates -> IEEE / ACM / LNCS LaTeX\n"
            "4. `pdflatex` -> PDF per venue")

st.title("Paper Format Translator")
st.caption("Cross-Venue Manuscript Reformatting - one manuscript in, IEEE / ACM / "
           "Springer LNCS out.")

tab_arxiv, tab_upload = st.tabs(["arXiv ID", "Upload"])

with tab_arxiv:
    arxiv_id = st.text_input("arXiv identifier", placeholder="e.g. 2207.01917",
                             help="Source is fetched from arxiv.org/e-print at request time.")
    fetch = st.button("Reformat from arXiv", type="primary", key="btn_arxiv",
                      on_click=clear_demo_marker)
with tab_upload:
    uploaded = st.file_uploader("Upload a LaTeX manuscript (.tex)",
                                type=["tex", "pdf"],
                                help="Figures are not uploaded, so figure links may break. "
                                     "PDF upload cannot be re-laid out.")
    upload = st.button("Reformat upload", type="primary", key="btn_upload",
                       on_click=clear_demo_marker)

st.markdown("**Output formats**")
fmt_cols = st.columns(3)
selected = []
for col, fmt in zip(fmt_cols, pipeline.FORMATS):
    with col:
        if st.checkbox(pipeline.FORMAT_LABELS[fmt], value=True, key=f"fmt_{fmt}"):
            selected.append(fmt)

if fetch:
    if not selected:
        st.warning("Pick at least one output format.")
    elif not arxiv_id.strip():
        st.warning("Enter an arXiv identifier first.")
    else:
        run_job(pipeline.process_arxiv, arxiv_id.strip(), tuple(selected))

if upload:
    if not selected:
        st.warning("Pick at least one output format.")
    elif uploaded is None:
        st.warning("Choose a .tex file first.")
    elif (uploaded.name or "").lower().endswith(".pdf"):
        st.error("PDF input is not supported - the pipeline reformats LaTeX source, "
                 "not rendered pages. Upload the .tex (or use the arXiv tab).")
    else:
        run_job(pipeline.process_tex, uploaded.getvalue(), uploaded.name, tuple(selected))

if st.session_state.get("result"):
    st.divider()
    render_results(st.session_state["result"],
                   st.session_state["result"]["report"].get("formats") or pipeline.FORMATS)
else:
    st.info("Pick a demo paper in the sidebar, paste an arXiv id, or upload a .tex file. "
            "The demo papers are cached, so they load instantly.")

st.divider()
footer = []
if GITHUB_URL:
    footer.append(f"[Code]({GITHUB_URL})")
if DATASET_DOI:
    footer.append(f"[Dataset]({DATASET_DOI})")
footer.append("Round-trip F1 0.9934 on 810 papers; 61 tests passing (Session 9)")
st.caption("  |  ".join(footer))
