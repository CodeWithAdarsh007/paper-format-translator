#!/usr/bin/env python3
"""sir_extractor - LaTeX paper source -> Structured Intermediate Representation (SIR) JSON.

Session 6 skill. Extracts one SIR per (paper, format) so that the original arXiv source and each
recompiled {ieee, acm, lncs} body can be compared field by field. That comparison is the
"preservation by construction" claim: if the SIRs agree, the recompile preserved the paper rather
than merely producing a PDF that looks right.

    from sir_extractor import extract_sir, compare_sirs
    sir = extract_sir("data/arxiv_mft_v3/papers/2403.11085", source_format="original")

CLI:
    python sir_extractor.py --input <paper_dir_or_tex> --output <json> \
        --source-format original|ieee|acm|lncs [--papers-root <dir>] [--include-root <dir>]

Schema: data/arxiv_mft_v3/sir/SIR_SCHEMA.md (version 1.0).

Two properties the rest of the session depends on:

* **Never raises.** Every anomaly is appended to ``parse_warnings`` and the best partial SIR is
  returned, so one malformed paper cannot stall a 3,240-file batch.
* **Include inlining with a fallback root.** ``recompiled/<id>/<fmt>/`` is pruned by recompile.py to
  ``wrapper.tex`` + ``_mft_body.tex`` (+ ``wrapper.pdf``), but ``_mft_body.tex`` still contains
  ``\\input{sections/00_abstract}`` and friends. Those targets only exist in the *original* paper
  tree, so the fallback root must be passed -- otherwise the recompiled SIR silently loses half the
  paper and the equivalence numbers become meaningless.

Parsing deliberately follows the source document, never a directory glob: ``2403.11085`` ships 51
``.tex`` files including stale drafts (``old_sections/``, ``03_method_v1.tex``) that would corrupt
the SIR if they were swept in.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

SCHEMA_VERSION = "2.0"
FORMATS = ("original", "ieee", "acm", "lncs")

# recompile.py writes "\documentclass{llncs}" for the "lncs" key; the identifier on disk is lncs.
DOCCLASS_HINTS = {"ieee": "IEEEtran", "acm": "acmart", "lncs": "llncs"}

MAX_INCLUDE_DEPTH = 12
MAX_TEXT_BYTES = 4_000_000

SECTIONING = (
    ("subsubsection", 3),
    ("subsection", 2),
    ("section", 1),
    ("part", 1),
    ("chapter", 1),
    ("subparagraph", 4),
    ("paragraph", 4),
)
_SECTION_RE = re.compile(
    r"\\(?:" + "|".join(n for n, _ in SECTIONING) + r")\*?(?:\[[^\]]*\])?\s*\{", re.M
)
_SECTION_LEVEL = dict(SECTIONING)

MATH_ENVS = {"equation", "equation*", "align", "align*", "eqnarray", "eqnarray*",
             "gather", "gather*", "multline", "multline*", "flalign", "flalign*",
             "displaymath", "math"}
LIST_ENVS = {"itemize": "itemize", "enumerate": "enumerate", "description": "itemize"}
FIGURE_ENVS = {"figure", "figure*", "wrapfigure", "float", "sidewaysfigure"}
# Environments whose content *is* the tabular: they carry \begin/\end themselves and must be
# captured whole. Before Session 9.6 only the float names were listed, so a bare \begin{tabular}
# was recursed into as prose ("lrr Model EN-DE FLOPs ..." as one paragraph) and a longtable was
# stored without its \begin, printing its cells as text.
TABULAR_ENVS = {"tabular", "tabular*", "tabularx", "tabulary", "tabu", "supertabular",
                "longtable"}
TABLE_ENVS = {"table", "table*", "sidewaystable"} | TABULAR_ENVS
SKIP_ENVS = {"abstract", "thebibliography"}          # consumed by metadata / references
# Drawing instructions, not prose. Recursing into them turns diagram labels ("π_PC") into Unicode
# text outside math; the templates do not load tikz, so the diagram cannot be reproduced anyway.
DIAGRAM_ENVS = {"tikzcd", "tikzpicture", "pgfpicture", "pspicture", "picture"}
NOISE_COMMANDS = (
    r"\\maketitle\b", r"\\tableofcontents\b", r"\\bibliographystyle\s*\{[^{}]*\}",
    r"\\bibliography\s*\{[^{}]*\}", r"\\addcontentsline\s*\{[^{}]*\}(?:\s*\{[^{}]*\}){2}",
    r"\\appendix\b", r"\\clearpage\b", r"\\newpage\b", r"\\pagebreak\b", r"\\pagenumbering\s*\{[^{}]*\}",
    r"\\thispagestyle\s*\{[^{}]*\}", r"\\markboth\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*\{(?:[^{}]|\{[^{}]*\})*\}",
    r"\\setcounter\s*\{[^{}]*\}\s*\{[^{}]*\}", r"\\hyphenation\s*\{[^{}]*\}",
)
_CITE_RE = re.compile(
    r"\\(?:nocite|cite|Cite|citet|citet|citep|citealp|citeauthor|citeyear|citelist|textcite|"
    r"parencite|autocite|footcite|smartcite|poscite|citename)\*?\b"
)
_REF_RE = re.compile(r"\\(?:cpageref|Cref|cref|autoref|nameref|eqref|refrange|ref)\*?\b")
_ENV_BEGIN_RE = re.compile(r"\\begin\s*\{([^}]*)\}")
_LABEL_RE = re.compile(r"\\label\s*\{([^{}]*)\}")
_DISPLAY_MATH_RE = re.compile(r"(?<!\\)\\\[(.+?)(?<!\\)\\\]", re.S)
_TEX_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")

# ---- math-span preservation (schema v2, Session 9) -------------------------------------------
# latex_to_text() runs pylatexenc, which turns ``\in`` into ``∈`` and drops the ``$`` delimiters.
# The SIR then held ``x ∈ A`` with nothing marking it as math, and the renderer emitted ``∈`` in
# text mode: one "Missing $ inserted" per character. So math is lifted out of the raw source
# BEFORE pylatexenc, replaced by «MATH_<i>», and put back verbatim (with delimiters) at render
# time. Guillemets are used because they appear in no LaTeX special-character list, so neither
# the text converter nor the renderer's escaper touches them.
MATH_PLACEHOLDER = "«MATH_{i}»"
MATH_PLACEHOLDER_RE = re.compile(r"«MATH_\d+»")
# Order matters: $$ before $, and \ensuremath is handled separately because its argument is
# brace-delimited rather than pattern-delimited.
INLINE_MATH_PATTERNS = (
    (re.compile(r"\$\$(.+?)\$\$", re.S), "$$", True),
    (re.compile(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$(?!\$)", re.S), "$", True),
    (re.compile(r"\\\((.+?)\\\)", re.S), "\\(", False),
    (re.compile(r"\\\[(.+?)\\\]", re.S), "\\[", False),
    (re.compile(r"\\begin\{math\}(.+?)\\end\{math\}", re.S), "\\begin{math}", False),
    (re.compile(r"\\begin\{displaymath\}(.+?)\\end\{displaymath\}", re.S),
     "\\begin{displaymath}", False),
)
_ENSUREMATH_RE = re.compile(r"\\ensuremath\s*\{")


# ------------------------------------------------------------------ text primitives

def strip_comments(text: str) -> str:
    """Drop LaTeX ``%`` comments. A ``%`` preceded by an odd number of backslashes is an escaped
    percent and stays put; ``\\%`` (even count) is a line break plus a comment."""
    out = []
    for line in text.split("\n"):
        cut = None
        for i, ch in enumerate(line):
            if ch != "%":
                continue
            back = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                back += 1
                j -= 1
            if back % 2 == 0:
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def match_braces(text: str, open_idx: int):
    """Index of a ``{`` -> (inner_text, index just past its ``}``)."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
        i += 1
    return text[open_idx + 1:], n


def split_top_level(text: str, sep_re: str):
    """Split on ``sep_re`` but only where brace depth is zero, so ``A {B \\and C}`` stays one part.

    An escaped delimiter (``\\{``, ``\\%``, ``\\\\``) is consumed as a pair so it cannot open a group.
    A macro such as ``\\and`` must instead be matched *at its backslash* — skipping the backslash and
    retrying on the letter was silently merging two authors into one.
    """
    pat = re.compile(sep_re)
    parts, last, depth, i, n = [], 0, 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and not text[i + 1].isalpha():
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            m = pat.match(text, i)
            if m:
                parts.append(text[last:m.start()])
                last = i = m.end()
                continue
        i += 1
    parts.append(text[last:])
    return [p for p in parts if p.strip()]


def find_env_end(text: str, body_start: int, name: str):
    """(start, end) of the ``\\end{name}`` matching a ``\\begin{name}`` whose content starts at
    ``body_start``, honouring same-name nesting. ``start == -1`` means the environment never closed."""
    pat = re.compile(r"\\(?:begin|end)\s*\{" + re.escape(name) + r"\}")
    depth = 1
    for m in pat.finditer(text, body_start):
        if text[m.start():m.start() + 6] == "\\begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.start(), m.end()
    return -1, len(text)


_L2T = None
_NEEDS_RENDER = re.compile(r"[\\${}&~%#]")
_L2T_CACHE: dict = {}


def latex_to_text(s: str) -> str:
    """Best-effort printable rendering. refext was trained on plain reference strings, so
    ``references[].raw`` must be text rather than LaTeX. Falls back to a light cleanup.

    Three costs were visible in the profile at 3,240 documents, each handled here: the converter
    object is built once per process (constructing ``LatexNodes2Text`` re-parses the macro graph),
    plain prose skips pylatexenc entirely, and repeat chunks hit a small memo.
    """
    global _L2T
    if not _NEEDS_RENDER.search(s):
        return re.sub(r"\s+", " ", s).strip()
    key = s if len(s) < 2000 else None
    if key is not None:
        hit = _L2T_CACHE.get(key)
        if hit is not None:
            return hit
    s = re.sub(r"\\textbb\s*\{", "{", s)
    if _L2T is None:
        try:
            from pylatexenc.latex2text import LatexNodes2Text
            _L2T = LatexNodes2Text()
        except Exception:
            _L2T = False
    try:
        out = _L2T.latex_to_text(s) if _L2T else s
    except Exception:
        out = s
    out = re.sub(r"\\[a-zA-Z@]+\*?", " ", out)
    out = out.replace("{", "").replace("}", "").replace("~", " ")
    out = re.sub(r"\s+", " ", out).strip()
    if key is not None and len(_L2T_CACHE) < 8192:
        _L2T_CACHE[key] = out
    return out


def _unescaped_dollars(s: str) -> int:
    """Count ``$`` that are not ``\\$`` escapes (``\\`` consumes the next char, so ``\\$`` is not)."""
    n, i, L = 0, 0, len(s)
    while i < L:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "$":
            n += 1
        i += 1
    return n


def _ensuremath_spans(text: str, spans: list):
    """``\\ensuremath{...}`` wraps math without delimiters. Its argument is brace-delimited, so it
    cannot go through the regex table; it is re-emitted as ``$...$``, which is equivalent in text
    mode. Arguments that themselves contain ``$`` are left for the normal pass."""
    out, i = [], 0
    while True:
        m = _ENSUREMATH_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        inner, after = match_braces(text, m.end() - 1)
        if "$" in inner or "\\ensuremath" in inner:
            out.append(text[i:m.end()])
            i = m.end()
            continue
        out.append(text[i:m.start()])
        idx = len(spans)
        spans.append({"raw": inner, "delims": "$", "index": idx})
        out.append(MATH_PLACEHOLDER.format(i=idx))
        i = after
    return "".join(out)


def extract_math_spans(text: str, warnings: list = None):
    """Replace math spans in *raw* LaTeX with ``«MATH_<i>»`` placeholders.

    Must run before ``latex_to_text``: once pylatexenc has turned ``\\in`` into ``∈`` and dropped
    the delimiters the span cannot be recognised any more. Returns ``(text_with_placeholders,
    spans)`` where each span is ``{"raw": ..., "delims": ..., "index": ...}``.

    An odd number of unescaped ``$`` in a segment means the dollars are not a usable delimiter pair
    (a literal ``$5``), so no dollar-based span is extracted and ``unmatched_math_delimiter`` is
    recorded. ``\\$`` is always literal.
    """
    if not text:
        return text, []
    spans = []
    out = _ensuremath_spans(text, spans)
    dollar_ok = _unescaped_dollars(out) % 2 == 0
    if not dollar_ok and warnings is not None:
        warnings.append("unmatched_math_delimiter")
    for pat, delims, uses_dollar in INLINE_MATH_PATTERNS:
        if uses_dollar and not dollar_ok:
            continue

        def replacer(m, delims=delims):
            idx = len(spans)
            spans.append({"raw": m.group(1), "delims": delims, "index": idx})
            return MATH_PLACEHOLDER.format(i=idx)

        out = pat.sub(replacer, out)
    return out, spans


# A bounding-box list that reached prose instead of staying in a layout tool. The nips_2017 source
# defines \todo[1]{\textcolor{red}{[[#1]]}}, and any macro that wraps its argument in [[...]] --
# a todo marker, a PDF-coordinate dump pasted into the source -- survives latex_to_text(), which
# strips the control sequence but keeps the argument. Emitting it puts literal
# "[[76, 877, 488, 906], [506, 182, 918, 211]]" in the PDF, so it is scrubbed from every field
# that goes through text_with_math(). Two or more integer quadruples are required, which no
# legitimate prose or math text matches.
_BBOX_ARTIFACT_RE = re.compile(
    r"(?:text)?\[" + r"\s*\[" + r"\s*-?\d+(?:\.\d+)?" + r"(?:\s*,\s*-?\d+(?:\.\d+)?){3}" + r"\s*\]"
    + r"(?:" + r"\s*,\s*\[" + r"\s*-?\d+(?:\.\d+)?" + r"(?:\s*,\s*-?\d+(?:\.\d+)?){3}" + r"\s*\])+" + r"\s*\]")


def scrub_layout_artifacts(s: str) -> str:
    """Drop bounding-box lists that leaked out of a layout tool into prose."""
    if not s or "[[" not in s:
        return s
    return norm_ws(_BBOX_ARTIFACT_RE.sub(" ", s))


def text_with_math(raw: str, warnings: list = None):
    """Raw LaTeX -> (plain text with placeholders, math spans). The one entry point every
    text-bearing SIR field goes through since v2."""
    clean, spans = extract_math_spans(raw or "", warnings)
    return scrub_layout_artifacts(norm_ws(latex_to_text(clean))), spans


# ---- paper-local macro definitions (schema v2) -----------------------------------------------
# Preserving math ($M_n(\R)$) without the source preamble makes every paper-local macro an
# "Undefined control sequence": 238 of 254 post-fix errors on 2312.00001 were exactly that.
# These are the constructor forms whose meaning is self-contained enough to re-emit; theorem
# declarations, counters and package setup are not, and the rebuilt body never uses them.
_MACRO_CTOR_RE = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator)\*?")
_DEF_RE = re.compile(r"\\def\s*\\([A-Za-z]+)(?![A-Za-z])")


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def collect_macro_definitions(preamble: str):
    """Canonical, re-emittable user macros from the preamble.

    ``\\newcommand`` and friends become ``\\providecommand`` so an accidental collision with a
    loaded package is silent instead of an error; ``\\def`` is kept verbatim (it redefines
    silently by design). Duplicate names keep the first definition, which is what the source's
    own compile would have used.
    """
    defs = {}
    text = preamble or ""
    for m in _MACRO_CTOR_RE.finditer(text):
        ctor = m.group(1)
        i = _skip_ws(text, m.end())
        if i < len(text) and text[i] == "{":
            name, i = match_braces(text, i)
            name = name.strip()
        else:
            nm = re.match(r"\\[A-Za-z]+", text[i:])
            if not nm:
                continue
            name, i = nm.group(0), i + nm.end()
        if not re.fullmatch(r"\\[A-Za-z]+", name) or "@" in name:
            continue
        nargs, default = None, None
        while True:
            j = _skip_ws(text, i)
            if j < len(text) and text[j] == "[":
                k = text.find("]", j)
                if k < 0:
                    break
                if nargs is None:
                    nargs = text[j + 1:k].strip()
                elif default is None:
                    default = text[j + 1:k]
                i = k + 1
                continue
            break
        j = _skip_ws(text, i)
        if j >= len(text) or text[j] != "{":
            continue
        body, _ = match_braces(text, j)
        key = name[1:]
        if key in defs:
            continue
        if ctor == "DeclareMathOperator":
            defs[key] = f"\\DeclareMathOperator{{{name}}}{{{body}}}"
        else:
            opt = f"[{nargs}]" if nargs else ""
            if opt and default is not None:
                opt += f"[{default}]"
            defs[key] = f"\\providecommand{{{name}}}{opt}{{{body}}}"
    for m in _DEF_RE.finditer(text):
        key = m.group(1)
        if key in defs or "@" in key:
            continue
        k = text.find("{", m.end())
        if k < 0:
            continue
        _body, after = match_braces(text, k)
        defs[key] = text[m.start():after]
    return [defs[k] for k in defs]


def clean_heading(s: str) -> str:
    """Heading text: numbers/labels/macros out, plain words in."""
    s = _LABEL_RE.sub(" ", s)
    s = re.sub(r"\\(?:cite|ref)\*?(?:\[[^\]]*\])*\s*\{[^{}]*\}", " ", s)
    return latex_to_text(s) if s.strip() else ""


def norm_ws(s):
    if s is None:
        return None
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ reading & includes

def read_tex(path: str):
    """(text, warning). UTF-8 first, latin-1 fallback, since arXiv sources are a mix."""
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read(), None
        except UnicodeDecodeError:
            continue
        except OSError as e:
            return None, f"cannot read {path}: {e}"
    return None, f"cannot decode {path}"


def _candidate_paths(name: str, cur_dir: str, roots):
    seen, out = set(), []
    bases = [cur_dir] + [r for r in roots if r]
    for base in bases:
        for cand in (name, name + ".tex"):
            p = cand if os.path.isabs(cand) else os.path.join(base, cand)
            p = os.path.normpath(p)
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
    return out


def inline_includes(text: str, cur_dir: str, roots, warnings: list,
                    _depth: int = 0, _seen=None):
    """Replace ``\\input{f}`` / ``\\include{f}`` / ``\\subfile{f}`` with the file's contents.

    Resolution order is the including file's own directory first, then each fallback root, because a
    recompiled body's includes only exist in the original paper tree. Cycles and runaway depth
    degrade to a warning rather than an exception.
    """
    if _seen is None:
        _seen = set()
    if _depth > MAX_INCLUDE_DEPTH:
        warnings.append(f"include depth > {MAX_INCLUDE_DEPTH} at {_depth}: giving up on inlining")
        return text
    out, pos = [], 0
    pat = re.compile(r"\\(?:input|include|subfile|InputIfFileExists)\s*\*?\s*\{([^{}]*)\}")
    for m in pat.finditer(text):
        if m.start() < pos:
            continue
        name = m.group(1).strip()
        if not name:
            continue
        chosen = None
        for p in _candidate_paths(name, cur_dir, roots):
            if os.path.isfile(p):
                chosen = p
                break
        out.append(text[pos:m.start()])
        if chosen is None:
            warnings.append(f"unresolved \\input: {name}")
            out.append("")
        else:
            real = os.path.realpath(chosen)
            if real in _seen:
                warnings.append(f"cyclic \\input skipped: {name}")
                out.append("")
            else:
                body, warn = read_tex(chosen)
                if warn:
                    warnings.append(warn)
                    out.append("")
                else:
                    _seen.add(real)
                    nested = inline_includes(strip_comments(body), os.path.dirname(chosen),
                                             roots, warnings, _depth + 1, _seen)
                    _seen.discard(real)
                    out.append("\n" + nested + "\n")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def read_head(path: str, limit: int = 1_000_000):
    """First ``limit`` bytes only. This exists to avoid dragging whole multi-megabyte trees through a
    network mount while scoring candidates, but the limit must stay above real preamble sizes: the E1
    rehearsal showed a 64 KB cap misranking papers whose ``\\begin{document}`` sits past it, which
    ``recompile.py`` (reading full text) would have ranked differently -- an asymmetry that looked like
    a preservation failure. Once sources are mirrored locally, 1 MB costs nothing."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def find_main_tex(root: str, warnings: list):
    """Main = has ``\\documentclass`` and ``\\begin{document}``; else has ``\\begin{document}``;
    else the largest ``.tex``. Never returns a stale draft that happens to be big."""
    texes = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            if fn.lower().endswith(".tex"):
                p = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                texes.append((p, size))
    if not texes:
        warnings.append(f"no .tex files under {root}")
        return None
    scored = []
    for p, size in texes:
        head = read_head(p)
        if not head:
            continue
        has_class = "\\documentclass" in head
        has_begin = "\\begin{document}" in head
        if has_class and has_begin:
            rank = 0
        elif has_begin:
            rank = 1
        elif has_class:
            rank = 2
        else:
            rank = 3
        scored.append((rank, -size, p))
    if not scored:
        warnings.append("every .tex under the paper tree was unreadable")
        return None
    scored.sort()
    rivals = [t for t in scored if t[0] == 0]
    if len(rivals) > 1:
        # recompile.py picks the largest \\documentclass+\\begin{document} file, and so do we, on
        # purpose: E1 must compare the same document on both sides. Where a package also ships a
        # longer rebuttal or supplementary file, that shared rule picks the wrong paper - a real
        # dataset artefact worth counting, not something to "fix" on one side of the comparison.
        warnings.append(f"{len(rivals)} candidate main .tex files (picked "
                        f"{os.path.basename(scored[0][2])}; largest-first as recompile.py does)")
    if scored[0][0] >= 2:
        warnings.append(f"no .tex contains \\begin{{document}} ({len(texes)} .tex files; "
                        f"fell back to {os.path.basename(scored[0][2])})")
    return scored[0][2]


# ------------------------------------------------------------------ metadata

def _macro_args(text: str, name: str, flags: int = 0):
    """Every ``\\name[..]{..}`` invocation's braced arguments."""
    pat = re.compile(r"\\" + re.escape(name) + r"\b\s*(?:\[[^\]]*\])?\s*\{", flags)
    out = []
    for m in pat.finditer(text):
        inner, after = match_braces(text, m.end() - 1)
        args = [inner]
        tail = text[after:]
        opt = re.match(r"\s*(?:\[[^\]]*\])?\s*\{", tail)
        if opt:
            second, _ = match_braces(text, after + opt.end() - 1)
            args.append(second)
        out.append(args)
    return out


_AUTHOR_SEP_RE = r"\\[aA][nN][dD]\b"

# A trailing \AND block often carries the group affiliation rather than a person
# ("\AND \\ Advanced Micro Devices, Inc. (AMD)"). Without this rule that line becomes an author
# with no name/e-mail of its own (2507.23194 went from 10 authors to 11).
_ORG_HINT_RE = re.compile(
    r"\b(?:University|Universit\w*|Institute|Institut|Department|Dept\.?|Laborator(?:y|ies)|Lab|"
    r"Research|Corporation|Corp\.?|Inc\.?|Ltd\.?|LLC|GmbH|School|College|Faculty|Center|Centre|"
    r"Academy|Hospital|Foundation|Company)\b", re.I)


def _author_rows(raw: str):
    """Split an author chunk on top-level ``\\\\`` line breaks, brace-aware.

    ``split_top_level`` cannot do this: it consumes a backslash pair as an escape before it
    consults the separator pattern, so ``\\\\`` never matches there. Lines are what separates a
    name from its affiliation and e-mail in the NIPS/ICML ``\\author`` layout.
    """
    parts, last, depth, i, n = [], 0, 0, 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            if raw[i + 1] == "\\":
                if depth == 0:
                    parts.append(raw[last:i])
                    last = i + 2
                i += 2
                continue
            if not raw[i + 1].isalpha():
                i += 2
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        i += 1
    parts.append(raw[last:])
    return [p for p in parts if p.strip()]


def _clean_author_field(s: str) -> str:
    r"""Drop superscript affiliation markers from a name/affiliation string.

    ``^1University of Illinois Chicago`` is how nips_2017-style blocks attach \inst numbers to the
    institute line. LaTeX reads the bare ``^`` as a math superscript ("Missing $ inserted"), and the
    marker itself carries no meaning once the affiliation is separated from the name.
    """
    s = re.sub(r"\^\s*\d+", "", s or "")
    s = re.sub(r"\^\s*\{[^{}]*\}", "", s)
    s = s.replace("^", "")
    return norm_ws(s)


def parse_authors(author_src: str, institute_src=None):
    """``\\author`` -> [{name, affiliation, email}].

    ``\\and`` is the declared separator, but llncs bodies write one comma-separated ``\\author{}``
    with ``\\inst{n}`` markers, so a lone chunk is split on top-level commas. ACM uses
    ``\\affiliation{\\institution{X}}``; llncs puts the institutions in a parallel ``\\institute``
    block whose ``\\and``-split entries line up with ``\\inst{n}`` indices.

    The separator is matched case-insensitively (``\\and``/``\\And``/``\\AND``): nips_2017 papers
    mix all three, and a case-sensitive match silently merged eight authors into one. Footnote
    markers are dropped *including their optional argument* -- ``\\footnotemark[1]`` otherwise
    leaves a literal ``[1]`` glued to the name.
    """
    chunks = split_top_level(author_src, _AUTHOR_SEP_RE) or [author_src]
    if len(chunks) == 1 and "," in chunks[0] and "\\inst" in chunks[0]:
        chunks = split_top_level(chunks[0], r",")
    institutes = []
    if institute_src:
        for part in split_top_level(institute_src, _AUTHOR_SEP_RE):
            institutes.append(norm_ws(latex_to_text(re.sub(r"\\\\", " ", part))))
    authors = []
    for raw in chunks:
        raw = re.sub(r"\\samethanks\s*(?:\[[^\]]*\])?", " ", raw)
        raw = re.sub(r"\\footnotemark\s*(?:\[[^\]]*\])?", " ", raw)
        raw = re.sub(r"\\footnotetext\s*(?:\[[^\]]*\])?\s*\{(?:[^{}]|\{[^{}]*\})*\}", " ", raw)
        raw = re.sub(r"\\thanks\s*\{(?:[^{}]|\{[^{}]*\})*\}", " ", raw)
        email = None
        em = re.search(r"\\(?:email|Eprint|url)\s*\{([^{}]*)\}", raw)
        if em:
            email = em.group(1).strip()
        else:
            em = re.search(r"\\texttt\s*\{\s*([^{}\s]*@[^{}\s]*)\s*\}", raw)
            if em:
                email = em.group(1).strip()
        inst_idx = None
        im = re.search(r"\\inst\s*\{([^{}]*)\}", raw)
        if im:
            digits = re.findall(r"\d+", im.group(1))
            inst_idx = int(digits[0]) - 1 if digits else None
        aff = None
        for affpat in (r"\\affiliation\s*\{", r"\\institution\s*\{"):
            am = re.search(affpat, raw)
            if am:
                inner, _ = match_braces(raw, am.end() - 1)
                inn = re.search(r"\\institution\s*\{([^{}]*)\}", inner)
                aff = norm_ws(latex_to_text(inn.group(1) if inn else inner))
                break
        if aff is None and inst_idx is not None and 0 <= inst_idx < len(institutes):
            aff = institutes[inst_idx]
        rows = _author_rows(raw)
        name_src = rows[0] if rows else raw
        if aff is None:
            rest = [r for r in rows[1:]
                    if not re.search(r"\\texttt\s*\{[^{}]*@|\\url\s*\{", r)]
            rest = [_clean_author_field(latex_to_text(r)) for r in rest]
            rest = [r for r in rest if r]
            if rest:
                aff = rest[0]
        elif aff:
            aff = _clean_author_field(aff)
        for drop in (r"\\inst\s*\{[^{}]*\}", r"\\orcidlink\s*\{[^{}]*\}",
                     r"\\footnotemark\s*(?:\[[^\]]*\])?", r"\\footnote\s*\{(?:[^{}]|\{[^{}]*\})*\}",
                     r"\\email\s*\{[^{}]*\}", r"\\affiliation\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}",
                     r"\\url\s*\{[^{}]*\}", r"\$?\^[^$]*\$?", r"\\textsuperscript\s*\{[^{}]*\}",
                     r"\\texttt\s*\{[^{}]*\}", r"\\hspace\*?\s*\{[^{}]*\}", r"\\hfill\b",
                     r"\\quad\b", r"\\qquad\b", r"\\\\", r"\\dagger\b", r"\\thanks"):
            name_src = re.sub(drop, " ", name_src)
        name = norm_ws(latex_to_text(name_src))
        if name:
            if (len(rows) <= 1 and aff is None and email is None and authors
                    and _ORG_HINT_RE.search(name)):
                if not authors[-1]["affiliation"]:
                    authors[-1]["affiliation"] = name
                continue
            authors.append({"name": name, "affiliation": aff or None, "email": email or None})
    return authors


TITLE_MACROS = ("title", "icmltitle", "papertitle", "bmvcpaper", "titlefootnote")
AUTHOR_MACROS = ("author",)
DEF_RE_TMPL = (r"\\(?:def|newcommand|renewcommand|providecommand|let)\s*\\%s\s*"
               r"(?:\[[^\]]*\])?\s*\{")


TITLE_GENERIC_SKIP = re.compile(r"running|note|format|font|sep|width|cover|head", re.I)
AUTHOR_GENERIC_SKIP = re.compile(r"running|note|title|list|sync|equal|address|thanks|copyright", re.I)


def _generic_brick(text: str, base: str, skip: re.Pattern):
    r"""Any ``\<anything>title{..}`` / ``\<anything>author{..}{keys}`` macro, e.g. MLSys."""
    out = []
    for m in re.finditer(r"\\([A-Za-z]*" + base + r"[A-Za-z]*)\b\s*\{", text, re.I):
        name = m.group(1)
        if skip.search(name):
            continue
        a1, after = match_braces(text, m.end() - 1)
        rest = text[after:]
        nxt = re.match(r"\s*\{", rest)
        a2 = None
        if nxt:
            a2, _ = match_braces(text, after + nxt.end() - 1)
        if a1.strip():
            out.append((name, a1, a2))
    return out


def find_title(sources):
    """Title via the class's own macro, then case-insensitively (INFORMS ``\\TITLE``), then
    ``\\def\\papertitle{...}``-style definitions, then any ``\\*title`` macro the template defines
    (MLSys ``\\mlsystitle``).

    Ordered exact-first on purpose: a paper whose title the original code found must keep producing
    byte-identical output, so the new passes only fire where the old one returned nothing.
    """
    for src in sources:
        for name in TITLE_MACROS:
            for args in _macro_args(src, name):
                t = norm_ws(clean_heading(args[0]))
                if t:
                    return t, name
    for src in sources:
        for name in TITLE_MACROS:
            for args in _macro_args(src, name, re.I):
                t = norm_ws(clean_heading(args[0]))
                if t:
                    return t, f"{name}(ci)"
    for src in sources:
        for name in TITLE_MACROS:
            dm = re.search(DEF_RE_TMPL % re.escape(name), src)
            if dm:
                inner, _ = match_braces(src, dm.end() - 1)
                t = norm_ws(clean_heading(inner))
                if t:
                    return t, f"{name}(def)"
    for src in sources:
        for nm, a1, _a2 in _generic_brick(src, "title", TITLE_GENERIC_SKIP):
            t = norm_ws(clean_heading(a1))
            if t:
                return t, nm
    return "", None


def _iterated_macro_args(text: str, name: str, nargs: int = 1):
    """Every ``\\name{a}{b}`` invocation's arguments, in document order."""
    out = []
    for m in re.finditer(r"\\" + re.escape(name) + r"\b\s*\{", text):
        inner, after = match_braces(text, m.end() - 1)
        args, pos = [inner], after
        for _ in range(nargs - 1):
            nxt = re.match(r"\s*\{", text[pos:])
            if not nxt:
                break
            a2, pos = match_braces(text, pos + nxt.end() - 1)
            args.append(a2)
        out.append(args)
    return out


def find_authors(text: str):
    """Authors from ``\\author``/``\\icmlauthor``/``\\addauthor``/``\\authorlist`` variants."""
    for name in ("author",):
        for args in _macro_args(text, name):
            inst = _macro_args(text, "institute")
            got = parse_authors(args[0], inst[0][0] if inst else None)
            if got:
                return got, name
    affil = {}
    for a in _iterated_macro_args(text, "icmlaffiliation", 2):
        affil[a[0].strip()] = norm_ws(latex_to_text(a[1]))
    got = []
    for a in _iterated_macro_args(text, "icmlauthor", 2):
        nm = norm_ws(latex_to_text(a[0]))
        keys = [k.strip() for k in re.split(r"[,&]", a[1])] if len(a) > 1 else []
        aff = "; ".join(affil[k] for k in keys if k in affil) or None
        if nm:
            got.append({"name": nm, "affiliation": aff, "email": None})
    if got:
        return got, "icmlauthor"
    got = []
    for a in _iterated_macro_args(text, "addauthor", 3):
        nm = norm_ws(latex_to_text(a[0]))
        if nm:
            got.append({"name": nm, "affiliation": None, "email": norm_ws(a[1]) if len(a) > 1 else None})
    if got:
        return got, "addauthor"
    for alt in ("authorlist", "authors", "theauthor"):
        for a in _macro_args(text, alt):
            got = parse_authors(a[0])
            if got:
                return got, alt
    for args in _macro_args(text, "author", re.I):
        inst = _macro_args(text, "institute", re.I)
        got = parse_authors(args[0], inst[0][0] if inst else None)
        if got:
            return got, "author(ci)"
    affil = {}
    for _nm, a1, a2 in _generic_brick(text, "affiliation", re.compile(r"running|list", re.I)):
        if a2:
            affil.setdefault(a1.strip(), norm_ws(latex_to_text(a2)))
    got = []
    for _nm, a1, a2 in _generic_brick(text, "author", AUTHOR_GENERIC_SKIP):
        nm = norm_ws(latex_to_text(a1))
        if not nm or len(nm) > 250:
            continue
        aff = None
        if a2:
            resolved = [affil[k.strip()] for k in re.split(r"[,;]", a2) if k.strip() in affil]
            aff = "; ".join(resolved) or None
        got.append({"name": nm, "affiliation": aff, "email": None})
    if got:
        return got, "generic"
    return [], None


def extract_metadata(preamble: str, body: str, warnings: list):
    """Title/authors/abstract/keywords, searched in the preamble first then the body.

    Single-file papers declare ``\\title`` in the preamble; llncs-style papers put ``\\title``,
    ``\\author`` and ``\\maketitle`` *inside* the body, and recompile.py splits the document into
    ``wrapper.tex`` (preamble) + ``_mft_body.tex`` (body). Searching both is what makes the original
    SIR and the recompiled SIR describe the same title instead of disagreeing on where it lives.
    """
    md = {"title": "", "authors": [], "abstract": "", "keywords": [], "title_macro": None,
          "author_macro": None}
    srcs = [s for s in (preamble, body) if s]
    md["title"], md["title_macro"] = find_title(srcs)
    if not md["title"]:
        warnings.append("no \\title found")
    for src in srcs:
        got, macro = find_authors(src)
        if got:
            md["authors"], md["author_macro"] = got, macro
            break
    if not md["authors"]:
        warnings.append("no \\author found")
    ab = re.search(r"\\begin\s*\{abstract\}(.*?)\\end\s*\{abstract\}", body, re.S)
    if not ab:
        ab = re.search(r"\\begin\s*\{abstract\}(.*?)\\end\s*\{abstract\}", preamble, re.S)
    if ab:
        raw_ab = ab.group(1)
    else:
        am = re.search(r"\\abstract\s*\{", body + preamble)
        raw_ab = None
        if am:
            raw_ab, _ = match_braces(body + preamble, am.end() - 1)
        else:
            warnings.append("no abstract found")
    if raw_ab is not None:
        md["abstract"], ab_spans = text_with_math(raw_ab, warnings)
        if ab_spans:
            md["abstract_math_spans"] = ab_spans
    kw_src = body + "\n" + preamble
    m = re.search(r"\\keywords\s*\{", kw_src)
    if m:
        inner, _ = match_braces(kw_src, m.end() - 1)
        md["keywords"] = [norm_ws(latex_to_text(k)) for k in split_top_level(inner, r",") if k.strip()]
    else:
        m = re.search(r"\\begin\s*\{keywords\}(.*?)\\end\s*\{keywords\}", kw_src, re.S)
        if m:
            md["keywords"] = [norm_ws(latex_to_text(k))
                              for k in split_top_level(m.group(1), r",") if k.strip()]
        else:
            for km in re.finditer(r"\\key\s*\{([^{}]*)\}\s*\{([^{}]*)\}", kw_src):
                md["keywords"].append(norm_ws(km.group(2)))
    return md


# ------------------------------------------------------------------ floats, math, lists

def parse_figure(inner: str, warnings: list):
    fig = {"label": None, "caption": None, "path": None, "placement": None}
    cap = re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", inner)
    if cap:
        text, _ = match_braces(inner, cap.end() - 1)
        lab = _LABEL_RE.search(text)
        if lab:
            fig["label"] = lab.group(1).strip()
            text = _LABEL_RE.sub(" ", text)
        fig["caption"], spans = text_with_math(text, warnings)
        if spans:
            fig["math_spans"] = spans
    ig = re.search(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}", inner)
    if ig:
        fig["path"] = ig.group(1).strip()
    else:
        ig = re.search(r"\\pgfimage\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}", inner)
        if ig:
            fig["path"] = ig.group(1).strip()
        elif "\\includegraphics" not in inner and not fig["caption"]:
            warnings.append("figure environment with neither caption nor graphics")
    if not fig["label"]:
        lab = _LABEL_RE.search(inner)
        if lab:
            fig["label"] = lab.group(1).strip()
    pl = re.search(r"\\begin\s*\{figure\}?\s*\[([^\]]*)\]", inner)
    if pl:
        fig["placement"] = pl.group(1).strip() or None
    return fig


def parse_table(inner: str, warnings: list, env: str = None, whole: str = None):
    tab = {"label": None, "caption": None, "latex": ""}
    cap = re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", inner)
    if cap:
        text, _ = match_braces(inner, cap.end() - 1)
        lab = _LABEL_RE.search(text)
        if lab:
            tab["label"] = lab.group(1).strip()
            text = _LABEL_RE.sub(" ", text)
        tab["caption"], spans = text_with_math(text, warnings)
        if spans:
            tab["math_spans"] = spans
    if env in TABULAR_ENVS:
        # The environment itself is the table: keep begin/end and every cell byte for byte.
        tab["latex"] = (whole if whole is not None else inner).strip()
    else:
        body = re.search(r"\\begin\s*\{((?:tabular|tabularx|array|nicematrix|bmatrix|IEEEeqnarray"
                         r"|longtable|tabulary|tabu|supertabular)\*?)\}\s*(?:\[[^\]]*\])?\s*\{",
                         inner)
        if body:
            envname = re.match(r"\\begin\s*\{([^}]*)\}", body.group(0)).group(1)
            end, _ = find_env_end(inner, body.end(), envname)
            tab["latex"] = inner[body.start(): end + len(envname) + 6].strip() if end > 0 \
                else inner[body.start():].strip()
        else:
            tab["latex"] = inner.strip()
            if inner.strip():
                warnings.append("table without a tabular environment")
    if not tab["label"]:
        lab = _LABEL_RE.search(inner)
        if lab:
            tab["label"] = lab.group(1).strip()
    return tab


def parse_math(inner: str, env: str, whole: str):
    lab = _LABEL_RE.search(inner)
    latex = whole.strip()
    if lab:
        latex = latex.replace(lab.group(0), "").strip()
    # Removing a \label that sat on its own line leaves a whitespace-only line, and a blank line
    # inside align/equation is a TeX error ("Missing $ inserted", "\par ended"): the rendered PDF
    # lost the whole display. Collapse blank lines; inside math they can only be a defect.
    latex = re.sub(r"\n\s*\n", "\n", latex)
    return {"label": lab.group(1).strip() if lab else None, "latex": latex, "env": env}


def parse_list(inner: str, kind: str, warnings: list = None):
    """``\\item`` separated entries. Any lead-in text before the first ``\\item`` is dropped, and
    ``description`` entries keep only their term+body after the optional label argument.

    v2: ``math_spans`` is a list parallel to ``items`` (one span list per item, ``[]`` for an item
    without math), present only when at least one item carries math.
    """
    parts = re.split(r"\\item\b(?:\[[^\]]*\])?", inner)
    items, item_spans = [], []
    for chunk in parts[1:]:
        txt, spans = text_with_math(chunk, warnings)
        if txt:
            items.append(txt)
            item_spans.append(spans)
    if not items:
        txt, spans = text_with_math(parts[0], warnings)
        if txt:
            items, item_spans = [txt], [spans]
    block = {"type": kind, "items": items}
    if any(item_spans):
        block["math_spans"] = item_spans
    return block


# ------------------------------------------------------------------ references

def find_env_close(text: str, start: int):
    """Index of the ``}`` closing a group that opened just before ``start``."""
    depth = 1
    i = start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i, i + 1
        i += 1
    return -1, len(text)


def _bib_fields_from_regex(text: str, warnings: list):
    """Fallback .bib reader when bibtexparser is unavailable or chokes on a malformed entry."""
    out = []
    for m in re.finditer(r"@\s*(\w+)\s*\{\s*([^,\s]+)\s*,", text):
        etype, key = m.group(1).lower(), m.group(2)
        end, _ = find_env_close(text, m.end())
        block = text[m.end():end] if end > 0 else text[m.end():]
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*[{\"](.*)[}\"]\s*,?\s*\n?", block, re.S):
            fields[fm.group(1).lower()] = norm_ws(strip_comments(fm.group(2)))
        out.append((key, {"entry_type": etype, **fields}))
    return out


def bib_index(path: str, warnings: list):
    """``.bib`` -> ``{key: {entry_type, author, title, year, ...}}``, the ground truth Step E needs.

    bibtexparser 2.x exposes ``parse_string``/``parse_file`` on a ``Library`` of ``Entry`` objects with
    ``.fields`` of ``Field(key, value)``; 1.x's ``bibtexparser.parse``/``loads`` do not exist in 2.1,
    and calling them silently dropped every run into the regex fallback.
    """
    text, warn = read_tex(path)
    if warn:
        warnings.append(warn)
        return {}
    try:
        import bibtexparser
        lib = bibtexparser.parse_string(text)
        out = []
        for e in getattr(lib, "entries", None) or []:
            fields = {}
            for f in getattr(e, "fields", None) or []:
                v = f.value if isinstance(f.value, str) else str(getattr(f.value, "text_value", f.value))
                fields[str(f.key).lower()] = norm_ws(v.replace("{", "").replace("}", ""))
            out.append((e.key, {"entry_type": getattr(e, "entry_type", "misc"), **fields}))
        if out:
            return dict(out)
        warnings.append("bibtexparser returned no entries; using regex fallback")
    except Exception as e:
        warnings.append(f"bibtexparser failed ({type(e).__name__}: {e}); using regex fallback")
    return dict(_bib_fields_from_regex(text, warnings))


def render_bib_raw(fields: dict, key: str) -> str:
    """A printable citation string, which is the shape refext was trained on."""
    a = norm_ws(re.sub(r"\s+and\s+", ", ", fields.get("author", "")))
    t = norm_ws(fields.get("title", ""))
    j = norm_ws(fields.get("journal") or fields.get("booktitle") or fields.get("institution")
                or fields.get("school") or "")
    y = norm_ws(fields.get("year", ""))
    v, n, pg = fields.get("volume", ""), fields.get("number", ""), fields.get("pages", "")
    parts = []
    if a:
        parts.append(a + ".")
    if t:
        parts.append(t.rstrip(".") + ".")
    tail = " ".join(x for x in (j, (v and f"vol. {v}"), (n and f"no. {n}"), (pg and f"pp. {pg}")) if x)
    if tail:
        parts.append(tail + (f", {y}." if y else "."))
    elif y:
        parts.append(f"{y}.")
    if not parts:
        parts.append(f"[{key}]")
    return norm_ws(" ".join(parts))


def refs_from_bib(paths, warnings: list, cited_keys=None):
    """Bibliography entries from one or more ``.bib`` files, restricted to the keys actually cited.

    ``\\bibliography{anthology,custom}`` is common in ACL-style packages, where ``anthology.bib`` is the
    whole ACL Anthology (~80,000 entries) and ``custom.bib`` holds the paper's own. Taking the file at
    face value turned one paper into an 80,558-entry reference list. A paper's reference list is what it
    cites, so the cited-key set filters the union; if nothing intersects (unusual styles) the full entry
    list is kept and the reason is recorded, because losing every reference is worse than over-inclusive.
    """
    if isinstance(paths, str):
        paths = [paths]
    merged = {}
    order = []
    for p in paths:
        for key, fields in bib_index(p, warnings).items():
            if key not in merged:
                order.append(key)
            merged[key] = fields          # later files win: custom overrides anthology
    if cited_keys and merged:
        keep = [k for k in order if k in cited_keys]
        if keep:
            dropped = len(merged) - len(keep)
            if dropped > 0:
                warnings.append(f"restricted {len(merged)} bib entries to {len(keep)} cited "
                                f"(dropped {dropped} uncited)")
            merged = {k: merged[k] for k in keep}
            order = keep
        else:
            warnings.append(f"none of the {len(cited_keys)} cited keys matched {len(merged)} bib "
                            f"entries; keeping all entries")
    return [{"raw": render_bib_raw(merged[k], k), "key": k, "structured": merged[k],
             "extracted": None} for k in order]


def cited_key_set(text: str):
    """Every key named by a ``\\cite``-family macro anywhere in the document, in order of appearance."""
    keys = []
    for m in _CITE_RE.finditer(text):
        i = _args_end(text, m.end())
        j = m.end()
        while j < i:
            if text[j] == "{":
                inner, j = match_braces(text, j)
                for k in re.split(r"[,;]", inner):
                    k = k.strip()
                    if k and k not in keys:
                        keys.append(k)
            else:
                j += 1
    return keys


def _norm_title(s: str) -> str:
    """Lowercased alphanumerics only, for matching a ``.bib`` title against compiled ``.bbl`` text."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _bibitem_refs(text: str, index=None, title_map=None):
    """``\\bibitem[..]{key} …`` entries. The optional label often carries author/year, which is kept
    as ground truth for numeric styles whose ``\\bibitem`` keys do not match the ``.bib``.

    Key identity is tried first, then a title-substring match: styles such as IEEE/alpha numerical
    rewrite or truncate keys (``.bib`` ``allen2020rapid`` vs ``.bbl`` ``bib1``), and without the title
    fallback those papers would contribute no field-level ground truth for Step E.
    """
    refs = []
    index = index or {}
    title_map = title_map or {}
    for m in re.finditer(r"\\bibitem\s*(\[([^\]]*)\])?\s*\{([^{}]*)\}", text):
        key = m.group(3).strip()
        nxt = re.search(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{", text[m.end():])
        chunk = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]
        raw = norm_ws(latex_to_text(re.sub(r"\\newblock\b", " ", chunk)))
        if not raw:
            continue
        structured = {"entry_type": "bibitem", "text": raw}
        if m.group(2):
            structured["bbl_label"] = norm_ws(m.group(2))
        entry = index.get(key)
        if entry is None and title_map:
            probe = _norm_title(raw)[:120]
            for t, ent in title_map.items():
                if len(t) >= 18 and t[:120] in probe:
                    entry = ent
                    break
        if entry:
            structured = {**entry, "text": raw, **({"bbl_label": structured["bbl_label"]}
                                                   if "bbl_label" in structured else {})}
            structured["gt_source"] = "bib_key" if index.get(key) else "bib_title"
        refs.append({"raw": raw, "key": key, "structured": structured, "extracted": None})
    return refs


def parse_bbl_file(path: str, warnings: list, index=None, title_map=None):
    text, warn = read_tex(path)
    if warn:
        warnings.append(warn)
        return []
    return _bibitem_refs(text, index, title_map)


def parse_thebibliography(inner: str):
    return _bibitem_refs(inner)


def _all_existing(names, roots, ext):
    out = []
    for name in names:
        clean = re.sub(r"\.bib$|\.bbl$", "", name.strip())
        for base in roots:
            if not base:
                continue
            cand = os.path.normpath(os.path.join(base, clean + ext))
            if os.path.isfile(cand) and cand not in out:
                out.append(cand)
                break
    return out


def _first_existing(names, roots, ext):
    got = _all_existing(names, roots, ext)
    return got[0] if got else None


def _any_in_roots(roots, ext, skip_prefix=("sample", "sample-", "references_sample")):
    for base in roots:
        if not base or not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for fn in names:
            if fn.lower().endswith(ext) and not fn.lower().startswith(skip_prefix):
                return os.path.join(base, fn)
    return None


def collect_references(body: str, preamble: str, roots, warnings: list, holders: dict,
                       main_stem=None):
    """Bibliography resolution, ordered by what a reader of the *published* paper actually saw.

    ``.bbl`` is preferred over ``.bib``. Evidence from the pilot: ``2403.11085/main.bib`` still holds
    the ECCV template's six dummy entries (``Anonymous24`` "The frobnicatable foo filter",
    ``Alpher02-05`` "Frobnication") mixed in with the real 53, while ``main.bbl`` contains exactly the
    41 references the paper printed. Measuring refext against template junk would have looked like a
    model failure instead of a source-artefact.
    """
    m = re.search(r"\\begin\s*\{thebibliography\}(.*?)\\end\s*\{thebibliography\}", body, re.S)
    if m:
        refs = parse_thebibliography(m.group(1))
        if refs:
            holders["env"] = True
            return refs
        warnings.append("thebibliography environment contained no \\bibitem")

    names = []
    joined = body + "\n" + preamble
    for bm in re.finditer(r"\\bibliography\s*\{", joined):
        txt, _ = match_braces(joined, bm.end() - 1)
        names += [n.strip() for n in txt.split(",") if n.strip()]

    # The compiled .bbl is normally named after the *main tex file*, not after the declared .bib
    # (2211.14865 declares \bibliography{anthology,custom} but ships acl2023.bbl), so a name miss must
    # not abandon the .bbl search -- falling through to the .bib there yields the whole ACL Anthology.
    bbl = _first_existing(names, roots, ".bbl")
    if bbl is None and main_stem:
        bbl = _first_existing([main_stem], roots, ".bbl")
    if bbl is None:
        bbl = _any_in_roots(roots, ".bbl")
    bibs = _all_existing(names, roots, ".bib")
    if not bibs:
        one = _any_in_roots(roots, ".bib")
        bibs = [one] if one else []

    if bbl:
        holders["bbl"] = bbl
        index = {}
        for p in bibs:
            index.update(bib_index(p, warnings))
        title_map = {_norm_title(v.get("title", "")): v for v in index.values() if v.get("title")}
        refs = parse_bbl_file(bbl, warnings, index, title_map)
        if refs:
            holders["bib"] = bibs
            return refs
        warnings.append(f"{os.path.basename(bbl)} had no \\bibitem entries")
    if bibs:
        holders["bib"] = bibs
        refs = refs_from_bib(bibs, warnings, cited_key_set(body))
        if refs:
            return refs
        warnings.append(f"{os.path.basename(bibs[0])} parsed to zero entries")
    if names:
        warnings.append(f"bibliography declared ({','.join(sorted(set(names)))}) "
                        f"but no .bib/.bbl resolved in the source package")
    return []


# ------------------------------------------------------------------ block scanner

def collect_label_kinds(body: str):
    """Pass 1: which environment owns which ``\\label``, so pass 2 can classify ``\\ref`` targets."""
    kinds = {}
    pos = 0
    while True:
        m = _ENV_BEGIN_RE.search(body, pos)
        if not m:
            break
        env, start = m.group(1), m.end()
        end, after = find_env_end(body, start, env)
        inner = body[start:end] if end > 0 else body[start:]
        kind = None
        if env in TABLE_ENVS or env == "subtable":
            kind = "table"
        elif env in FIGURE_ENVS or env == "subfigure":
            kind = "figure"
        elif env in MATH_ENVS:
            kind = "equation"
        if kind:
            for lm in _LABEL_RE.finditer(inner):
                kinds.setdefault(lm.group(1).strip(), kind)
        pos = (after if end > 0 else start)
    for lm in _LABEL_RE.finditer(body):
        lab = lm.group(1).strip()
        if lab in kinds:
            continue
        low = lab.lower()
        if low.startswith(("fig", "fig:", "figure", "chart")):
            kinds[lab] = "figure"
        elif low.startswith(("tab", "table", "tbl")):
            kinds[lab] = "table"
        elif low.startswith(("eq", "eqn", "alg", "listing", "sec")):
            kinds[lab] = "equation" if low.startswith(("eq", "eqn")) else "other"
    return kinds


def scan_blocks(text: str, ctx: dict, depth: int = 0):
    """Ordered blocks for one region of the body. Unrecognised environments are recursed into rather
    than dropped, so a ``tcolorbox`` full of prose still contributes its paragraphs."""
    blocks = []
    pos = 0
    n = len(text)
    while pos < n:
        nxt = _next_construct(text, pos, ctx["label_kinds"])
        if nxt is None:
            blocks.extend(paragraphs(text[pos:], ctx["warnings"]))
            break
        kind, start, end, payload = nxt
        if start > pos:
            blocks.extend(paragraphs(text[pos:start], ctx["warnings"]))
        if kind == "env":
            env, inner_start, inner_end, after = payload
            inner = text[inner_start:inner_end]
            if env in SKIP_ENVS:
                pass
            elif env in DIAGRAM_ENVS:
                pass
            elif env in FIGURE_ENVS:
                f = parse_figure(inner, ctx["warnings"])
                if f["label"] or f["caption"]:
                    ctx["figures"].append(f)
                    if f["label"]:
                        ctx["labels"].setdefault(f["label"], f["caption"] or "")
            elif env in TABLE_ENVS:
                t = parse_table(inner, ctx["warnings"], env=env, whole=text[start:end])
                if t["label"] or t["caption"] or t["latex"]:
                    ctx["tables"].append(t)
                    if t["label"]:
                        ctx["labels"].setdefault(t["label"], t["caption"] or "")
            elif env in MATH_ENVS:
                e = parse_math(inner, env, text[start:end])
                ctx["equations"].append(e)
                if e["label"]:
                    ctx["labels"].setdefault(e["label"], e["latex"])
                blocks.append({"type": "equation", "latex": e["latex"], "label": e["label"]})
            elif env in LIST_ENVS:
                blocks.append(parse_list(inner, LIST_ENVS[env], ctx["warnings"]))
            elif depth < 6 and env not in ("verbatim", "lstlisting", "minted"):
                blocks.extend(scan_blocks(inner, ctx, depth + 1))
            else:
                txt, spans = text_with_math(inner, ctx["warnings"])
                if txt:
                    block = {"type": "paragraph", "text": txt}
                    if spans:
                        block["math_spans"] = spans
                    blocks.append(block)
        elif kind == "math":
            e = parse_math(payload, "display", text[start:end])
            ctx["equations"].append(e)
            if e["label"]:
                ctx["labels"].setdefault(e["label"], e["latex"])
            blocks.append({"type": "equation", "latex": e["latex"], "label": e["label"]})
        elif kind == "cite":
            blocks.append({"type": "citation", "keys": payload})
            ctx["n_cites"] += 1
        elif kind == "ref":
            lab, rkind = payload
            if rkind == "figure":
                blocks.append({"type": "figure_ref", "label": lab})
            elif rkind == "table":
                blocks.append({"type": "table_ref", "label": lab})
        elif kind == "noop":
            ctx["warnings"].append(f"unclosed environment: {payload}")
        elif kind == "section":
            break
        pos = max(end, pos + 1)
    return blocks


def _next_construct(text: str, pos: int, kinds=None):
    """Earliest structure of interest at or after ``pos``: (kind, start, end, payload)."""
    kinds = kinds or {}
    cands = []
    m = _ENV_BEGIN_RE.search(text, pos)
    if m:
        env, istart = m.group(1), m.end()
        iend, after = find_env_end(text, istart, env)
        if iend < 0:
            # An unclosed \begin{...}: skip the tag and keep scanning, so the text that follows is
            # still parsed instead of being swallowed as the body of a truncated environment.
            cands.append(("noop", m.start(), istart, env))
        else:
            cands.append(("env", m.start(), after, (env, istart, iend, after)))
    m = re.compile(r"(?<!\\)\\\[").search(text, pos)
    if m:
        close = re.compile(r"(?<!\\)\\\]").search(text, m.end())
        stop = close.start() if close else len(text)
        cands.append(("math", m.start(), (close.end() if close else len(text)), text[m.end():stop]))
    # Display $$...$$: without this the scan split at the first \begin{array}/\begin{cases}
    # *inside* the math, leaving two fragments whose odd dollar counts could not be extracted.
    m = re.compile(r"(?<!\\)\$\$").search(text, pos)
    if m:
        close = re.compile(r"(?<!\\)\$\$").search(text, m.end())
        stop = close.start() if close else len(text)
        cands.append(("math", m.start(), (close.end() if close else len(text)), text[m.end():stop]))
    m = _CITE_RE.search(text, pos)
    if m:
        keys = _read_cite_args(text, m.end())
        cands.append(("cite", m.start(), _args_end(text, m.end()), keys))
    m = _REF_RE.search(text, pos)
    if m:
        lab = _read_first_arg(text, m.end())
        cands.append(("ref", m.start(), _args_end(text, m.end()), (lab, kinds.get(lab) if lab else None)))
    m = _SECTION_RE.search(text, pos)
    if m:
        cands.append(("section", m.start(), m.end(), None))
    if not cands:
        return None
    cands.sort(key=lambda c: c[1])
    return cands[0]


def _args_end(text: str, pos: int) -> int:
    """Index just past any ``[..]``/``{..}`` argument group following a macro."""
    i = pos
    while i < len(text):
        if text[i] in " \t\n":
            i += 1
            continue
        if text[i] == "[":
            j = text.find("]", i)
            i = (j + 1) if j > 0 else i + 1
            continue
        if text[i] == "{":
            _, after = match_braces(text, i)
            return after
        return i
    return i


def _read_cite_args(text: str, pos: int):
    i = pos
    keys = []
    while i < len(text):
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i >= len(text):
            break
        if text[i] == "[":
            j = text.find("]", i)
            if j < 0:
                break
            i = j + 1
            continue
        if text[i] == "{":
            inner, i = match_braces(text, i)
            for k in re.split(r"[,;]", inner):
                k = k.strip()
                if k:
                    keys.append(k)
            continue
        break
    return keys


def _read_first_arg(text: str, pos: int):
    i = pos
    while i < len(text) and text[i] in " \t\n":
        i += 1
    if i < len(text) and text[i] == "[":
        j = text.find("]", i)
        i = (j + 1) if j > 0 else i + 1
    while i < len(text) and text[i] in " \t\n":
        i += 1
    if i < len(text) and text[i] == "{":
        inner, _ = match_braces(text, i)
        return inner.strip()
    return None


def paragraphs(chunk: str, warnings: list = None):
    """Prose between structures, split on blank lines. Comments are already gone.

    Math is lifted out before latex_to_text (v2): otherwise ``$x \\in A$`` becomes ``x ∈ A`` and
    the renderer later emits ``∈`` outside math mode, which pdflatex rejects per character."""
    out = []
    for part in re.split(r"\n\s*\n", chunk):
        txt, spans = text_with_math(part, warnings)
        if txt and re.search(r"[A-Za-z0-9]", txt):
            block = {"type": "paragraph", "text": txt}
            if spans:
                block["math_spans"] = spans
            out.append(block)
    return out


def build_sections(body: str, ctx: dict):
    """Level 1..4 section list; ``level: 0, heading: null`` is the lead-in before the first heading.

    A heading is kept even when its body scans empty, because the heading sequence is one of the
    fields Step E compares — dropping it would hide a real structural difference on one side only.
    """
    sections = []
    cur = {"level": 0, "heading": None, "blocks": []}
    pos = 0
    while True:
        m = _SECTION_RE.search(body, pos)
        if not m:
            break
        name = re.match(r"\\(\w+)", m.group(0)).group(1)
        inner, after = match_braces(body, m.end() - 1)
        cur["blocks"].extend(scan_blocks(body[pos:m.start()], ctx))
        if cur["blocks"] or cur["heading"] is not None:
            sections.append(cur)
        heading_src, heading_spans = extract_math_spans(inner, ctx["warnings"])
        cur = {"level": _SECTION_LEVEL.get(name, 1), "heading": clean_heading(heading_src),
               "blocks": []}
        if heading_spans:
            cur["math_spans"] = heading_spans
        pos = after
    cur["blocks"].extend(scan_blocks(body[pos:], ctx))
    if cur["blocks"] or cur["heading"] is not None or not sections:
        sections.append(cur)
    return sections


# ------------------------------------------------------------------ main entry

def _detect_paper_id(input_dir: str):
    name = os.path.basename(os.path.normpath(input_dir))
    if _TEX_ID_RE.match(name):
        return name
    parent = os.path.basename(os.path.dirname(os.path.normpath(input_dir)))
    if _TEX_ID_RE.match(parent):
        return parent
    return name


def extract_sir(input_path: str, source_format: str = "original", papers_root: str = None,
                include_roots=None):
    """Parse one (paper, format) into a SIR dict. Never raises."""
    warnings, figures, tables, equations = [], [], [], []
    input_path = os.path.abspath(input_path)
    sir = {"schema_version": SCHEMA_VERSION, "paper_id": None, "source_format": source_format,
           "extracted_at": datetime.now(timezone.utc).isoformat(), "source": {},
           "metadata": {"title": "", "authors": [], "abstract": "", "keywords": []},
           "sections": [], "figures": figures, "tables": tables, "equations": equations,
           "references": [], "macros": [], "labels": {}, "counts": {}, "parse_warnings": warnings}
    try:
        if os.path.isdir(input_path):
            paper_dir = input_path
            if source_format in ("ieee", "acm", "lncs"):
                body_tex = os.path.join(paper_dir, "_mft_body.tex")
                wrapper = os.path.join(paper_dir, "wrapper.tex")
                main = wrapper if os.path.isfile(wrapper) else (
                    body_tex if os.path.isfile(body_tex) else find_main_tex(paper_dir, warnings))
            else:
                main = find_main_tex(paper_dir, warnings)
        else:
            main = input_path
            paper_dir = os.path.dirname(input_path)
        if not main:
            warnings.append("no main .tex could be located")
            sir["paper_id"] = _detect_paper_id(paper_dir)
            sir["counts"] = _counts(sir)
            return sir

        roots = [os.path.dirname(main)]
        roots.append(paper_dir)
        for r in (include_roots or []):
            roots.append(os.path.abspath(r))
        pid = _detect_paper_id(paper_dir)
        roots_seen = {os.path.normpath(r) for r in roots}
        for pr in (papers_root if isinstance(papers_root, (list, tuple)) else [papers_root]):
            if not pr:
                continue
            alt = os.path.join(os.path.abspath(pr), pid)
            if not os.path.isdir(alt):
                continue
            if os.path.normpath(alt) not in roots_seen:
                roots.append(alt)
                roots_seen.add(os.path.normpath(alt))
            # E1 showed 447 pairs where the recompiled side lost includes the original side resolved.
            # Cause: relative \input paths. When the source's main file lives in a subdirectory
            # (files/experiments.tex), its \input targets are relative to that subdirectory, and
            # _mft_body.tex inherits those paths but not that location. Adding the original main file's
            # own directory makes both sides see the same tree.
            sub = os.path.dirname(find_main_tex(alt, []))
            if sub and os.path.isdir(sub) and os.path.normpath(sub) not in roots_seen:
                roots.append(sub)
                roots_seen.add(os.path.normpath(sub))
            break
        roots = [r for r in dict.fromkeys(os.path.normpath(x) for x in roots) if os.path.isdir(r)]

        raw, warn = read_tex(main)
        if warn or raw is None:
            warnings.append(warn or "empty main file")
            sir["paper_id"] = pid
            sir["counts"] = _counts(sir)
            return sir
        if len(raw) > MAX_TEXT_BYTES:
            warnings.append(f"main file truncated at {MAX_TEXT_BYTES} bytes")
            raw = raw[:MAX_TEXT_BYTES]

        stripped = strip_comments(raw)
        full = inline_includes(stripped, os.path.dirname(main), roots, warnings)

        m = re.search(r"\\begin\s*\{document\}", full)
        if m:
            preamble = full[:m.start()]
            rest = full[m.end():]
            e = re.search(r"\\end\s*\{document\}", rest)
            body = rest[:e.start()] if e else rest
            if not e:
                warnings.append("no \\end{document}; parsed to end of file")
        else:
            preamble, body = "", full
            warnings.append("no \\begin{document} found")

        # References are collected while \bibliography{...} is still present: the noise-command
        # sweep below removes it, and re-deriving the bib name afterwards would lose it.
        holders = {}
        bib_search = roots + [os.path.dirname(main)]
        refs = collect_references(body, preamble, bib_search, warnings, holders,
                                  main_stem=os.path.splitext(os.path.basename(main))[0])

        for pat in NOISE_COMMANDS:
            body = re.sub(pat, " ", body)

        # The abstract and thebibliography are consumed as metadata/references, so removing them
        # before the section scan stops reference text posing as body prose.
        scan_body = re.sub(r"\\begin\s*\{thebibliography\}.*?\\end\s*\{thebibliography\}", " ",
                           body, flags=re.S)
        scan_body = re.sub(r"\\begin\s*\{abstract\}.*?\\end\s*\{abstract\}", " ",
                           scan_body, flags=re.S)

        md = extract_metadata(preamble, body, warnings)
        sir["macros"] = collect_macro_definitions(preamble)
        ctx = {"warnings": warnings, "figures": figures, "tables": tables,
               "equations": equations, "labels": {}, "n_cites": 0,
               "label_kinds": collect_label_kinds(scan_body)}
        sir["sections"] = build_sections(scan_body, ctx)
        sir["labels"] = ctx["labels"]
        sir["references"] = refs
        sir["metadata"] = md
        sir["paper_id"] = pid
        def _rel(p):
            return p.replace("\\", "/") if p else None

        bib_src = ("thebibliography" if holders.get("env") else
                   "bbl" if holders.get("bbl") else "bib" if holders.get("bib") else
                   "none" if not refs else "bib")
        bibs_used = holders.get("bib") or []
        sir["source"] = {
            "tex_file": os.path.relpath(main, paper_dir).replace("\\", "/") if paper_dir else os.path.basename(main),
            "root": paper_dir.replace("\\", "/"),
            "include_roots": [r.replace("\\", "/") for r in roots],
            "bib_file": _rel(bibs_used[0]) if bibs_used else None,
            "bib_files": [_rel(b) for b in bibs_used],
            "bbl_file": _rel(holders.get("bbl")),
            "bib_source": bib_src,
            "bytes_read": len(raw.encode("utf-8", "ignore")),
        }
        sir["counts"] = _counts(sir)
        if not sir["sections"]:
            warnings.append("no sections parsed")
    except Exception as e:  # the never-raises contract: a batch must survive one bad paper
        warnings.append(f"unexpected {type(e).__name__}: {e}")
        try:
            sir["paper_id"] = sir["paper_id"] or _detect_paper_id(input_path)
        except Exception:
            pass
        sir["counts"] = _counts(sir)
    return sir


def _counts(sir):
    paras = sum(1 for s in sir["sections"] for b in s["blocks"] if b["type"] == "paragraph")
    cites = sum(len(b["keys"]) for s in sir["sections"] for b in s["blocks"] if b["type"] == "citation")
    return {"sections": len(sir["sections"]), "paragraphs": paras, "figures": len(sir["figures"]),
            "tables": len(sir["tables"]), "equations": len(sir["equations"]),
            "references": len(sir["references"]), "citations": cites}


# ------------------------------------------------------------------ comparison (Step E)

_COMPARE_NOISE = (
    (r"\\(?:xspace|,|;|:|!|quad|qquad|hspace\s*\{[^{}]*\}|hfill|vspace\s*\{[^{}]*\})\s*", " "),
    (r"[{},]", " "),
)


def normalize_for_compare(value: str) -> str:
    """The one normalization both sides go through before equality is tested. Kept intentionally
    small: whitespace, comments and spacing macros only, so it cannot paper over a real difference."""
    if value is None:
        return ""
    s = strip_comments(str(value))
    for pat, rep in _COMPARE_NOISE:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def _section_paras(sir):
    out = []
    for s in sir.get("sections", []):
        txt = " ".join(b["text"] for b in s.get("blocks", []) if b.get("type") == "paragraph")
        out.append(normalize_for_compare(txt))
    return out


def compare_sirs(a: dict, b: dict, id_label: str = "") -> dict:
    """Field-by-field source vs recompiled comparison. Exact string equality after normalization."""
    fields = []

    def add(name, va, vb):
        na, nb = normalize_for_compare(va), normalize_for_compare(vb)
        fields.append({"field": name, "match": na == nb,
                       "source_len": len(na), "target_len": len(nb)})

    add("metadata.title", (a.get("metadata") or {}).get("title"),
        (b.get("metadata") or {}).get("title"))
    add("metadata.abstract", (a.get("metadata") or {}).get("abstract"),
        (b.get("metadata") or {}).get("abstract"))
    ha = [normalize_for_compare(s.get("heading") or "") for s in a.get("sections", [])]
    hb = [normalize_for_compare(s.get("heading") or "") for s in b.get("sections", [])]
    add("sections.headings", " | ".join(ha), " | ".join(hb))
    pa, pb = _section_paras(a), _section_paras(b)
    for i in range(max(len(pa), len(pb))):
        add(f"sections[{i}].paragraphs", pa[i] if i < len(pa) else "\x00absent",
            pb[i] if i < len(pb) else "\x00absent")
    fa = " | ".join(normalize_for_compare(f.get("caption") or "") for f in a.get("figures", []))
    fb = " | ".join(normalize_for_compare(f.get("caption") or "") for f in b.get("figures", []))
    add("figures.captions", fa, fb)
    ta = " | ".join(normalize_for_compare(t.get("caption") or "") for t in a.get("tables", []))
    tb = " | ".join(normalize_for_compare(t.get("caption") or "") for t in b.get("tables", []))
    add("tables.captions", ta, tb)
    matched = sum(1 for f in fields if f["match"])
    total = len(fields)
    mismatched = [f["field"] for f in fields if not f["match"]]
    return {"label": id_label, "compared": total, "matched": matched,
            "score": (matched / total) if total else 1.0,
            "mismatched_fields": mismatched, "fields": fields}


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="LaTeX source -> SIR JSON (Session 6)")
    ap.add_argument("--input", required=True, help="paper directory or .tex file")
    ap.add_argument("--output", required=True, help="SIR JSON path")
    ap.add_argument("--source-format", default="original", choices=FORMATS)
    ap.add_argument("--papers-root", default=None,
                    help="root holding <paper_id>/ source trees; used as the include/bib fallback")
    ap.add_argument("--include-root", action="append", default=[],
                    help="extra fallback root for \\input/.bib resolution (repeatable)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    sir = extract_sir(a.input, a.source_format, a.papers_root, a.include_root)
    outdir = os.path.dirname(os.path.abspath(a.output))
    os.makedirs(outdir, exist_ok=True)
    with open(a.output, "w", encoding="utf-8") as fh:
        json.dump(sir, fh, ensure_ascii=False, indent=1)
    if not a.quiet:
        print(f"{sir['paper_id']} {sir['source_format']}: title={bool(sir['metadata']['title'])} "
              f"sections={len(sir['sections'])} refs={len(sir['references'])} "
              f"warnings={len(sir['parse_warnings'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
