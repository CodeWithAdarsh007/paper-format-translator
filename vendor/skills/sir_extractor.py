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

# 2.1: SIR gained "external_files" and the "listing" / "includepdf" block types (Session 14B).
# Old 2.0 SIRs still render -- the renderer reads the key with .get() and defaults to empty -- but
# they carry no listings, so a corpus re-extraction is what would make them show any.
SCHEMA_VERSION = "2.1"
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
# The subset that spans the page rather than one column. IEEEtran is two-column, so the renderer
# typesets these at \textwidth and everything else at \columnwidth (Session 13, Step A2).
# wrapfigure is deliberately not one of them: it sits *inside* the text flow of a single column.
SPANNING_FLOAT_ENVS = {"figure*", "sidewaysfigure", "table*"}
# Environments whose content *is* the tabular: they carry \begin/\end themselves and must be
# captured whole. Before Session 9.6 only the float names were listed, so a bare \begin{tabular}
# was recursed into as prose ("lrr Model EN-DE FLOPs ..." as one paragraph) and a longtable was
# stored without its \begin, printing its cells as text.
TABULAR_ENVS = {"tabular", "tabular*", "tabularx", "tabulary", "tabu", "supertabular",
                "longtable", "tblr", "longtblr"}
TABLE_ENVS = {"table", "table*", "sidewaystable"} | TABULAR_ENVS
# Float bodies that are not a tabular but must still be kept whole: a wrapper that changes how the
# tabular is laid out or annotated. Slicing to the inner tabular would drop the wrapper (and, for
# threeparttable, the table notes) and would leave the wide tabular unscaled.
TABLE_WRAPPER_ENVS = {"threeparttable", "adjustbox", "subtable", "minipage", "varwidth",
                      "boxedminipage", "floatbox"}

SKIP_ENVS = {"abstract", "thebibliography"}          # consumed by metadata / references
# Drawing instructions, not prose. Recursing into them turns diagram labels ("π_PC") into Unicode
# text outside math; the templates do not load tikz, so the diagram cannot be reproduced anyway.
DIAGRAM_ENVS = {"tikzcd", "tikzpicture", "pgfpicture", "pspicture", "picture"}
# Environments whose mandatory arguments belong to the *tag*, not to the body.
_ARGFUL_ENVS = {"cjk", "cjk*", "lrbox", "multicols", "filecontents", "filecontents*"}
# ``\appendix`` is *structure*, not noise: it is the only thing that tells the target class to
# letter the sections that follow. Deleting it (Session 13 and earlier) made GPT-3's appendices
# A-H continue the body's numbering, so the reader saw "XVI. ADDITIONAL SAMPLES FROM GPT-3".
_APPENDIX_RE = re.compile(r"(?<!\\)\\appendix\b")
NOISE_COMMANDS = (
    r"\\maketitle\b", r"\\tableofcontents\b", r"\\bibliographystyle\s*\{[^{}]*\}",
    r"\\bibliography\s*\{[^{}]*\}", r"\\addcontentsline\s*\{[^{}]*\}(?:\s*\{[^{}]*\}){2}",
    r"\\clearpage\b", r"\\newpage\b", r"\\pagebreak\b", r"\\pagenumbering\s*\{[^{}]*\}",
    r"\\thispagestyle\s*\{[^{}]*\}", r"\\markboth\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*\{(?:[^{}]|\{[^{}]*\})*\}",
    r"\\setcounter\s*\{[^{}]*\}\s*\{[^{}]*\}", r"\\hyphenation\s*\{[^{}]*\}",
)
_CITE_RE = re.compile(
    r"\\(?:[A-Za-z]*[Cc]it[A-Za-z]*|apud|apudcn|apudnn|nocite)\*?(?![A-Za-z])"
)
_REF_RE = re.compile(r"\\(?:cpageref|Cref|cref|autoref|nameref|eqref|refrange|vref|pageref|ref)\*?\b")
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

# ---- citation/reference preservation (Session 14A-1, Step D) -----------------------------------
# pylatexenc's own text converter rewrites ``\cite{...}`` to the literal string ``<cit.>``,
# ``\ref``/``\autoref`` to ``<ref>`` and ``\Cref`` to ``<Ref>``. Because ``latex_to_text`` runs on
# captions, list items and headings, those placeholders were stored *in the SIR* and printed into
# the reader's PDF (42 ``<cit.>`` and 32 ``<ref>``/``<Ref>`` across the five audited papers). The
# macros are lifted out before conversion, exactly the way math spans are, and put back as
# canonical LaTeX at render time.
XREF_PLACEHOLDER = "«XREF_{i}»"
XREF_PLACEHOLDER_RE = re.compile(r"«XREF_\d+»")

# ---- footnote positioning (Session 14A-2, Step F) ---------------------------------------------
# ``normalise_leaky_macros`` flattens ``\footnote{text}`` to ``text`` because a note inside a
# tabular body cannot be a real \\footnote (2303.08774's ``AMC 10\\footnotemark``), and inlining is
# the only form that both compiles and keeps the sentence. In *prose* that costs the reader the
# footnote apparatus entirely: GPT-3's zero-shot note landed in the middle of a sentence with no
# separator. In prose the note is therefore wrapped in an ordered pair of markers that survive
# flattening, and ``renderer.emit_footnote_markers`` turns the pair back into \\footnote{...} at
# emit time, at the exact anchor the author chose.
#
# The markers deliberately avoid ``_``: protect_tex() escapes underscores, and a marker split by
# that escape would print literally (the reason «MATH_<i>» needs the doubled-form regex in the
# renderer). ``@`` is not a catcode character the flattener can touch.
FNOTE_OPEN = "«FOOTOPEN@{i}»"
FNOTE_CLOSE = "«FOOTCLOSE@{i}»"
_FNOTE_MACRO_RE = re.compile(r"\\(?:footnote|footnotetext)\*?(?![A-Za-z])")
_XREF_MACRO_RE = re.compile(
    r"\\(?:[A-Za-z]*[Cc]it[A-Za-z]*|apud|apudcn|apudnn|nocite"
    r"|cpageref|Cref|cref|autoref|nameref|eqref|refrange|vref|pageref|ref)\*?(?![A-Za-z])")
# ``\Cref``/``\autoref`` print the type word themselves; the target classes load no cleveref, so
# the word is reconstructed from the label's own prefix when the sentence does not already say it.
_LABEL_PREFIX_WORD = (("tab", "Table"), ("fig", "Figure"), ("sec", "Section"),
                      ("subsec", "Section"), ("app", "Appendix"), ("eqn", "Equation"),
                      ("eq", "Equation"), ("alg", "Algorithm"), ("thm", "Theorem"),
                      ("listing", "Listing"), ("lst", "Listing"))
# Label namespaces are compared by *token*, not by whole-prefix equality: real papers write
# ``app-sec:``, ``app-subsec:`` and ``subsec:`` (2402.12424 uses all three), and an exact match on
# "tab"/"fig"/"sec" silently returned no type word for every one of them. Tokens are the parts of
# the text before the first ``:`` split on ``-``/``_``.
_LABEL_TOKEN_WORD = (
    ({"app", "apps", "appendix", "appendices"}, "Appendix"),
    ({"tab", "tabs", "table", "tables", "tbl"}, "Table"),
    ({"fig", "figs", "figure", "figures"}, "Figure"),
    ({"eq", "eqs", "eqn", "eqnarray", "equation", "equations"}, "Equation"),
    ({"alg", "algs", "algorithm", "algorithms"}, "Algorithm"),
    ({"lst", "listing", "listings", "code", "codesnippet"}, "Listing"),
    ({"thm", "thms", "theorem", "theorems"}, "Theorem"),
    ({"sec", "secs", "section", "sections", "subsec", "subsection", "subsections",
      "subsubsec", "subsubsection", "s", "ss", "sss", "chapter", "part"}, "Section"),
)
# The tokens that make a label a *section* label rather than a float label. Used to bind an
# orphan \label to the section it sits in.
_SECTION_TOKENS = ("sec", "secs", "section", "sections", "subsec", "subsection",
                   "subsections", "subsubsec", "subsubsection", "subsubsections",
                   "s", "ss", "sss", "chapter", "part")


_TYPE_WORD_BEFORE_RE = re.compile(
    r"(?:Fig(?:ure)?\.?|Tab(?:le)?\.?|Sec(?:tion)?\.?|Appendi[xc]|Eq(?:uation)?\.?|"
    r"List(?:ing)?\.?|Th(?:m|m|eorem)|Algor(?:ithm)?)[~ ]*$", re.I)

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
# that goes through text_with_math().
#
# Session 13: the pattern is deliberately a copy of renderer.BBOX_ARTIFACT_RE rather than an import.
# The two ends have to agree, and test_renderer.py::test_scrubbers_agree_at_both_ends asserts that
# they do; importing was not an option because the test suite loads this file by path while
# `renderer` may already be bound to the app's vendored copy. The old local version demanded *two
# or more* quadruples, so a single "text[[78, 726, 489, 905]]" survived it, and it only ever ran on
# the way in -- author names, keywords, reference strings and preserved macros never passed through
# it at all. renderer.py now scrubs on the way out too, at the one choke point every text field
# reaches, which is the half that actually closes the class.
_NUM_RE = r"-?\d+(?:\.\d+)?"
_QUAD_RE = r"\[\s*" + _NUM_RE + r"(?:\s*,\s*" + _NUM_RE + r"){3}\s*\]"
_DUMP_RE = (r"(?:\\?text\s*)?\[\s*" + _QUAD_RE + r"(?:\s*,\s*" + _QUAD_RE + r")*\s*\]"
            r"(?:\s*\})?")
_BBOX_ARTIFACT_RE = re.compile(r"\\todo\s*(?:\[[^\]]*\])?\s*\{\s*" + _DUMP_RE + r"\s*\}"
                               + r"|" + _DUMP_RE)


def scrub_layout_artifacts(s: str) -> str:
    """Drop bounding-box lists that leaked out of a layout tool into prose."""
    if not s or "[[" not in s:
        return s
    return norm_ws(_BBOX_ARTIFACT_RE.sub(" ", s))


def _read_xref_args(text: str, pos: int):
    """``(optional_args, brace_args, index_after_last_argument)`` for a macro's argument group.

    The returned index never includes trailing whitespace: dropping a macro must not also drop the
    space that separated it from the next word (that is what glued ``.../evals`` to ``here``).
    """
    opts, braces = [], []
    i, consumed = pos, pos
    while i < len(text):
        j = _skip_ws(text, i)
        if j >= len(text):
            break
        if text[j] == "[":
            k = text.find("]", j)
            if k < 0:
                break
            opts.append(text[j + 1:k].strip())
            i = consumed = k + 1
            continue
        if text[j] == "{":
            inner, i = match_braces(text, j)
            braces.append(inner)
            consumed = i
            continue
        break
    return opts, braces, consumed


def _label_tokens(label: str) -> list:
    """The namespace tokens of a label: ``app-sec:significance`` -> ``["app", "sec"]``."""
    head = (label or "").split(":")[0].strip().lower()
    return [t for t in re.split(r"[-_\s]+", head) if t]


def _label_type_word(label: str) -> str:
    """The word ``\\Cref``/``\\autoref`` would have printed, from the label's own namespace."""
    toks = set(_label_tokens(label))
    for words, word in _LABEL_TOKEN_WORD:
        if toks & words:
            return word
    for prefix, word in _LABEL_PREFIX_WORD:
        if (label or "").split(":")[0].strip().lower() == prefix:
            return word
    return ""


def _is_section_label(label: str) -> bool:
    """True when the label's namespace says it points at a sectioning command."""
    toks = _label_tokens(label)
    return any(t in _SECTION_TOKENS for t in toks)


# ``\Cref{fig:a,fig:b}`` prints "Figures 1 and 2": one plural word, then the numbers joined in
# prose. Mixed families print one word each ("Table 1, Figure 2"), which is what Session 14A-1
# pinned in ``test_cref_resolved`` and is kept.
_LABEL_WORD_PLURAL = {"Appendix": "Appendices", "Figure": "Figures", "Table": "Tables",
                      "Section": "Sections", "Equation": "Equations", "Listing": "Listings",
                      "Algorithm": "Algorithms", "Theorem": "Theorems"}
# Macros that print the type word themselves. The target classes load neither cleveref nor
# (in general) hyperref's autoref names, so the word has to be reconstructed at extraction time.
_WORDY_REF_MACROS = ("cref", "autoref", "vref", "nameref")


def _ref_parts(labs: list, macro: str, before: str = "", kind_of=None) -> list:
    """The (word, label) pairs a reference renders as -- one entry per target.

    ``macro`` is the name that was used (``ref``, ``Cref``, ``cref``, ``autoref``, ``pageref``…):
    only the wordy family asks for a keyword, and the keyword is skipped when the sentence already
    says it ("as shown in Table \\Cref{tab:x}" must not become "Table Table 3").
    """
    if not labs:
        return []
    wants_word = (macro or "ref").lower() in _WORDY_REF_MACROS
    said = bool(_TYPE_WORD_BEFORE_RE.search(before or ""))
    parts = []
    for lab in labs:
        word = ""
        if wants_word and not said:
            word = _label_type_word(lab)
            if not word and kind_of:
                # A label with an unrecognised namespace still has an owning environment from
                # collect_label_kinds(); use that rather than dropping the word.
                word = {"figure": "Figure", "table": "Table",
                        "equation": "Equation"}.get(kind_of(lab) or "", "")
        parts.append({"word": word, "label": lab})
    return parts


def ref_join(parts: list) -> str:
    """Join reference parts the way the source macro would have printed them.

    One target is ``Table~\\ref{tab:x}``. Several of the same family are
    ``Figures~\\ref{fig:a} and~\\ref{fig:b}`` -- one plural word, then the numbers in prose, which
    is what cleveref prints. Mixed families keep a word each ("Table~\\ref{tab:x},
    Figure~\\ref{fig:y}"), the form Session 14A-1 pinned in ``test_cref_resolved``. The renderer
    carries a copy of this function for the emit-time trim; ``test_ref_join_agrees_at_both_ends``
    fails if the two ever disagree.
    """
    if not parts:
        return ""
    words = [(p.get("word") or "") for p in parts]
    labs = [(p.get("label") or "") for p in parts]
    if len(parts) == 1:
        return ("%s~\\ref{%s}" % (words[0], labs[0])) if words[0] else "\\ref{%s}" % labs[0]
    if len(set(words)) == 1 and words[0]:
        head = _LABEL_WORD_PLURAL.get(words[0], words[0] + "s")
        out = ["%s~\\ref{%s}" % (head, labs[0])]
        rest = labs[1:]
        for i, lab in enumerate(rest):
            out.append((" and~\\ref{%s}" if i == len(rest) - 1 else ", ~\\ref{%s}") % lab)
        return "".join(out)
    return ", ".join(("%s~\\ref{%s}" % (w, lab)) if w else ("\\ref{%s}" % lab)
                     for w, lab in zip(words, labs))


def _ref_latex(labs: list, macro: str, before: str = "", kind_of=None) -> str:
    """Canonical ``\\ref`` LaTeX for *labs*, with the type word the source macro implied."""
    return ref_join(_ref_parts(labs, macro, before, kind_of))


def extract_xref_spans(text: str, spans: list, warnings: list = None) -> str:
    """Lift ``\\cite``/``\\ref``-family macros out of raw LaTeX as ``«XREF_i»``.

    Each recorded span carries ``xref`` (canonical LaTeX to emit verbatim) instead of
    ``raw``/``delims``, which is what tells ``renderer.math_delimited`` not to wrap it in ``$``.
    ``\\cite`` keeps its optional notes; the style variants (``\\citep``/``\\citet``/``\\citealp``
    ...) all become ``\\cite``, because none of the three target classes loads natbib, so the
    numbered style is the only one that compiles. An empty argument is reported and dropped
    rather than printed as a placeholder.
    """
    if not text or not _XREF_MACRO_RE.search(text):
        return text
    warn = warnings if warnings is not None else []
    out, i = [], 0
    while True:
        m = _XREF_MACRO_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        name = m.group(0).lstrip("\\")
        opts, braces, after = _read_xref_args(text, m.end())
        is_cite = "cite" in name.lower() or name.lower() in ("apud", "apudcn", "apudnn")
        latex = None
        if is_cite:
            keys = [k.strip() for k in re.split(r"[,;]", braces[-1] if braces else "") if k.strip()]
            if keys:
                arg = "".join(f"[{o}]" for o in opts if o)
                latex = "\\cite%s{%s}" % (arg, ",".join(keys))
            else:
                warn.append("empty_citation_argument")
        else:
            labs = [k.strip() for k in re.split(r"[,;]", braces[-1] if braces else "") if k.strip()]
            if labs:
                latex = _ref_latex(labs, name, text[:m.start()])
            else:
                warn.append("empty_ref_argument")
        out.append(text[i:m.start()])
        if latex is not None:
            idx = len(spans)
            spans.append({"index": idx, "xref": latex, "kind": "cite" if is_cite else "ref"})
            out.append(XREF_PLACEHOLDER.format(i=idx))
        i = max(after, m.end())
    return "".join(out)


# ---- directive / bookkeeping macro leaks (Session 14A-1, Step C) --------------------------------
# ``latex_to_text`` has no rule for these macros, so pylatexenc printed their *bookkeeping*
# instead of their content and the result reached the reader's PDF. Measured on the audited
# papers: ``\hyperref[systemcard]{system card}`` -> ``[systemcard]system card``,
# ``\footnotetext[\thefootnote]{For AMC 10...}`` -> ``[]For AMC 10...``,
# ``\includepdf[pages=-]{assets/Card.pdf}`` -> ``[pages=-]assets/Card.pdf``,
# ``\lstinputlisting[style=data,...]{data/p.md}`` -> ``[style=data,...]data/p.md``,
# ``\let\@fnsymbol\@arabic`` -> ``fnsymbolarabic``.
# ``\href{newser.com}{newser.com}`` -> ``newser.comnewser.com``: the converter prints the link
# *target* and the link *text* back to back with no separator, which is the duplicate-URL class.
_LEAKY_MACRO_RE = re.compile(
    r"\\(?:hyperref|href|url|footnote|footnotetext|footnotemark|footref|footmarking|includepdf"
    r"|lstinputlisting|let|global|UTF8gbsn|CJKfamily|CJKdate|CJKdigits|fnsymbol|label"
    r"|stepcounter|setcounter|addtocounter|addtocounters|newcounter|counterwithin"
    r"|setcounterwithin|value)\*?(?![A-Za-z])")
_CJK_ENV_RE = re.compile(
    r"\\(?:begin|end)\s*\{CJK\*?\}\s*(?:\{[^{}]*\}\s*\{[^{}]*\})?")
_CONTROL_WORD_RE = re.compile(r"\\[@A-Za-z]+\*?")

# Emit-time backstop, kept as a copy of ``renderer.DIRECTIVE_LEAK_PATTERNS`` for the same reason
# the bbox scrub is: the test suite loads this file by path while ``renderer`` may already be bound
# to the app's vendored copy, and importing across the two broke collection.
# ``test_scrubbers_agree_at_both_ends`` asserts the two lists stay identical.
DIRECTIVE_LEAK_PATTERNS = (
    r"\[pages?=[^\]]*\]\s*[^\s{,}]*\.pdf",   # \includepdf[pages=-]{file.pdf}
    r"\[pages?=[^\]]*\]",
    r"\[systemcard\]",
    r"\[style=[^\]]*\]",
    r"\[language=[^\]]*\]",
    r"\blst:[A-Za-z0-9_:.\-]+",
    r"\bsec:[A-Za-z0-9_:.\-]+",
    r"\bUTF8gbsn\b",
    r"\bfnsymbolarabic\b",
    r"\b\d+\s+footnote\b",
    r"\bfootnote\b(?=\s*\[)",
)
_DIRECTIVE_LEAK_RES = tuple(re.compile(p) for p in DIRECTIVE_LEAK_PATTERNS)


# ---- external file macros: \lstinputlisting and \includepdf (Session 14B, Steps B and C) -------
# Both name a file in the source tree that has to travel with the rebuilt document. 2501.12948
# writes eight of them, each alone on its own line of appendix.tex; 2303.08774 appends the 60-page
# GPT-4 System Card the same way. normalise_leaky_macros() below still handles a *mention* inside
# a sentence, but a call that occupies a block of its own is lifted here into a block, which is the
# only level that can create one: by the time a string reaches the flattener the code no longer
# knows whether it was a whole paragraph or a clause.
_FILE_MACRO_RE = re.compile(r"\\(lstinputlisting|includepdf)\*?(?![A-Za-z])")
# Option keys worth keeping as structured fields. Everything else in the [..] group is replayed
# verbatim, so an unusual key is dropped to the renderer rather than silently lost.
_LISTING_OPT_KEYS = ("caption", "label", "style", "language", "name")


def _opt_value(opts, key):
    """The value of ``key=value`` inside a listings/pdfpages option list, brace-aware."""
    for o in opts or []:
        m = re.search(r"(?:^|[,{])\s*" + re.escape(key) + r"\s*=\s*", o)
        if not m:
            continue
        rest = o[m.end():]
        if rest.startswith("{"):
            val, _end = match_braces(rest, 0)
            return val.strip()
        return rest.split(",", 1)[0].strip()
    return None


def parse_file_macro(text: str, m):
    """``(block, index_past_call)`` for the ``\\lstinputlisting`` / ``\\includepdf`` at *m*."""
    name = m.group(1)
    opts, braces, after = _read_xref_args(text, m.end())
    path = (braces[-1] if braces else "").strip()
    if not path:
        return None, after
    if name == "includepdf":
        block = {"type": "includepdf", "path": path,
                 "options": (opts[0].strip() if opts else "pages=-") or "pages=-"}
    else:
        block = {"type": "listing", "path": path}
        for key in _LISTING_OPT_KEYS:
            val = _opt_value(opts, key)
            if val:
                block[key] = val
        rest = [kv.strip() for kv in re.split(r",", (opts[0] if opts else ""))
                if kv.strip() and not any(
                    re.match(r"^\s*" + k + r"\s*=", kv) for k in _LISTING_OPT_KEYS)]
        if rest:
            block["extra_options"] = ",".join(rest)
    return block, after


def strip_directive_leaks(s):
    """Remove leaked directives and macro names before they are stored in the SIR."""
    if not s:
        return s
    s = str(s)
    if "[" not in s and "sec:" not in s and "lst:" not in s and "footnote" not in s \
            and "UTF8gbsn" not in s and "fnsymbolarabic" not in s:
        return s
    for rx in _DIRECTIVE_LEAK_RES:
        s = rx.sub(" ", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip() if "\n" not in s else re.sub(r"[ \t]{2,}", " ", s)


def normalise_leaky_macros(text: str, warnings: list = None) -> str:
    """Unwrap the macros whose name (not content) would otherwise be printed.

    A footnote keeps its text inline rather than as a real \\footnote: the target classes put
    these notes inside tabular bodies (2303.08774's ``AMC 10\\footnotemark``), and LaTeX forbids
    \\footnote there, so inlining is the only form that both compiles and preserves the sentence.
    ``\\href`` and ``\\hyperref`` keep the *visible* argument, which is what LaTeX prints, so the
    link target stops being printed next to the link text (``newser.comnewser.com``).
    \\includepdf and \\lstinputlisting are dropped with a warning naming the file — merging them
    is Session 14A-2's scope; the leak is not.
    """
    if not text:
        return text
    text = _CJK_ENV_RE.sub(" ", text)
    if not _LEAKY_MACRO_RE.search(text):
        return text
    warn = warnings if warnings is not None else []
    out, i = [], 0
    while True:
        m = _LEAKY_MACRO_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        name = m.group(0).lstrip("\\").rstrip("*")
        _opts, braces, after = _read_xref_args(text, m.end())
        arg = braces[-1] if braces else None
        out.append(text[i:m.start()])
        if name in ("footnote", "footnotetext", "hyperref", "href", "url"):
            # The kept argument is re-scanned: 2303.08774 hides
            # \footnote{\href{u}{u}} inside a footnote, and without the recursion the inner
            # macro is skipped over and still prints its target twice.
            out.append(normalise_leaky_macros(arg or "", warn))
        elif name in ("footnotemark", "footref", "footmarking"):
            pass
        elif name in ("includepdf", "lstinputlisting"):
            warn.append("%s: %s (the file's contents are not in the converted document)"
                        % ("external PDF page range not merged" if name == "includepdf"
                           else "listing file not inlined", (arg or "").strip()))
        else:                                   # \let, \global, font switches
            j = after
            for _ in range(2 if name in ("let", "global") else 0):
                k = _skip_ws(text, j)
                cw = _CONTROL_WORD_RE.match(text, k)
                if not cw:
                    break
                j = cw.end()
            out.append(" ")
            i = max(j, m.end())
            continue
        i = max(after, m.end())
    return "".join(out)


def extract_footnote_markers(text: str, warnings: list = None):
    """Wrap each prose ``\\footnote{...}`` in an open/close marker pair, keeping its argument raw.

    The argument stays in the running text, so math, citations and links inside a footnote are
    lifted by the normal passes and their placeholders stay index-consistent. Only the outermost
    note is marked: a ``\\footnote`` nested inside another is illegal LaTeX, and leaving the inner
    one to be inlined is the right reading of a source that should not have written it.

    Returns ``(text_with_markers, marked_count)``.
    """
    if not text or not _FNOTE_MACRO_RE.search(text):
        return text, 0
    out, i, n = [], 0, 0
    while True:
        m = _FNOTE_MACRO_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        _opts, braces, after = _read_xref_args(text, m.end())
        arg = braces[-1] if braces else None
        out.append(text[i:m.start()])
        if arg and arg.strip():
            out.append(FNOTE_OPEN.format(i=n) + arg + FNOTE_CLOSE.format(i=n))
            n += 1
        elif not arg:
            (warnings if warnings is not None else []).append("empty_footnote_argument")
        i = max(after, m.end())
    return "".join(out), n


def text_with_math(raw: str, warnings: list = None, footnotes: bool = False, macros=None):
    """Raw LaTeX -> (plain text with placeholders, math spans). The one entry point every
    text-bearing SIR field goes through since v2.

    ``footnotes=True`` is for prose (paragraphs, list items, an unrecognised environment's body):
    it marks real footnotes so the renderer can emit them as ``\\footnote{}``. Captions, the
    abstract and headings leave it off and keep the Session 14A-1 inlined form, which is what made
    2303.08774 compile.

    ``macros`` is the paper-local text-expansion table. It is applied after the math and reference
    spans have been lifted out, which is what keeps ``$M_n(\\R)$`` intact while still turning
    ``\\dsri{}`` into "DeepSeek-R1".
    """
    if footnotes and raw:
        raw, _n = extract_footnote_markers(raw, warnings)
    clean, spans = extract_math_spans(raw or "", warnings)
    clean = extract_xref_spans(clean, spans, warnings)
    clean = expand_text_macros(clean, macros)
    clean = normalise_leaky_macros(clean, warnings)
    return scrub_layout_artifacts(
        norm_ws(strip_directive_leaks(latex_to_text(clean)))), spans


# ---- paper-local macro definitions (schema v2) -----------------------------------------------
# Preserving math ($M_n(\R)$) without the source preamble makes every paper-local macro an
# "Undefined control sequence": 238 of 254 post-fix errors on 2312.00001 were exactly that.
# These are the constructor forms whose meaning is self-contained enough to re-emit; theorem
# declarations, counters and package setup are not, and the rebuilt body never uses them.
_MACRO_CTOR_RE = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator)\*?")
_DEF_RE = re.compile(r"\\def\s*\\([A-Za-z]+)(?![A-Za-z])")
# A colour is referenced by bare name (\cellcolor{ColorHigh}), never with a backslash, so it is not
# a "macro" in the _MACRO_CTOR_RE sense and was silently dropped: 2402.12424 defined ColorLow/Mid/
# High in its preamble and its own \applyGradient macros then failed with
# "! Package xcolor Error: Undefined color 'ColorHigh'" — 108 errors in each format.
_DEFINECOLOR_RE = re.compile(r"\\definecolor\s*\{\s*([^{}]+?)\s*\}\s*"
                             r"\{\s*([^{}]+?)\s*\}\s*\{\s*([^{}]*?)\s*\}")



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
    out = [defs[k] for k in defs]
    # Colours are emitted verbatim (no \providecommand rewrite) and keep their own namespace, so a
    # paper colour called "red" overrides xcolor's shorthand exactly as the source did.
    colours = {}
    for m in _DEFINECOLOR_RE.finditer(text):
        name = m.group(1)
        if name in colours or "@" in name:
            continue
        colours[name] = f"\\definecolor{{{name}}}{{{m.group(2)}}}{{{m.group(3)}}}"
    return out + list(colours.values())


# ---- applying the macro table to text (Session 14B, Step D) ------------------------------------
# collect_macro_definitions() already parses the preamble correctly and the definitions are
# re-emitted into the rebuilt preamble, so any site that survives as *raw* LaTeX renders fine:
# 2501.12948's "Training Costs & \textbf{\dsro{}}" table header was always right. The loss is
# confined to fields that go through latex_to_text(), whose final sweep (see line 313) deletes every
# control word it did not resolve. For a name macro written \dsri{} the argument is empty, so the
# word disappears and the sentence reads "the effectiveness of is highly contingent".
#
# Only zero-argument macros whose body flattens to pure text are expanded here. That restriction is
# what keeps math safe: \R is defined as \mathbb{R}, which flattens to "R", and substituting it
# inside $M_n(\R)$ would silently downgrade the notation. Those stay for the emitted preamble.
_TEXT_EXPANDABLE_RE = re.compile(
    r"\\providecommand\{\\([A-Za-z]+)\}(?:\[(\d+)\](?:\[[^\]]*\])?)?\{(.+)\}\s*$", re.S)
# The body must also be *made of* text, judged on the raw definition rather than its flattened
# form. latex_to_text() deletes every control word it does not resolve, so the body of 2402.12424's
# \opensource -- a wrapper around \includegraphics of a lock icon -- flattens to the literal string
# "<graphics>", contains no backslash, and passed a flattened-only filter. Every \opensource in a
# caption was then replaced by that string. A body that draws, colours or measures is not text.
_TEXT_SAFE_BODY_RE = re.compile(
    r"^(?:[^\\]|\\(?:text(?:sc|bf|it|rm|sf|tt|up|md)|emph|xspace|mbox)\b|\\[,;:!`'])*$")


def build_text_macro_expansions(macro_defs):
    """``{name: plain text}`` for the zero-argument macros whose body is printable words."""
    table = {}
    for d in macro_defs or []:
        m = _TEXT_EXPANDABLE_RE.match(d.strip())
        if not m or m.group(2):
            continue
        if not _TEXT_SAFE_BODY_RE.match(m.group(3)):
            continue
        flat = norm_ws(latex_to_text(m.group(3)))
        if not flat or any(c in flat for c in "\\{}#$&%_<>"):
            continue
        table.setdefault(m.group(1), flat)
    return table


def expand_text_macros(text: str, table) -> str:
    """Replace ``\\name`` and ``\\name{}`` with the table's text, keeping the spaces either side.

    Dropping the surrounding space is what glued ".../evals" to "here" in Session 14A-1, so the
    whitespace a call consumed is put back rather than swallowed: group 2 is the run before an
    optional ``{}``, group 3 the run after it.
    """
    if not text or not table:
        return text

    def repl(m):
        val = table.get(m.group(1))
        if val is None:
            return m.group(0)
        return val + (m.group(2) or "") + (m.group(3) or "")

    return re.sub(r"(?<!\\)\\([A-Za-z]+)(?![A-Za-z])(\s*)(?:\{\s*\}(\s*))?", repl, text)


def clean_heading(s: str, macros=None) -> str:
    """Heading text: numbers/labels/macros out, plain words in."""
    s = _LABEL_RE.sub(" ", s)
    s = re.sub(r"\\(?:cite|ref)\*?(?:\[[^\]]*\])*\s*\{[^{}]*\}", " ", s)
    s = expand_text_macros(s, macros)
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

# Decorative macros whose argument is a symbol *name* ("peach", "blueberries"), not text.
# Keeping the argument is how 2402.12424's \twemoji affiliation markers became fruit words
# appended to the surnames: Yulong Chenblueberries, Lin Mapeach, Yue Zhanglemon.
_AUTHOR_DECOR_RE = re.compile(
    r"\\twemoji\s*\{[^{}]*\}|\\emoji\s*\{[^{}]*\}|\\symbol\s*\{[^{}]*\}"
    r"|\\clubsuit\b|\\heartsuit\b|\\spadesuit\b|\\diamondsuit\b"
    r"|\\color\s*\{[^{}]*\}")
_TEXTCOLOR_UNWRAP_RE = re.compile(
    r"\\textcolor\s*\{[^{}]*\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}")

# One entry in a line of names; a bare comma in "Doe, Jane" fails the >= 2 token test.
_PERSON_ORG_RE = re.compile(r"[@\d&/]")

_AUTHOR_NAME_DROP = (
    r"\\inst\s*\{[^{}]*\}", r"\\orcidlink\s*\{[^{}]*\}",
    r"\\footnotemark\s*(?:\[[^\]]*\])?", r"\\footnote\s*\{(?:[^{}]|\{[^{}]*\})*\}",
    r"\\email\s*\{[^{}]*\}", r"\\affiliation\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}",
    r"\\url\s*\{[^{}]*\}", r"\$?\^[^$]*\$?", r"\\textsuperscript\s*\{[^{}]*\}",
    r"\\texttt\s*\{[^{}]*\}", r"\\hspace\*?\s*\{[^{}]*\}", r"\\hfill\b",
    r"\\qquad\b", r"\\quad\b", r"\\\\", r"\\dagger\b", r"\\thanks",
    r"\\bf\b", r"\\large\b", r"\*",
)


def _author_row_text(raw: str) -> str:
    """Printable text of one ``\\\\``-separated row of an ``\\author`` block."""
    s = _AUTHOR_DECOR_RE.sub(" ", raw)
    s = _TEXTCOLOR_UNWRAP_RE.sub(r"\1", s)
    s = re.sub(r"\\(?:q?quad)\b", ", ", s)
    for pat in _AUTHOR_NAME_DROP:
        s = re.sub(pat, " ", s)
    return _clean_author_field(latex_to_text(s))


def _person_list(text: str):
    """The comma-separated names in *text*, or None when it is not a pure name list.

    Only used for the second and later rows of an ``\\author`` block. Those rows are an
    affiliation/email by convention, so the split needs to be conservative: a department line
    ("Department of X, University of Y") or a placename ("Cambridge, MA") must not qualify.
    """
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        return None
    for p in parts:
        if _ORG_HINT_RE.search(p) or _PERSON_ORG_RE.search(p):
            return None
        toks = p.split()
        if not 2 <= len(toks) <= 4:
            return None
        if not all(t[0].isupper() or (len(t) == 2 and t.endswith(".")) for t in toks):
            return None
    return parts


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
    s = norm_ws(s)
    return re.sub(r"\s+([,.;:])", r"\1", s)


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
        for affpat in (r"\\affiliation\s*\{", r"\\affil\s*\{", r"\\institution\s*\{"):
            am = re.search(affpat, raw)
            if am:
                inner, _ = match_braces(raw, am.end() - 1)
                inn = re.search(r"\\institution\s*\{([^{}]*)\}", inner)
                aff = norm_ws(latex_to_text(inn.group(1) if inn else inner))
                break
        if aff is None and inst_idx is not None and 0 <= inst_idx < len(institutes):
            aff = institutes[inst_idx]
        rows = _author_rows(raw)
        if rows and re.match(r"^\s*\\\\", raw):
            # No name line of its own: this chunk is the group/organisation row that some
            # templates put after a final separator ("\AND \\ OpenAI", "\AND \\ AMD, Inc.").
            # It is an affiliation, never a person.
            group = _author_row_text(rows[0])
            if group:
                if aff is None:
                    aff = group
                if authors and not authors[-1]["affiliation"]:
                    authors[-1]["affiliation"] = aff
            continue
        name_src = rows[0] if rows else raw
        extra_names = []
        if aff is None:
            rest = []
            for r in rows[1:]:
                if re.search(r"\\texttt\s*\{[^{}]*@|\\url\s*\{", r):
                    continue
                txt = _author_row_text(r)
                if not txt:
                    continue
                people = _person_list(txt)
                if people:
                    extra_names.extend(people)
                elif not rest:
                    rest.append(txt)
            if rest:
                aff = rest[0]
        else:
            aff = _clean_author_field(aff)
        name = _author_row_text(name_src)
        people = _person_list(name) if name else None
        if people:
            name, more = people[0], people[1:]
            extra_names = more + extra_names
        if name:
            if (len(rows) <= 1 and aff is None and email is None and authors
                    and _ORG_HINT_RE.search(name)):
                if not authors[-1]["affiliation"]:
                    authors[-1]["affiliation"] = name
                continue
            authors.append({"name": name, "affiliation": aff or None, "email": email or None})
            for extra in extra_names:
                authors.append({"name": extra, "affiliation": aff or None, "email": None})
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


def pdfinfo_fields(text: str):
    r"""``\hypersetup{pdfinfo={Title={..}, Author={..}}}`` -> {field: value}.

    A `pdfpages` wrapper such as 1412.6980 deposits no ``\title``/``\author`` at all: the manuscript
    is an included PDF and its metadata lives here. Those strings are the paper's own, so reading
    them beats rendering "Untitled".
    """
    out = {}
    for m in re.finditer(r"pdfinfo\s*=\s*\{", text):
        inner, _ = match_braces(text, m.end() - 1)
        for fm in re.finditer(r"\\?([A-Z][A-Za-z]*)\s*=\s*\{", inner):
            val, _ = match_braces(inner, fm.end() - 1)
            if val.strip():
                out.setdefault(fm.group(1), val.strip())
    return out


def extract_metadata(preamble: str, body: str, warnings: list, macros=None):
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
    for src in srcs:
        got, macro = find_authors(src)
        if got:
            md["authors"], md["author_macro"] = got, macro
            break
    if not md["title"] or not md["authors"]:
        info = pdfinfo_fields(preamble + "\n" + body)
        if not md["title"] and info.get("Title"):
            md["title"] = norm_ws(latex_to_text(info["Title"]))
            warnings.append("title taken from \\hypersetup pdfinfo (no \\title in the source)")
        if not md["authors"] and info.get("Author"):
            md["authors"] = [{"name": norm_ws(latex_to_text(a)), "affiliation": None,
                              "email": None}
                             for a in split_top_level(info["Author"], r",") if a.strip()]
            warnings.append("authors taken from \\hypersetup pdfinfo (no \\author in the source)")
    if not md["title"]:
        warnings.append("no \\title found")
    if not md["authors"]:
        warnings.append("no \\author found")
    md["affiliations"] = list(dict.fromkeys(
        a["affiliation"] for a in md["authors"] if a.get("affiliation")))
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
        md["abstract"], ab_spans = text_with_math(raw_ab, warnings, macros=macros)
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

# The float's own placement argument. ``scan_blocks`` hands float parsers the text *after*
# \begin{figure} / \begin{table}, so the bracket is the first token of that text; ``(?<!\\)`` keeps
# a display-math \[ from being read as one.
_PLACEMENT_RE = re.compile(r"^\s*(?<!\\)\[([^\]]*)\]")


def split_placement(inner: str):
    """``('[t]', body_without_it)`` for a float environment body.

    Leaving the bracket in the body put a literal ``[t]`` in the rendered document, and it is also
    the only place the source's own placement hint can be read from.
    """
    m = _PLACEMENT_RE.match(inner or "")
    if not m:
        return None, inner or ""
    return (m.group(1).strip() or None), inner[m.end():]


def _drop_caption_label(body: str) -> str:
    """Remove the float's own ``\\caption``/``\\label`` from a verbatim body: the renderer re-emits
    both from ``caption``/``label``, and keeping the source copies printed every caption twice."""
    out, i = [], 0
    while i < len(body):
        m = re.compile(r"\\(caption|label)\s*(?:\[[^\]]*\])?\s*\{").search(body, i)
        if not m:
            out.append(body[i:])
            break
        text, after = match_braces(body, m.end() - 1)
        out.append(body[i:m.start()])
        i = after
    return "".join(out)


def parse_figure(inner: str, warnings: list, macros=None):
    fig = {"label": None, "caption": None, "path": None, "placement": None}
    fig["placement"], inner = split_placement(inner)
    cap = re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", inner)
    if cap:
        text, _ = match_braces(inner, cap.end() - 1)
        lab = _LABEL_RE.search(text)
        if lab:
            fig["label"] = lab.group(1).strip()
            text = _LABEL_RE.sub(" ", text)
        fig["caption"], spans = text_with_math(text, warnings, macros=macros)
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
    # ``\begin{subfigure}{}`` / ``\subfloat`` panels carry their own \label inside the parent float
    # (2501.12948 appendix.tex:682 \label{mmlupro}). Only the parent becomes a SIR figure, so those
    # targets were defined nowhere and the body's "As illustrated in Figure~\ref{mmlupro}" printed a
    # keyword with no number. They are recorded here and emitted inside the parent float, where
    # LaTeX resolves them to the parent's number -- which is the panel's own figure.
    panels = [m.group(1).strip() for m in _LABEL_RE.finditer(inner)
              if m.group(1).strip() and m.group(1).strip() != fig["label"]]
    if panels:
        fig["panel_labels"] = panels
    return fig


def parse_table(inner: str, warnings: list, env: str = None, whole: str = None,
                 macros=None):
    tab = {"label": None, "caption": None, "latex": "", "placement": None}
    # Only a float carries a placement: for a bare tabular the leading bracket is its column
    # alignment argument (\begin{longtable}[c]{ll}), which belongs inside the body.
    if env not in TABULAR_ENVS:
        tab["placement"], inner = split_placement(inner)
    cap = re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", inner)
    if cap:
        text, _ = match_braces(inner, cap.end() - 1)
        lab = _LABEL_RE.search(text)
        if lab:
            tab["label"] = lab.group(1).strip()
            text = _LABEL_RE.sub(" ", text)
        tab["caption"], spans = text_with_math(text, warnings, macros=macros)
        if spans:
            tab["math_spans"] = spans
    if env in TABULAR_ENVS:
        # The environment itself is the table: keep begin/end and every cell byte for byte.
        tab["latex"] = (whole if whole is not None else inner).strip()
    else:
        body = re.search(r"\\begin\s*\{((?:tabular|tabularx|array|nicematrix|bmatrix|IEEEeqnarray"
                         r"|longtable|tabulary|tabu|supertabular|tblr|longtblr)\*?)\}"
                         r"\s*(?:\[[^\]]*\])?\s*\{", inner)
        wrapper = re.search(r"\\begin\s*\{(" + "|".join(sorted(TABLE_WRAPPER_ENVS)) + r")\*?\}",
                            inner) or re.search(r"\\resizebox\s*\{", inner)
        if wrapper:
            # The wrapper is part of the table: a threeparttable's tablenotes is content, and a
            # resizebox/adjustbox is what keeps a wide tabular inside the column. Slicing to the
            # tabular alone dropped both. \caption and \label are removed because the template
            # re-emits them from caption/label, which used to print every caption twice.
            tab["latex"] = _drop_caption_label(inner).strip()
        elif body:
            envname = re.match(r"\\begin\s*\{([^}]*)\}", body.group(0)).group(1)
            end, _ = find_env_end(inner, body.end(), envname)
            tab["latex"] = inner[body.start(): end + len(envname) + 6].strip() if end > 0 \
                else inner[body.start():].strip()
        else:
            tab["latex"] = _drop_caption_label(inner).strip()
            # A table float holding an image is what an image-based-table paper looks like, not a
            # parse failure, so it is not warned about.
            if tab["latex"] and "\\includegraphics" not in inner and "\\pgfimage" not in inner:
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


def parse_list(inner: str, kind: str, warnings: list = None, macros=None):
    """``\\item`` separated entries. Any lead-in text before the first ``\\item`` is dropped, and
    ``description`` entries keep only their term+body after the optional label argument.

    v2: ``math_spans`` is a list parallel to ``items`` (one span list per item, ``[]`` for an item
    without math), present only when at least one item carries math.
    """
    parts = re.split(r"\\item\b(?:\[[^\]]*\])?", inner)
    items, item_spans = [], []
    for chunk in parts[1:]:
        txt, spans = text_with_math(chunk, warnings, footnotes=True, macros=macros)
        if txt:
            items.append(txt)
            item_spans.append(spans)
    if not items:
        txt, spans = text_with_math(parts[0], warnings, footnotes=True)
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


def _unbrace(value: str) -> str:
    """Drop a single pair of protecting braces around a whole bib value (``{Vaswani, A}`` -> the
    list), leaving inner braces such as ``${\\alpha}$`` intact."""
    v = (value or "").strip()
    if v.startswith("{") and v.endswith("}") and v.count("{") == v.count("}"):
        depth = 0
        for i, ch in enumerate(v):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and i != len(v) - 1:
                    return v          # closes before the end: not one enclosing pair
        return v[1:-1].strip()
    return v


def _bib_fields_from_regex(text: str, warnings: list):
    """Fallback .bib reader when bibtexparser is unavailable or chokes on a malformed entry.

    One field per match, with balanced-brace depth allowed inside ``{...}``: the previous pattern
    used a greedy ``(.*)`` across the whole entry, so a multi-field record came back as one string
    of ``field = {…},`` noise (measured on 2402.12424/custom.bib: 53/53 entries wrong, some
    degenerating to the ``[key]`` stub). A fallback that rewrites a bibliography is worse than none.
    """
    out = []
    for m in re.finditer(r"@\s*(\w+)\s*\{\s*([^,\s]+)\s*,", text):
        etype, key = m.group(1).lower(), m.group(2)
        end, _ = find_env_close(text, m.end())
        block = text[m.end():end] if end > 0 else text[m.end():]
        fields = {}
        for fm in re.finditer(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*\{((?:[^{}{}]|\{[^{}]*\})*)\}",
                              block):
            fields.setdefault(fm.group(1).lower(),
                              norm_ws(strip_comments(_unbrace(fm.group(2)))))
        for fm in re.finditer(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]*)"', block):
            fields.setdefault(fm.group(1).lower(),
                              norm_ws(strip_comments(_unbrace(fm.group(2)))))
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
            blocks.extend(paragraphs(text[pos:], ctx["warnings"], ctx.get("macros")))
            break
        kind, start, end, payload = nxt
        if start > pos:
            blocks.extend(paragraphs(text[pos:start], ctx["warnings"], ctx.get("macros")))
        if kind == "env":
            env, inner_start, inner_end, after = payload
            inner = text[inner_start:inner_end]
            if env in SKIP_ENVS:
                pass
            elif env in DIAGRAM_ENVS:
                pass
            elif env in FIGURE_ENVS:
                f = parse_figure(inner, ctx["warnings"], ctx.get("macros"))
                if f["label"] or f["caption"]:
                    f["section"] = ctx.get("section_index")
                    # figure*/sidewaysfigure span the whole page, not one column: the IEEE renderer
                    # needs that to choose \textwidth over \columnwidth (Step A2).
                    f["span"] = env in SPANNING_FLOAT_ENVS
                    ctx["figures"].append(f)
                    if f["label"]:
                        ctx["labels"].setdefault(f["label"], f["caption"] or "")
                    for panel in f.get("panel_labels") or []:
                        ctx["labels"].setdefault(panel, f["caption"] or "")
            elif env in TABLE_ENVS:
                t = parse_table(inner, ctx["warnings"], env=env, whole=text[start:end],
                              macros=ctx.get("macros"))
                if t["label"] or t["caption"] or t["latex"]:
                    t["section"] = ctx.get("section_index")
                    t["span"] = env in SPANNING_FLOAT_ENVS
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
                blocks.append(parse_list(inner, LIST_ENVS[env], ctx["warnings"],
                                          ctx.get("macros")))
            elif depth < 6 and env not in ("verbatim", "lstlisting", "minted"):
                blocks.extend(scan_blocks(inner, ctx, depth + 1))
            else:
                txt, spans = text_with_math(inner, ctx["warnings"], footnotes=True,
                                            macros=ctx.get("macros"))
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
            lab, rkind, macro, rlabels = payload
            lab = (lab or "").strip()
            labs = rlabels or ([lab] if lab else [])
            if not labs:
                ctx["warnings"].append("empty_ref_argument")
            else:
                # "Table VI", not "VI": the word the source macro would have printed, and the
                # canonical LaTeX the template emits verbatim. `label` stays the first target so
                # renderer.anchor_floats() still finds the float this sentence cites.
                parts = _ref_parts(labs, macro, text[pos:start],
                                   kind_of=(lambda k: ctx["label_kinds"].get(k))
                                   if ctx.get("label_kinds") else None)
                block = {"type": "ref", "label": labs[0], "refs": parts,
                         "xref": ref_join(parts)}
                if len(labs) > 1:
                    block["labels"] = labs
                if rkind == "figure":
                    block["type"] = "figure_ref"
                elif rkind == "table":
                    block["type"] = "table_ref"
                blocks.append(block)
        elif kind == "filemacro":
            block = payload
            blocks.append(block)
            # Registering the label is what closes the critical: the prose says
            # "the summary prompt in Listing \ref{lst:prompt:summary}", and the reference was blank
            # because nothing in the rebuilt document ever defined that label.
            lab = (block.get("label") or "").strip()
            if lab:
                ctx["labels"].setdefault(lab, block.get("caption") or block.get("path") or "")
            ctx.setdefault("external_files", []).append(block)
        elif kind == "noop":
            ctx["warnings"].append(f"unclosed environment: {payload}")
        elif kind == "section":
            break
        pos = max(end, pos + 1)
    return blocks


def _footnote_arg_ranges(text: str) -> list:
    """``[start, end)`` of every ``\\footnote{...}`` argument in *text*, outermost only."""
    ranges = []
    for m in _FNOTE_MACRO_RE.finditer(text):
        _opts, braces, after = _read_xref_args(text, m.end())
        if not braces:
            continue
        # The argument is the first top-level group; its end is where scanning stopped for it.
        start = text.index("{", m.end()) if "{" in text[m.end():after] else m.end()
        ranges.append((start, after))
    return ranges


def _inside_footnote(text: str, pos: int) -> bool:
    """True when *pos* falls inside a footnote argument.

    ``_next_construct`` treats every ``\\cite``/``\\ref`` as a block boundary, which used to cut a
    footnote in half before ``paragraphs`` ever saw it: GPT-3's ``...meta-learning\\footnote{In the
    context of language models this has been called...}`` came out as two paragraphs with a
    detached parenthesis, and 2303.08774 hides ``\\footnote{\\href{u}{u}}`` the same way. Inside a
    note the citation is handled by ``extract_xref_spans`` on the intact argument instead, so the
    note stays one unit and the marker pair around it stays balanced.
    """
    return any(start <= pos < end for start, end in _footnote_arg_ranges(text))


def _next_construct(text: str, pos: int, kinds=None):
    """Earliest structure of interest at or after ``pos``: (kind, start, end, payload)."""
    kinds = kinds or {}
    cands = []
    m = _ENV_BEGIN_RE.search(text, pos)
    if m:
        env, istart = m.group(1), m.end()
        if env.lower() in _ARGFUL_ENVS:
            # ``\begin{CJK*}{UTF8}{gbsn}`` carries mandatory arguments in the tag. They are not
            # content, and leaving them in the body turned "{UTF8}{gbsn}" into a stray paragraph
            # that printed ``UTF8gbsn`` above 2501.12948's first section. Only these environments
            # are skipped: for a general environment a leading ``{...}`` really can be its body.
            _o, _b, istart = _read_xref_args(text, istart)
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
    if m and not _inside_footnote(text, m.start()):
        keys = _read_cite_args(text, m.end())
        cands.append(("cite", m.start(), _args_end(text, m.end()), keys))
    m = _REF_RE.search(text, pos)
    if m and not _inside_footnote(text, m.start()):
        lab = _read_first_arg(text, m.end())
        # The macro *name* has to travel with the target: \Cref and \autoref print a type word
        # ("Table VI") that a plain \ref does not, and the block path is where most references in
        # these papers land. Dropping the name here is what turned "In \Cref{tab:x}" into "In VI".
        macro = m.group(0).lstrip("\\").rstrip("*")
        labels = [k.strip() for k in re.split(r"[,;]", lab or "") if k.strip()]
        cands.append(("ref", m.start(), _args_end(text, m.end()),
                      (lab, kinds.get(lab) if lab else None, macro, labels)))
    m = _SECTION_RE.search(text, pos)
    if m:
        cands.append(("section", m.start(), m.end(), None))
    m = _FILE_MACRO_RE.search(text, pos)
    if m and not _inside_footnote(text, m.start()):
        block, after_fm = parse_file_macro(text, m)
        if block is not None:
            cands.append(("filemacro", m.start(), after_fm, block))
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


def paragraphs(chunk: str, warnings: list = None, macros=None):
    """Prose between structures, split on blank lines. Comments are already gone.

    Math is lifted out before latex_to_text (v2): otherwise ``$x \\in A$`` becomes ``x ∈ A`` and
    the renderer later emits ``∈`` outside math mode, which pdflatex rejects per character."""
    out = []
    for part in re.split(r"\n\s*\n", chunk):
        txt, spans = text_with_math(part, warnings, footnotes=True, macros=macros)
        if txt and re.search(r"[A-Za-z0-9]", txt):
            block = {"type": "paragraph", "text": txt}
            if spans:
                block["math_spans"] = spans
            out.append(block)
    return out


def _bind_region_labels(section: dict, region_text: str, ctx: dict):
    """Give a section the ``\\label`` the source wrote *inside* its body instead of under its heading.

    ``build_sections`` used to accept a heading label only when it was adjacent to the heading, so
    three real source shapes lost it and every ``\\ref`` to it became a stranded keyword:
    ``\\section{X}\\input{graphs/fig.tex}\\label{section:x}`` (2005.14165 main.tex:267-269), and a
    bare ``\\label{sec: evaluation_set}`` sitting mid-prose (2501.12948 appendix.tex:415, :788).
    In LaTeX a ``\\label`` with no intervening stepped counter refers to the current section, so
    binding it is the faithful reading, not a guess.

    Only a label that (a) is section-family, (b) no figure, table or equation already claims, and
    (c) no other section has taken, is bound -- and only to a section that has no label yet, so a
    heading's own label is never overwritten.
    """
    if section.get("level", 0) < 1 or not region_text or "\\" not in region_text:
        return
    claimed = ctx.get("labels") or {}
    taken = ctx.setdefault("section_labels", set())
    own = re.sub(r"\s+", "", (section.get("label") or "")).lower()
    for m in _LABEL_RE.finditer(region_text):
        lab = (m.group(1) or "").strip()
        if not lab or not _is_section_label(lab):
            continue
        key = re.sub(r"\s+", "", lab).lower()
        # The heading's own label is inside this region too (the region starts at the closing
        # brace), so it must not be re-bound as a second \label of the same section.
        if key == own or key in claimed or key in taken:
            continue
        if section.get("label"):
            # The heading already owns a label; a second one in the body still has to be defined
            # or a \ref to it resolves to nothing, so record it alongside rather than discarding it.
            extra = section.setdefault("extra_labels", [])
            extra.append(lab)
            taken.add(key)
            continue
        section["label"] = lab
        taken.add(key)
        return


def build_sections(body: str, ctx: dict):
    """Level 1..4 section list; ``level: 0, heading: null`` is the lead-in before the first heading.

    A heading is kept even when its body scans empty, because the heading sequence is one of the
    fields Step E compares — dropping it would hide a real structural difference on one side only.
    """
    sections = []
    cur = {"level": 0, "heading": None, "blocks": []}
    pos = 0
    in_appendix = False

    def region(text):
        """Blocks of one run between headings, honouring any ``\\appendix`` marker inside it."""
        nonlocal in_appendix
        if _APPENDIX_RE.search(text):
            in_appendix = True
            text = _APPENDIX_RE.sub(" ", text)
        return scan_blocks(text, ctx)

    while True:
        m = _SECTION_RE.search(body, pos)
        if not m:
            break
        name = re.match(r"\\(\w+)", m.group(0)).group(1)
        inner, after = match_braces(body, m.end() - 1)
        # Which section a float was declared in is the anchor renderer.anchor_floats() falls back to
        # when the body never cites it, so the ordinal has to be in ctx before the region is scanned.
        ctx["section_index"] = len(sections)
        region_text = body[pos:m.start()]
        cur["blocks"].extend(region(region_text))
        _bind_region_labels(cur, region_text, ctx)
        if cur["blocks"] or cur["heading"] is not None:
            sections.append(cur)
        heading_src, heading_spans = extract_math_spans(inner, ctx["warnings"])
        cur = {"level": _SECTION_LEVEL.get(name, 1),
               "heading": clean_heading(heading_src, ctx.get("macros")),
               "blocks": []}
        # ``\section*{Acknowledgements}`` is a heading the author deliberately did not number.
        # _SECTION_RE matches the star and used to discard it, so GPT-3's two unnumbered end-matter
        # headings came out as "IX. Acknowledgements" and "X. Contributions" -- two numbered
        # sections the source does not have. Recorded here, emitted by the templates.
        if re.match(r"\\%s\s*\*" % re.escape(name), m.group(0)):
            cur["numbered"] = False
        # A section's own ``\label`` is how ``\ref{sec:approach}`` resolves. The rebuilt document
        # re-numbers headings, so without carrying the label the reference is defined nowhere and
        # LaTeX prints ?? -- which is why this is part of the same fix as the blank cross-refs.
        lab = _LABEL_RE.search(inner)
        if not lab:
            lab = re.match(r"\s*(?:\\label\s*\{([^{}]*)\})", body[after:after + 160])
            if lab:
                cur["label"] = lab.group(1).strip()
        else:
            cur["label"] = lab.group(1).strip()
        if in_appendix:
            cur["appendix"] = True
        if heading_spans:
            cur["math_spans"] = heading_spans
        pos = after
    ctx["section_index"] = len(sections)
    tail = body[pos:]
    cur["blocks"].extend(region(tail))
    _bind_region_labels(cur, tail, ctx)
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
        # Some arXiv deposits are the compiled PDF itself (1602.03837 is 935 kB of %PDF-1.4 served
        # as main.tex). Parsing it as LaTeX produced 101 errors from PDF object names such as \TH
        # and a PDF of the wrong paper at the end of it; the honest answer is that there is no
        # manuscript text here, so the app reports that instead of compiling garbage.
        if raw.lstrip()[:5] == "%PDF-":
            warnings.append("this arXiv source is a compiled PDF rather than LaTeX - the paper's "
                            "source was never deposited, so there is no manuscript text to "
                            "reformat (upload the .tex or try another paper)")
            sir["paper_id"] = pid
            sir["counts"] = _counts(sir)
            return sir
        if len(raw) > MAX_TEXT_BYTES:
            warnings.append(f"main file truncated at {MAX_TEXT_BYTES} bytes")
            raw = raw[:MAX_TEXT_BYTES]

        stripped = strip_comments(raw)
        full = inline_includes(stripped, os.path.dirname(main), roots, warnings)
        # A pdfpages wrapper (1412.6980) says "the paper is in 0_adam_main.pdf". Nothing in the
        # .tex can recover those pages, so the outcome must be reported rather than looked like a
        # renderer bug: one page of front matter and no body.
        if re.search(r"\\includepdf\s*(?:\[[^\]]*\])?\s*\{", full):
            warnings.append("the source includes its own PDF via \\includepdf (pdfpages): the "
                            "manuscript body was never deposited as LaTeX, so only the front "
                            "matter and any LaTeX around the include can be reformatted")

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

        # Collected before the metadata pass so the abstract gets the same expansion prose does:
        # 2501.12948 writes \dsri{} in running text, and latex_to_text() deletes an unresolved
        # control word along with its (empty) argument.
        sir["macros"] = collect_macro_definitions(preamble)
        text_macros = build_text_macro_expansions(sir["macros"])
        md = extract_metadata(preamble, body, warnings, text_macros)
        ctx = {"warnings": warnings, "figures": figures, "tables": tables,
               "equations": equations, "labels": {}, "n_cites": 0,
               "external_files": [], "macros": text_macros,
               "label_kinds": collect_label_kinds(scan_body)}
        sir["sections"] = build_sections(scan_body, ctx)
        sir["labels"] = ctx["labels"]
        sir["external_files"] = ctx["external_files"]
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
