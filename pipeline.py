#!/usr/bin/env python3
"""Serving wrapper around the validated Session 6/7 pipeline (Session 8).

    process_arxiv(arxiv_id) -> result dict
    process_tex(tex_bytes)  -> result dict
    process_paper_dir(dir)  -> result dict   (demo caching / CLI)

The heavy lifting stays in the read-only skills: sir_extractor parses LaTeX into
the SIR, renderer.j2 templates rebuild a full document for another venue, and
pdflatex compiles it. This module only orchestrates them and reports honestly:

* extraction never raises (the SIR carries parse_warnings instead),
* a compile that produces a >20 kB PDF is ``built``; exit 0 on top of that is
  ``clean``; everything else is ``failed`` with the first errors surfaced,
* without pdflatex the .tex is still returned, marked ``unavailable``.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
FORMATS = ("ieee", "acm", "lncs")
FORMAT_LABELS = {"ieee": "IEEE", "acm": "ACM", "lncs": "Springer LNCS"}
MIN_PDF_BYTES = 20 * 1024
COMPILE_TIMEOUT = 180
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

ARXIV_ID_RE = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?$"
)


def _skills_dir() -> Path:
    candidates = [os.environ.get("PFT_SKILLS_DIR"), APP_DIR / "vendor" / "skills",
                  Path("D:/OpenCode/skills")]
    for c in candidates:
        if c and (Path(c) / "sir_extractor.py").is_file():
            return Path(c).resolve()
    raise RuntimeError("pipeline modules not found (set PFT_SKILLS_DIR)")


SKILLS_DIR = _skills_dir()
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

import renderer  # noqa: E402
import sir_extractor  # noqa: E402

TEMPLATES_DIR = SKILLS_DIR / "render_templates"


def find_pdflatex() -> str | None:
    candidates = [os.environ.get("PDFLATEX"), shutil.which("pdflatex"),
                  r"D:\MiKTeX\miktex\bin\x64\pdflatex.exe", "/usr/bin/pdflatex",
                  "/usr/local/bin/pdflatex"]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


def normalize_arxiv_id(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^(?:arxiv:|https?://arxiv\.org/(?:abs|pdf)/)", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s)
    return s.strip()


def _fail(message: str, **extra) -> dict:
    result = {"success": False, "papers": {},
              "report": {"error": message, "parse_warnings": [message],
                         "compile_status": {}, "extracted_refs": 0, "pdf_bytes": {}}}
    result["report"].update(extra)
    return result


def _extract_sir(source_dir: Path, papers_root: str | None):
    return sir_extractor.extract_sir(str(source_dir), source_format="original",
                                     papers_root=papers_root)


FIGURE_EXTS = ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps")
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(\[[^\]]*\])?\{([^}]+)\}")


def _normalise_figure_path(raw) -> str:
    p = str(raw or "").strip().strip("{}").strip().replace("\\", "/")
    # ".." comes from a source bundle, so it must not escape the build dir; the basename index
    # still resolves a legitimate "../figs/x.png" (flattened arXiv sources do this).
    return "/".join(seg for seg in p.split("/") if seg not in ("", ".", ".."))


def _figure_paths(sir: dict) -> list[str]:
    seen, out = set(), []
    for fig in sir.get("figures") or []:
        if not isinstance(fig, dict) or not fig.get("path"):
            continue
        p = _normalise_figure_path(fig["path"])
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _basename_index(source_dir: Path) -> dict:
    index = {}
    try:
        for p in source_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".eps"):
                index.setdefault(p.name.lower(), p)
    except OSError:
        pass
    return index


def copy_figures(sir: dict, source_dir: Path, build_dir: Path) -> dict:
    """Copy the images the SIR names into the build dir at the SIR's own relative paths.

    The document is rebuilt from scratch, so a path like ``figs/plot.pdf`` only means something
    if the build tree has the same shape as the source tree. Exact path -> extension variants ->
    a unique basename elsewhere in the tree; anything still missing is reported and gets a
    placeholder (see apply_figure_fallbacks) instead of a 0-byte PDF.
    """
    source_dir, build_dir = Path(source_dir), Path(build_dir)
    paths = _figure_paths(sir)
    copied, missing, remapped = [], [], {}
    index = None
    for rel in paths:
        src, found = None, rel
        stem, ext = os.path.splitext(rel)
        for cand in [rel] + [stem + a for a in FIGURE_EXTS[1:] if a != ext.lower()]:
            p = source_dir / cand
            if p.is_file():
                src, found = p, cand
                break
        if src is None:
            if index is None:
                index = _basename_index(source_dir)
            hit = index.get(os.path.basename(rel).lower())
            if hit is not None:
                src, found = hit, (stem + hit.suffix if not ext else rel[:-len(ext)] + hit.suffix)
        if src is None:
            missing.append(rel)
            continue
        # An extensionless SIR path must land under the real suffix, or graphicx will not find it;
        # a different extension is only usable if the tex argument is rewritten to match.
        dst_rel = rel if (ext and found == rel) else (found if ext else rel + src.suffix)
        if dst_rel != rel:
            remapped[rel] = dst_rel
        try:
            (build_dir / dst_rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, build_dir / dst_rel)
            copied.append(dst_rel)
        except OSError:
            missing.append(rel)
    return {"requested": len(paths), "copied": len(copied), "missing": len(missing),
            "copied_names": copied, "missing_names": missing, "remapped": remapped}


def apply_figure_fallbacks(tex: str, missing_paths, remapped=None) -> tuple[str, int]:
    """Point \\includegraphics at a substituted file, or at a visible placeholder when none exists.

    A missing image is a hard TeX error ("File ... not found") that can take the whole compile
    with it; the placeholder keeps the float, the caption and the numbering.
    """
    wanted = {_normalise_figure_path(p).lower() for p in missing_paths or []}
    swaps = {_normalise_figure_path(k).lower(): v for k, v in (remapped or {}).items()}
    if not wanted and not swaps:
        return tex, 0
    count = 0

    def repl(m):
        nonlocal count
        path = _normalise_figure_path(m.group(2))
        opts = m.group(1) or ""
        if path.lower() in swaps:
            count += 1
            return "\\includegraphics" + opts + "{" + swaps[path.lower()] + "}"
        if path.lower() not in wanted:
            return m.group(0)
        count += 1
        return "\\fbox{\\textit{[Figure: " + renderer.protect_tex(path) + "]}}"

    return _INCLUDEGRAPHICS_RE.sub(repl, tex), count


def _pdf_pages(pdf_path: Path) -> int:
    # pdflatex's "Output written on ..." line wraps on long temp paths, so read the PDF.
    try:
        import pymupdf
        with pymupdf.open(pdf_path) as doc:
            return doc.page_count
    except Exception:
        try:
            import fitz
            with fitz.open(pdf_path) as doc:
                return doc.page_count
        except Exception:
            return 0


def _compile(tex: str, build_dir: Path, source_dir: Path) -> tuple[dict, bytes | None]:
    build_dir.mkdir(parents=True, exist_ok=True)
    tex_path = build_dir / "doc.tex"
    tex_path.write_text(tex, encoding="utf-8", newline="\n")
    info = {"status": "unavailable", "pdf_bytes": 0, "pages": 0,
            "error_count": 0, "errors": [], "compiler": "", "seconds": 0.0}
    pdflatex = find_pdflatex()
    if not pdflatex:
        info["errors"] = ["pdflatex not installed on this host - .tex available for download"]
        return info, None
    info["compiler"] = pdflatex
    env = os.environ.copy()
    texmf = APP_DIR / "vendor" / "texmf"
    if texmf.is_dir():
        # trailing separator keeps the default search path after ours
        env["TEXINPUTS"] = str(texmf) + os.pathsep + env.get("TEXINPUTS", ".") + os.pathsep
    cmd = [pdflatex, "-interaction=nonstopmode", "-no-shell-escape",
           "-output-directory", str(build_dir), str(tex_path)]
    t0 = time.time()
    rc, log = 1, ""
    for _ in range(2):
        try:
            proc = subprocess.run(cmd, cwd=str(source_dir), capture_output=True,
                                  env=env, timeout=COMPILE_TIMEOUT)
            rc = proc.returncode
            log = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            info["status"] = "failed"
            info["errors"] = [f"pdflatex timed out after {COMPILE_TIMEOUT}s"]
            info["seconds"] = round(time.time() - t0, 1)
            return info, None
        except OSError as exc:
            info["status"] = "failed"
            info["errors"] = [f"could not run pdflatex: {exc}"]
            info["seconds"] = round(time.time() - t0, 1)
            return info, None
    info["seconds"] = round(time.time() - t0, 1)

    log_file = build_dir / "doc.log"
    if log_file.is_file():
        log = log_file.read_text(encoding="utf-8", errors="replace")
    errors = re.findall(r"^! (.+)$", log, re.M)
    seen, uniq = set(), []
    for e in errors:
        e = e.strip()
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    info["error_count"] = len(errors)
    info["errors"] = uniq[:8]
    pages = re.findall(r"Output written on .*?\((\d+) pages?", log)
    info["pages"] = int(pages[-1]) if pages else 0

    pdf_path = build_dir / "doc.pdf"
    if pdf_path.is_file() and pdf_path.stat().st_size > MIN_PDF_BYTES:
        data = pdf_path.read_bytes()
        info["pdf_bytes"] = len(data)
        info["status"] = "clean" if rc == 0 else "built"
        if not info["pages"]:
            info["pages"] = _pdf_pages(pdf_path)
        return info, data
    info["status"] = "failed"
    if not info["errors"]:
        info["errors"] = ["no PDF over 20 kB produced"]
    return info, None


def _render_and_compile(sir_path: Path, work_dir: Path, source_dir: Path,
                        formats, sir: dict | None = None) -> dict:
    papers = {}
    for fmt in formats:
        entry = {"status": "failed", "pdf": None, "tex": None, "pdf_bytes": 0,
                 "pages": 0, "error_count": 0, "errors": [], "warnings": [],
                 "figures": {}}
        try:
            tex, rinfo = renderer.render(str(sir_path), fmt, str(TEMPLATES_DIR))
        except Exception as exc:  # renderer raises on bad format / unreadable SIR only
            entry["errors"] = [f"render failed: {exc}"]
            papers[fmt] = entry
            continue
        entry["warnings"] = rinfo.get("warnings", [])
        build_dir = work_dir / f"{fmt}_build"
        if sir is not None:
            figs = copy_figures(sir, source_dir, build_dir)
            if figs["missing"] or figs.get("remapped"):
                tex, touched = apply_figure_fallbacks(tex, figs["missing_names"],
                                                      figs.get("remapped"))
                figs["placeholders"] = touched
            if figs["missing"]:
                entry["warnings"].append(
                    f"{figs['missing']} figure file(s) missing from the source "
                    f"-> placeholder ({', '.join(figs['missing_names'][:3])})")
            entry["figures"] = figs
        entry["tex"] = tex
        comp, pdf = _compile(tex, build_dir, source_dir)
        entry.update(comp)
        entry["pdf"] = pdf
        papers[fmt] = entry
    return papers


def _build_result(sir: dict, papers: dict, formats, source_label: str,
                  elapsed: float) -> dict:
    compile_status = {f: papers[f]["status"] for f in formats}
    return {
        "success": any(papers[f].get("pdf") for f in formats),
        "papers": papers,
        "report": {
            "paper_id": sir.get("paper_id") or "",
            "title": (sir.get("metadata") or {}).get("title") or "",
            "authors": [a.get("name") if isinstance(a, dict) else str(a)
                        for a in (sir.get("metadata") or {}).get("authors", [])][:12],
            "source": source_label,
            "formats": list(formats),
            "extracted_refs": len(sir.get("references") or []),
            "sections": len(sir.get("sections") or []),
            "figures": len(sir.get("figures") or []),
            "parse_warnings": (sir.get("parse_warnings") or [])[:20],
            "compile_status": compile_status,
            "pdf_bytes": {f: papers[f].get("pdf_bytes", 0) for f in formats},
            "pages": {f: papers[f].get("pages", 0) for f in formats},
            "error_count": {f: papers[f].get("error_count", 0) for f in formats},
            "figure_handling": {f: papers[f].get("figures", {}) for f in formats},
            "elapsed_s": round(elapsed, 1),
        },
    }


def _process_source_dir(source_dir: Path, work_dir: Path, source_label: str,
                        papers_root: str | None = None, formats=FORMATS,
                        paper_id: str | None = None) -> dict:
    t0 = time.time()
    formats = tuple(f for f in FORMATS if f in set(formats)) or FORMATS
    try:
        sir = _extract_sir(source_dir, papers_root)
    except Exception as exc:
        return _fail(f"SIR extraction failed: {exc}", source=source_label)
    if paper_id:
        sir["paper_id"] = paper_id
    if not (sir.get("sections") or (sir.get("metadata") or {}).get("title")):
        warn = "; ".join((sir.get("parse_warnings") or [])[:3]) or "no LaTeX content found"
        return _fail(f"could not read a paper from this source ({warn})",
                     source=source_label)

    sir_path = work_dir / "sir.json"
    sir_path.write_text(json.dumps(sir, ensure_ascii=False), encoding="utf-8")
    papers = _render_and_compile(sir_path, work_dir, source_dir, formats, sir=sir)
    result = _build_result(sir, papers, formats, source_label, time.time() - t0)
    if not result["success"]:
        reasons = []
        for f in formats:
            reasons.extend(papers[f].get("errors", [])[:1])
        result["report"]["error"] = "; ".join(dict.fromkeys(reasons)) or "all formats failed"
    return result


def _flatten_single_dir(root: Path) -> Path:
    entries = [p for p in root.iterdir() if p.name not in ("__MACOSX",)]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def _download_arxiv(arxiv_id: str, dest: Path) -> None:
    import requests

    url = f"https://arxiv.org/e-print/{arxiv_id}"
    resp = requests.get(url, timeout=120, allow_redirects=True,
                        headers={"User-Agent": "paper-format-translator/1.0 (demo app)"})
    resp.raise_for_status()
    data = resp.content
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"arXiv source exceeds {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
    if data[:2] == b"\x1f\x8b":
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                try:
                    tar.extractall(dest, filter="data")
                except TypeError:
                    tar.extractall(dest)
            return
        except tarfile.TarError:
            (dest / "main.tex").write_bytes(gzip.decompress(data))
            return
    if data[:4] == b"PK\x03\x04":
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                target = (dest / member).resolve()
                if dest.resolve() not in target.parents and target != dest.resolve():
                    continue
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))
        return
    (dest / "main.tex").write_bytes(data)


def process_arxiv(arxiv_id: str, formats=FORMATS) -> dict:
    """Fetch arXiv source, parse, render and compile it. Never raises."""
    pid = normalize_arxiv_id(arxiv_id)
    if not ARXIV_ID_RE.match(pid):
        return _fail(f"'{arxiv_id}' is not a valid arXiv identifier (e.g. 2207.01917)")
    work_dir = Path(tempfile.mkdtemp(prefix="pft_arxiv_"))
    source_dir = work_dir / "source"
    source_dir.mkdir()
    try:
        try:
            _download_arxiv(pid, source_dir)
        except Exception as exc:
            return _fail(f"could not fetch arXiv:{pid} - {exc}",
                         source=f"arXiv:{pid}")
        return _process_source_dir(_flatten_single_dir(source_dir), work_dir,
                                   f"arXiv:{pid}", formats=formats, paper_id=pid)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def process_tex(tex_bytes: bytes, filename: str = "main.tex", formats=FORMATS) -> dict:
    """Render an uploaded .tex file. Figures are not uploaded, so they are dropped."""
    if not tex_bytes or not tex_bytes.strip():
        return _fail("the uploaded file is empty")
    if len(tex_bytes) > MAX_UPLOAD_BYTES:
        return _fail(f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    name = os.path.basename(filename or "main.tex")
    if not name.lower().endswith(".tex"):
        return _fail("only .tex uploads are supported (PDF input cannot be re-laid out)")
    work_dir = Path(tempfile.mkdtemp(prefix="pft_upload_"))
    source_dir = work_dir / "source"
    source_dir.mkdir()
    try:
        (source_dir / name).write_bytes(tex_bytes)
        return _process_source_dir(source_dir, work_dir, f"upload:{name}",
                                   formats=formats, paper_id=Path(name).stem)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def process_paper_dir(paper_dir, formats=FORMATS, papers_root=None) -> dict:
    """Render a paper already on disk (demo cache builds). Never raises."""
    src = Path(paper_dir)
    if not src.is_dir():
        return _fail(f"paper dir not found: {src}")
    work_dir = Path(tempfile.mkdtemp(prefix="pft_local_"))
    try:
        return _process_source_dir(src, work_dir, f"demo:{src.name}",
                                   papers_root=papers_root or str(src.parent),
                                   formats=formats)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Render a paper dir / arXiv id to 3 formats")
    ap.add_argument("--paper-dir")
    ap.add_argument("--arxiv-id")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--formats", default=",".join(FORMATS))
    a = ap.parse_args(argv)
    formats = [f.strip() for f in a.formats.split(",") if f.strip()]
    if a.arxiv_id:
        result = process_arxiv(a.arxiv_id, formats)
    elif a.paper_dir:
        result = process_paper_dir(a.paper_dir, formats)
    else:
        ap.error("need --paper-dir or --arxiv-id")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for fmt, entry in result["papers"].items():
        if entry.get("pdf"):
            (out / f"{fmt}.pdf").write_bytes(entry["pdf"])
        if entry.get("tex"):
            (out / f"{fmt}.tex").write_text(entry["tex"], encoding="utf-8")
    (out / "report.json").write_text(json.dumps(result["report"], indent=2),
                                     encoding="utf-8")
    print(json.dumps({k: v for k, v in result["report"].items()
                      if k not in ("parse_warnings",)}, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(_cli())
