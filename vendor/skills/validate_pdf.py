#!/usr/bin/env python3
"""Gate-2 content-integrity scanner for a converted paper PDF.

Gate 1 (``app/pipeline.py``) counts LaTeX errors, checks float placement and
diffs the body. None of that sees a defect that compiles cleanly but prints
``<cit.>``, ``[pages=-]foo.pdf`` or ``Section ,`` into the reader's document.
This scanner reads the *text layer of the finished PDF* and reports those
classes.

    python validate_pdf.py --pdf out.pdf --source-pages 75 [--json]

Exit code: 0 clean, 1 at least one critical defect, 2 warnings only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

CRITICAL = "critical"
WARNING = "warning"


@dataclass(frozen=True)
class Check:
    cid: str
    severity: str
    pattern: str
    description: str
    flat: bool = False           # match on whitespace-collapsed page text
    ci: bool = False             # case-insensitive (headings print uppercase)

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, re.M | (re.I if self.ci else 0))


# ``??`` right after a float keyword is a caption reference that failed to
# resolve; the audit treats it as inherited from the source, so it is reported
# separately from a bare ``??``.
_FLOAT_QQ = re.compile(r"^(?:Fig(?:ure)?\.?|Tab(?:le)?\.?)\s")


# Only words that *require* a number to be a reference. "Example:", "Definition:",
# "Remark:" and "Page:" are prose labels that legitimately appear with no number,
# so including them produced false positives on real papers (measured on 2005.14165).
_XREF_WORDS = (
    r"Sections|Section|Appendices|Appendix|Figures|Figure|Tables|Table"
    r"|Equations|Equation|Theorems|Theorem|Lemmas|Lemma|Corollaries|Corollary"
    r"|Propositions|Proposition|Listings|Listing"
)

CHECKS: tuple[Check, ...] = (
    # --- P0-2 / P0-3: unresolved citation and reference placeholders --------
    Check("unresolved_citation", CRITICAL, r"<cit\.>",
          "unresolved citation placeholder"),
    Check("unresolved_ref", CRITICAL, r"<[Rr]ef[^a-zA-Z]",
          "unresolved \\ref/\\autoref/\\cref placeholder"),
    Check("unresolved_qq", CRITICAL,
          r"(?:(?:Fig(?:ure)?\.?|Tab(?:le)?\.?)\s+)?\?\?",
          "unresolved LaTeX cross-reference (??)"),
    # --- P0-1: directive / macro leaks --------------------------------------
    Check("pages_directive_leak", CRITICAL, r"\[pages?=[^\]]*\]",
          "\\includepdf page-range directive leaked as text"),
    Check("systemcard_leak", CRITICAL, r"\[systemcard\]",
          "custom \\systemcard link directive leaked as text"),
    Check("style_directive_leak", CRITICAL, r"\[style=[^\]]*\]",
          "listing \\lstset style directive leaked as text"),
    Check("language_directive_leak", CRITICAL, r"\[language=[^\]]*\]",
          "listing language directive leaked as text"),
    Check("label_leak", CRITICAL, r"\bsec:[A-Za-z0-9_:.\-]+",
          "LaTeX section label leaked as text"),
    Check("lstlabel_leak", CRITICAL, r"\blst:[A-Za-z0-9_:.\-]+",
          "LaTeX listing label leaked as text"),
    Check("utf8gbsn_leak", CRITICAL, r"\bUTF8gbsn\b",
          "CJK font encoding directive leaked as text"),
    Check("fnsymbol_leak", CRITICAL, r"\bfnsymbolarabic\b",
          "footnote-symbol macro name leaked as text"),
    Check("footnote_artifact", CRITICAL, r"\b\d+\s+footnote\b",
          "footnote counter artifact leaked as text"),
    Check("footnote_word", CRITICAL,
          r"(?<![A-Za-z])footnote(?![A-Za-z])(?!\s+\d)(?<!\d\s)(?<!\d\s\s)",
          "bare-word footnote macro leaked as text"),
    # --- P0-6: duplicate URL emission ---------------------------------------
    Check("duplicate_url", CRITICAL,
          r"([A-Za-z0-9:/._?%=&+~#-]{4,}?(?:\.(?:com|org|net|io|ai|dev|gov|edu)"
          r"|/)[A-Za-z0-9:/._?%=&+~#-]*)\1",
          "same URL emitted twice with no separator", flat=True),
    # --- P0-4: blank cross-references ---------------------------------------
    # Comma/period/semicolon only. A colon after the keyword is a *label* form
    # ("Figure: the source drew this with TikZ"), and including it made the TikZ
    # placeholder in 2005.14165 account for 56 of the 57 hits this class reports.
    Check("blank_crossref", CRITICAL,
          r"\b(?:" + _XREF_WORDS + r")\s*[,.;](?=\s|$)",
          "cross-reference keyword followed by no number", flat=True),
    Check("blank_crossref_open", WARNING,
          r"\b(?:" + _XREF_WORDS + r")(?![A-Za-z])(?:\s+[a-z]+){0,3}\s*$",
          "cross-reference keyword ends the line with no number (soft)"),
    # --- P0-5: appendix hierarchy -------------------------------------------
    # A plain "II. APPROACH" Roman heading is normal IEEE/LNCS output and is
    # NOT a defect; what is a defect is appendix material printed as a
    # numbered section, so the heading has to name itself as one.
    Check("numbered_appendix_heading", WARNING,
          r"^\s*(?:[IVXLCDM]{1,7}|\d{1,2})\.\s+.*\b(?:Appendix|Appendices"
          r"|Cont(?:d|inued))\b.*$",
          "numbered section heading that names appendix/continued material",
          ci=True),
    Check("appendix_unnumbered", WARNING,
          r"^\s*Appendix\b(?!\s*[A-Z0-9])",
          "'Appendix' heading with no letter or number", ci=True),
    # --- truncation is measured from the page count, not the text ------------
)

MAX_EXAMPLES = 5


def extract_pages(pdf_path: str) -> list[str]:
    """Per-page text layer of the PDF. pymupdf first, then pdfplumber, pypdf."""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError:
            fitz = None
    if fitz is not None:
        with fitz.open(pdf_path) as doc:
            return [page.get_text() for page in doc]

    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber is not None:
        with pdfplumber.open(pdf_path) as pdf:
            return [(page.extract_text() or "") for page in pdf.pages]

    from pypdf import PdfReader  # last resort
    reader = PdfReader(pdf_path)
    return [(page.extract_text() or "") for page in reader.pages]


def _snippet(text: str, start: int, end: int, width: int = 48) -> str:
    a, b = max(0, start - width), min(len(text), end + width)
    return " ".join(text[a:b].split())


@dataclass
class Finding:
    cid: str
    severity: str
    description: str
    page: int
    snippet: str

    def as_dict(self) -> dict:
        return {"id": self.cid, "severity": self.severity,
                "description": self.description, "page": self.page,
                "snippet": self.snippet}


@dataclass
class Report:
    pdf: str
    pages: int
    source_pages: int | None
    findings: list[Finding]
    counts: dict[str, int]

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == CRITICAL]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def exit_code(self) -> int:
        if self.critical:
            return 1
        return 2 if self.warnings else 0

    def as_dict(self) -> dict:
        return {
            "pdf": self.pdf,
            "pages": self.pages,
            "source_pages": self.source_pages,
            "exit_code": self.exit_code,
            "verdict": {1: "FAIL", 2: "WARN", 0: "CLEAN"}[self.exit_code],
            "critical_counts": {k: v for k, v in self.counts.items()
                                if k in _SEV_OF and _SEV_OF[k] == CRITICAL},
            "warning_counts": {k: v for k, v in self.counts.items()
                               if k in _SEV_OF and _SEV_OF[k] == WARNING},
            "counts": self.counts,
            "findings": [f.as_dict() for f in self.findings],
        }


_SEV_OF = {c.cid: c.severity for c in CHECKS}
_SEV_OF["figure_qq"] = WARNING          # reported by the unresolved_qq pattern
_SEV_OF.setdefault("truncated_output", WARNING)


def scan(pages: list[str], source_pages: int | None = None,
         pdf: str = "") -> Report:
    """Run every check over an already-extracted list of page texts."""
    flat_pages = [" ".join(p.split()) for p in pages]
    findings: list[Finding] = []
    for check in CHECKS:
        rx = check.compiled()
        for pno, raw in enumerate(pages, start=1):
            text = flat_pages[pno - 1] if check.flat else raw
            if not text:
                continue
            for m in rx.finditer(text):
                cid = check.cid
                if cid == "unresolved_qq" and _FLOAT_QQ.match(m.group(0)):
                    cid = "figure_qq"
                sev = _SEV_OF.get(cid, check.severity)
                findings.append(Finding(
                    cid, sev, check.description, pno,
                    _snippet(text, m.start(), m.end())))

    n = len(pages)
    if source_pages and n < 0.6 * source_pages:
        findings.append(Finding(
            "truncated_output", WARNING,
            "converted PDF is under 60%% of the source length "
            "(%d of %d pages)" % (n, source_pages),
            n, "pages=%d source_pages=%d" % (n, source_pages)))

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.cid] = counts.get(f.cid, 0) + 1
    return Report(pdf, n, source_pages, findings, counts)


def validate(pdf_path: str, source_pages: int | None = None) -> Report:
    return scan(extract_pages(pdf_path), source_pages, pdf=pdf_path)


def format_text(report: Report, examples: int = MAX_EXAMPLES) -> str:
    lines = ["== validate_pdf: %s ==" % report.pdf,
             "pages: %d%s" % (report.pages,
                              "" if not report.source_pages
                              else " (source: %d)" % report.source_pages)]
    for sev, label, code in ((CRITICAL, "CRITICAL", "FAIL"),
                             (WARNING, "WARNING", "WARN")):
        bucket = [f for f in report.findings if f.severity == sev]
        if not bucket:
            continue
        lines.append(label)
        order = [c.cid for c in CHECKS if c.severity == sev]
        order += ["figure_qq", "truncated_output"]
        for cid in order:
            hits = [f for f in bucket if f.cid == cid]
            if not hits:
                continue
            lines.append("  %-26s %3d   %s" % (cid, len(hits), hits[0].description))
            for f in hits[:examples]:
                lines.append("      p%-4d %s" % (f.page, f.snippet))
    if not report.findings:
        lines.append("no content defects found")
    lines.append("VERDICT: %s  (exit %d)" % (
        {1: "FAIL", 2: "WARN", 0: "CLEAN"}[report.exit_code], report.exit_code))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdf", required=True, nargs="+",
                    help="converted PDF to scan (several allowed)")
    ap.add_argument("--source-pages", type=int, default=None,
                    help="page count of the source paper, for truncation check")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--examples", type=int, default=MAX_EXAMPLES,
                    help="examples per category in text output (default 5)")
    ns = ap.parse_args(argv)

    reports = [validate(p, ns.source_pages) for p in ns.pdf]
    if ns.json:
        payload = [r.as_dict() for r in reports]
        print(json.dumps(payload[0] if len(payload) == 1 else payload, indent=2))
    else:
        for r in reports:
            print(format_text(r, ns.examples))
            print()
    return max((r.exit_code for r in reports), default=0)


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
