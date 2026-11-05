#!/usr/bin/env python3
"""renderer.py — SIR JSON -> full LaTeX source for another format (Session 7).

Session 4's recompile.py was a *wrapper swap*: it kept the paper's original body and only changed
the \\documentclass. This renderer is the stronger thing: it throws the body away and rebuilds a
complete document from the SIR's structured content, through a Jinja2 template, for whichever class
is asked for. If the resulting document still reads back as the same paper (Step E round trip), then
the format was genuinely translated rather than relabelled.

CLI:
    python renderer.py --sir <id>_original.json --format ieee --output <id>/ieee.tex \\
        [--templates-dir D:/OpenCode/skills/render_templates]

Formats: ieee | acm | lncs  ("llncs" is accepted as an alias of the on-disk key "lncs").

Text protection: a SIR text field was already flattened by sir_extractor.latex_to_text(), which
leaves & % # _ $ bare. Bare % would delete the rest of the line, bare & breaks text mode and an
unpaired $ opens math mode, so the renderer escapes all of them; sir_extractor un-escapes them again
on the way back out, which is what makes the round trip exact rather than what breaks it
(measured on 21,512 fields: 0.9994 surviving, versus 0.9963 if the text is emitted verbatim).

Math (SIR schema v2, Session 9): a v2 field carries ``math_spans`` -- raw LaTeX lifted out by the
extractor before pylatexenc could turn ``\\in`` into ``∈`` and drop the ``$``. After escaping, each
``«MATH_<i>»`` placeholder is replaced by its raw span *with delimiters*, so the output compiles in
math mode. A v1 field has no ``math_spans``; it falls back to plain escaping, unchanged.

Unicode fallback (Session 9.5): pylatexenc also emits math symbols *outside* any span, where
protect_tex() cannot help. ``UNICODE_MATH_MAP`` turns those back into LaTeX after escaping; the
non-math parts of a v2 field get the same treatment around each placeholder. Symbols with no
mapping are left alone and reported as a warning rather than silently dropped.

Nothing raises: missing title/authors/sections/references fall back to placeholders and are
reported in the returned warnings list.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

TEMPLATES = {"ieee": "paper_ieee.j2", "acm": "paper_acm.j2",
             "lncs": "paper_llncs.j2", "llncs": "paper_llncs.j2"}

BS = chr(92)
# What the flattener leaves bare in SIR text. $ is included because a SIR field's "$" is nearly
# always a literal currency sign (13 fields in 120 sampled papers contain "$" at all, only 2 with an
# unbalanced pair) -- and an unpaired $ opens math mode, which is the "Missing $ inserted" compile
# error. Escaping it is also strictly more faithful: 0.99940 vs 0.99893 fields surviving a second
# extraction pass (render/dollar_probe.py, 21,512 fields).
_PROTECT_CHARS = "&#_%$"
_PROTECT_RE = re.compile("(?<![" + re.escape(BS) + "])([" + re.escape(_PROTECT_CHARS) + "])")
_KEY_BAD = re.compile(r"[^A-Za-z0-9_:.\-\/]")


def protect_tex(s):
    """Escape the LaTeX catcode characters the flattener left bare, leaving escaped ones alone."""
    if not s:
        return ""
    return _PROTECT_RE.sub(lambda g: BS + g.group(1), str(s))


# ----------------------------------------------------------------- layout-artifact scrub (Session 13)
# Session 12's scrubber lived only inside ``sir_extractor.text_with_math()``, which left two gaps.
# (1) Coverage: an author name, a keyword, a reference string, a preserved table body and a
# paper-local macro never pass through it, so a dump sitting in any of those reached the PDF.
# (2) Form: it demanded two or more coordinate quadruples, so a lone ``text[[78, 726, 489, 905]]``
# survived it. Both are closed here, on the renderer's way out, so the artifact cannot be printed
# whatever produced the SIR. A quadruple is four plain comma-separated numbers - enough to tell a
# coordinate dump from the interval notation ``[[0, 4]]`` and the padding spec
# ``[[0,0],[0,0],[0,pad\_len],[0,0]]`` that real papers write in math.
_NUM = r"-?\d+(?:\.\d+)?"
_QUAD = r"\[\s*" + _NUM + r"(?:\s*,\s*" + _NUM + r"){3}\s*\]"
_BBOX_BODY = r"\[\s*" + _QUAD + r"(?:\s*,\s*" + _QUAD + r")*\s*\]"
# Text-mode fields: the bare [[...]] form is scrubbed too, because a field that holds prose has no
# legitimate double-bracket quadruple in it. The \todo branch is tried first so its braces go with
# it; latex_to_text() has already dropped the control sequence in most sources, which the second
# branch then catches.
_DUMP = (r"(?:\\?text\s*)?\[\s*" + _QUAD + r"(?:\s*,\s*" + _QUAD + r")*\s*\]"
         r"(?:\s*\})?")
BBOX_ARTIFACT_RE = re.compile(r"\\todo\s*(?:\[[^\]]*\])?\s*\{\s*" + _DUMP + r"\s*\}" + r"|" + _DUMP)
# Verbatim LaTeX (table bodies, equations, macros): only the marked forms, so an author's own
# [[1,2,3,4]] inside a tabular cannot be eaten. The \todo branch takes its closing brace too, which
# is what \todo{[[...]]} expands to in the nips/neurips styles.
BBOX_MARKED_RE = re.compile(r"\\todo\s*(?:\[[^\]]*\])?\s*\{\s*" + _BBOX_BODY + r"\s*\}"
                            r"|(?:\\?text)\s*" + _BBOX_BODY)


def strip_bbox(s, marked_only=False):
    """Drop bounding-box dumps that leaked out of a layout tool into the manuscript."""
    if not s or "[[" not in s:
        return s
    return (BBOX_MARKED_RE if marked_only else BBOX_ARTIFACT_RE).sub(" ", str(s))


# ----------------------------------------------------------------- CJK / combining marks (Session 13)
# 2501.12948 (DeepSeek-R1) deposits Chinese text in its source. None of the three target classes
# loads a CJK package, so every ideograph is a "Unicode character 请 (U+8BF7) not available" hard
# error: 869 of them and no PDF. Replacing each run with one visible [CJK] marker keeps the
# document compiling, keeps the surrounding Latin intact, and tells the reader what is missing.
# Fullwidth ASCII (U+FF01-U+FF5E) folds to its plain equivalent instead, which is what it means.
_CJK_RANGES = ((0x2E80, 0x2EFF), (0x3000, 0x303F), (0x3040, 0x30FF), (0x3400, 0x4DBF),
               (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF66, 0xFFDC), (0x20000, 0x2FA1F))
_COMBINING = (0x0300, 0x036F)


def _is_cjk(ch):
    o = ord(ch)
    return any(a <= o <= b for a, b in _CJK_RANGES)


def fold_cjk(s):
    """Fullwidth ASCII -> ASCII, combining marks -> dropped, CJK runs -> one ``[CJK]`` marker."""
    if not s:
        return ""
    out = []
    for ch in str(s):
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif _COMBINING[0] <= o <= _COMBINING[1]:
            continue
        elif _is_cjk(ch):
            if not out or out[-1] != "[CJK]":
                out.append("[CJK]")
        else:
            out.append(ch)
    return "".join(out)


def strip_cjk(s):
    """``fold_cjk`` plus the count of characters it replaced, for the warning."""
    if not s:
        return "", 0
    text = str(s)
    folded = fold_cjk(text)
    if folded == text:
        return text, 0
    return folded, sum(1 for ch in text if _is_cjk(ch) or 0xFF01 <= ord(ch) <= 0xFF5E
                       or _COMBINING[0] <= ord(ch) <= _COMBINING[1])


# ``deepseek``/``ctexart`` wrap CJK in a CJK* environment whose body is the text itself. The
# environment cannot survive a class that does not define it, so it is unwrapped and its contents
# go through the same fold.
_CJK_ENV_RE = re.compile(r"\\begin\s*\{CJK\*?\}(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}?"
                         r"|(?:\s*\\end\s*\{CJK\*?\})")


# pylatexenc turns \bullet into ∙ and \in into ∈ *outside* math too (inside $...$ Session 9's
# math_spans already restored the raw LaTeX). protect_tex() only knows the five catcode
# characters, so a bare ∙ reached pdflatex and aborted 1709.08492 with "Unicode character ∙
# (U+2219)". Every entry here must compile under the packages all three templates load
# (amsmath/amssymb/amsfonts); the symbols are base LaTeX or AMS.
UNICODE_MATH_MAP = {
    '∙': r'$\bullet$', '·': r'$\cdot$', '×': r'$\times$',
    '÷': r'$\div$', '±': r'$\pm$', '∓': r'$\mp$',
    '∈': r'$\in$', '∉': r'$\notin$', '⊂': r'$\subset$',
    '⊆': r'$\subseteq$', '⊃': r'$\supset$', '⊇': r'$\supseteq$',
    '∪': r'$\cup$', '∩': r'$\cap$', '∅': r'$\emptyset$',
    '∞': r'$\infty$', '∂': r'$\partial$', '∇': r'$\nabla$',
    '∑': r'$\sum$', '∏': r'$\prod$', '∫': r'$\int$',
    '√': r'$\sqrt{}$', '≤': r'$\leq$', '≥': r'$\geq$',
    '≠': r'$\neq$', '≈': r'$\approx$', '≡': r'$\equiv$',
    '∼': r'$\sim$', '∝': r'$\propto$', '→': r'$\rightarrow$',
    '←': r'$\leftarrow$', '↔': r'$\leftrightarrow$',
    '⇒': r'$\Rightarrow$', '⇐': r'$\Leftarrow$',
    '⇔': r'$\Leftrightarrow$', '∀': r'$\forall$', '∃': r'$\exists$',
    '¬': r'$\neg$', '∧': r'$\wedge$', '∨': r'$\vee$',
    'α': r'$\alpha$', 'β': r'$\beta$', 'γ': r'$\gamma$',
    'δ': r'$\delta$', 'ε': r'$\varepsilon$', 'ζ': r'$\zeta$',
    'η': r'$\eta$', 'θ': r'$\theta$', 'ι': r'$\iota$',
    'κ': r'$\kappa$', 'λ': r'$\lambda$', 'μ': r'$\mu$',
    'ν': r'$\nu$', 'ξ': r'$\xi$', 'π': r'$\pi$',
    'ρ': r'$\rho$', 'σ': r'$\sigma$', 'τ': r'$\tau$',
    'υ': r'$\upsilon$', 'φ': r'$\phi$', 'χ': r'$\chi$',
    'ψ': r'$\psi$', 'ω': r'$\omega$',
    'Γ': r'$\Gamma$', 'Δ': r'$\Delta$', 'Θ': r'$\Theta$',
    'Λ': r'$\Lambda$', 'Ξ': r'$\Xi$', 'Π': r'$\Pi$',
    'Σ': r'$\Sigma$', 'Υ': r'$\Upsilon$', 'Φ': r'$\Phi$',
    'Ψ': r'$\Psi$', 'Ω': r'$\Omega$',
    '⟨': r'$\langle$', '⟩': r'$\rangle$',
    '‖': r'$\|$', '′': r"$'$", '″': r"$''$",
    '°': r'$^{\circ}$', '†': r'$\dagger$', '‡': r'$\ddagger$',
    '§': r'\S', '¶': r'\P', '©': r'\copyright',
    '®': r'\textregistered', '™': r'\texttrademark',
    '…': r'\ldots{}', '–': r'--', '—': r'---',
    '‘': r'`', '’': r"'", '“': r'``', '”': r"''",
    # Remaining members of the corpus's observed math-symbol set (Session 9 census).
    '≃': r'$\simeq$', '≅': r'$\cong$', '⊕': r'$\oplus$',
    '⊗': r'$\otimes$', '∘': r'$\circ$', '∖': r'$\setminus$',
    '−': r'$-$', '⋅': r'$\cdot$', '⊥': r'$\perp$',
    '≪': r'$\ll$', '≫': r'$\gg$', '⋆': r'$\star$',
    '∗': r'$\ast$', '↦': r'$\mapsto$', '↪': r'$\hookrightarrow$',
    '⟶': r'$\longrightarrow$', '⊢': r'$\vdash$', '⊨': r'$\models$',
    'ℓ': r'$\ell$', 'ℏ': r'$\hbar$', '⋯': r'$\cdots$',
    '⋮': r'$\vdots$', '⋱': r'$\ddots$', '∠': r'$\angle$',
    '△': r'$\triangle$', 'ℝ': r'$\mathbb{R}$', 'ℕ': r'$\mathbb{N}$',
    'ℤ': r'$\mathbb{Z}$', 'ℚ': r'$\mathbb{Q}$', 'ℂ': r'$\mathbb{C}$',
}


def escape_unicode(text):
    """Replace Unicode math symbols with LaTeX.

    Runs AFTER protect_tex(): the replacements contain $, \\ and braces themselves, so escaping
    them would turn a working ``$\\bullet$`` into ``\\$\\bullet\\$`` and print the markup.
    """
    if not text:
        return ""
    return "".join(UNICODE_MATH_MAP.get(ch, ch) for ch in str(text))


def protect_tex_unicode(s):
    """The one text-mode choke point: scrub, fold, escape, then map Unicode.

    Every prose field in every template reaches the PDF through here (directly, or as the
    placeholder-free part of ``render_with_math``), which is what makes the Session 13 scrub a
    class fix rather than a per-field patch: author names, keywords, headings, captions, list items,
    reference strings and abstracts are all covered by one call site.
    """
    return escape_unicode(protect_tex(fold_cjk(strip_bbox(s))))


# protect_tex() escapes the "_" inside «MATH_<i>» as "\_", so the pattern must accept both forms.
_PLACEHOLDER_RE = re.compile(r"«MATH\\?_(\d+)»")
_PLACEHOLDER_SPLIT_RE = re.compile(r"(«MATH\\?_\d+»)")


def _live_dollar_positions(s):
    """Indexes of ``$`` preceded by an even number of backslashes.

    ``\\\\$`` is a line break followed by a live dollar; a single-backslash lookbehind calls it
    escaped, which is how the extractor mispaired the delimiters in 1709.08492's long caption.
    """
    out = []
    for i, ch in enumerate(s):
        if ch != "$":
            continue
        k, j = 0, i - 1
        while j >= 0 and s[j] == BS:
            k += 1
            j -= 1
        if k % 2 == 0:
            out.append(i)
    return out


def _unbalanced_dollar_span(span):
    if not isinstance(span, dict):
        return False
    if (span.get("delims") or "$") not in ("$", "$$"):
        return False
    raw = span.get("raw")
    return len(_live_dollar_positions("" if raw is None else str(raw))) % 2 == 1


def _recover_dollar_span(raw):
    """Re-split a span whose delimiter pairing is off.

    The extractor paired a closing ``$`` with the next closing ``$``, so the raw reads
    ``text $ math``: the part before the stray dollar was body text in the source, the part after
    it was the math that dollar opened. Emitting the text island protected and the math island
    wrapped rebuilds what the author wrote instead of opening a runaway math mode.
    """
    cuts = _live_dollar_positions(raw)
    segs, last = [], 0
    for p in cuts:
        segs.append(raw[last:p])
        last = p + 1
    segs.append(raw[last:])
    out = []
    for i, seg in enumerate(segs):
        if not seg:
            continue
        out.append("$" + seg + "$" if i % 2 else protect_tex_unicode(seg))
    return "".join(out)


def math_delimited(span):
    """A span dict -> raw LaTeX wrapped in its original delimiters. Unknown delimiters fall back
    to ``$...$``, which is valid for any math-mode content."""
    raw = span.get("raw")
    raw = "" if raw is None else str(raw)
    delims = span.get("delims") or "$"
    if delims in ("$", "$$") and len(_live_dollar_positions(raw)) % 2 == 1:
        return _recover_dollar_span(raw)
    if delims == "$$":
        return "$$" + raw + "$$"
    if delims == "\\(":
        return "\\(" + raw + "\\)"
    if delims == "\\[":
        return "\\[" + raw + "\\]"
    if delims == "\\begin{math}":
        return "\\begin{math}" + raw + "\\end{math}"
    if delims == "\\begin{displaymath}":
        return "\\begin{displaymath}" + raw + "\\end{displaymath}"
    return "$" + raw + "$"


def render_with_math(text, math_spans=None):
    """Escape LaTeX in *text* but put each ``math_spans`` entry back as raw math.

    The placeholders are ``«MATH_<i>»``; neither ``«`` nor ``»`` is in the protect list, so
    escaping leaves them intact and the substitution happens afterwards. A placeholder without a
    matching span is dropped rather than printed.
    """
    text = "" if text is None else str(text)
    if not math_spans:
        # The placeholders are internal sentinels: a field that still carries one has lost its
        # spans somewhere (v1 SIR, or a template that forgot to pass them), and printing «MATH_0»
        # is worse than dropping the span.
        return _PLACEHOLDER_RE.sub("", protect_tex_unicode(text))
    spans = {}
    for span in math_spans:
        if not isinstance(span, dict):
            continue
        try:
            spans[int(span.get("index"))] = span
        except (TypeError, ValueError):
            continue
    if not spans:
        return _PLACEHOLDER_RE.sub("", protect_tex_unicode(text))

    def swap(m):
        span = spans.get(int(m.group(1)))
        return "" if span is None else math_delimited(span)

    # Escape per placeholder-free part: a Unicode symbol *inside* a span is raw LaTeX already
    # (math_delimited puts it back untouched), while the same symbol outside becomes math here.
    parts = []
    for part in _PLACEHOLDER_SPLIT_RE.split(text):
        if _PLACEHOLDER_RE.fullmatch(part):
            parts.append(part)
        else:
            parts.append(protect_tex_unicode(part))
    return _PLACEHOLDER_RE.sub(swap, "".join(parts))


def citekey(s):
    """A \\bibitem/\\ref argument must survive both bibtex and LaTeX, so keep the safe subset."""
    if s is None:
        return "ref"
    k = _KEY_BAD.sub("", str(s).strip())
    return k or "ref"


# Blocks that are a *token* in the running text, not a paragraph of their own. The extractor splits
# a sentence at every \cite/\ref, so "improvements through tricks \cite{a} and \cite{b}, while ..."
# arrives as paragraph/citation/paragraph/citation/paragraph. LaTeX joins the pieces with a space
# only if there is no blank line between them, so the template must not put one there.
_INLINE_BLOCK_TYPES = {"citation", "figure_ref", "table_ref"}


def annotate_block_separators(sections):
    """Give each block a ``_sep``: the whitespace the template emits *before* it.

    A paragraph block already ends with "\\n", and one more newline is what ends a LaTeX paragraph:
    two consecutive paragraphs need it (they came from a blank line in the source). An inline block
    (citation/reference) must not get it, because the blank line would split the sentence it belongs
    to into a fragment with the citation number alone on the line. Every other boundary keeps the
    newline, which TeX reads as a space.
    """
    out = []
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        blocks, prev_type = [], None
        for i, block in enumerate(section.get("blocks") or []):
            if not isinstance(block, dict):
                blocks.append(block)
                continue
            block = dict(block)
            cur_inline = block.get("type") in _INLINE_BLOCK_TYPES
            block["_sep"] = "" if (prev_type == "paragraph" and cur_inline) else "\n"
            blocks.append(block)
            prev_type = block.get("type")
        section = dict(section)
        section["blocks"] = blocks
        out.append(section)
    return out


def graphic_path(s):
    """\\includegraphics target: whitespace and braces break it, so normalise and flag it."""
    p = str(s or "").strip().strip("{}")
    p = p.replace(BS + BS, "").strip()
    return strip_bbox(p)


# Session 12 forwarded the source's own placement hint ([t], [b!], [!htbp]) and then anchored the
# float at its citation. Anchoring moved the *source* of the float but TeX was still free to move
# the box, so on 2005.14165 Table II was typeset on top of Table I and the floats that the
# citation walk could not anchor piled up after the last section. ``[H]`` (float.sty, loaded by all
# three templates) is what "in the sequence of the given paper" requires: the float is typeset
# exactly where it is written and cannot be deferred.
FLOAT_PLACEMENT = "H"


def float_width(fmt, span=False):
    r"""The width a float's content is typeset at, per class.

    IEEEtran is two-column, so an ordinary figure takes ``\columnwidth`` and only a figure the
    source declared as ``figure*``/``table*`` (``span``) may take ``\textwidth``; giving every float
    ``\linewidth`` is what squeezed GPT-3's full-width plots into one column. acmart and LNCS bodies
    are set in one column, where ``\linewidth`` is the measure.
    """
    if fmt == "ieee":
        return BS + "textwidth" if span else BS + "columnwidth"
    return BS + "linewidth"


def anchor_floats(sections, figures, tables):
    """Emit every float at the place in the body where the paper put it.

    ``render_floats`` used to place every figure and table of the paper in one block after the last
    section, so Figure 1 of 1706.03762 - referenced on page 2 - was typeset on page 5. The SIR keeps
    an inline ``figure_ref``/``table_ref`` block with the target label wherever the body cites a
    float, so a cited float's position is available without touching the schema.

    Floats of a kind are still *emitted* in declaration order, because that is what numbers them:
    anchoring each one at its own first reference renumbered 1706.03762's tables (its Table III is
    cited in the body before its Table I, so a naive anchor made "Variations…" Table I). Each float
    goes at max(its own anchor, the position the previous float of that kind took).

    Session 13 replaced the one rule that broke this on real papers: a float with no in-text
    reference used to set ``tail_only``, which deferred **every later float of that kind** to the
    trailing pile. 2005.14165 cites only 35 of its 110 floats, so 102 of them arrived after the last
    section (report Step 0). An unanchored float now falls back to where it was declared -
    ``figures[i]["section"]`` is the ordinal of the source section that held the environment - and
    only a float whose declaration section is unknown still goes to the tail.
    """
    anchor_at = {}                                # label -> flat block index of first citation
    for n, block in enumerate(_flat_blocks(sections)):
        if isinstance(block, dict) and block.get("type") in ("figure_ref", "table_ref"):
            anchor_at.setdefault(str(block.get("label") or ""), n)
    section_end = _section_end_positions(sections)

    def plan(items):
        anchored, pending, last = [], [], -1
        for item in items or []:
            label = str(item.get("label") or "") if isinstance(item, dict) else ""
            pos = anchor_at.get(label)
            if pos is None:
                sec = item.get("section") if isinstance(item, dict) else None
                if isinstance(sec, int) and 0 <= sec < len(section_end):
                    pos = section_end[sec]
            if pos is None:
                pending.append(item)
                continue
            last = max(pos, last)
            anchored.append((last, item))
        return anchored, pending

    figure_plan, pending_figures = plan(figures)
    table_plan, pending_tables = plan(tables)
    insert_at = {}
    for kind, plan_items in (("figure", figure_plan), ("table", table_plan)):
        for pos, item in plan_items:
            insert_at.setdefault(pos, []).append({"type": "float", "kind": kind, "float": item,
                                                  "_sep": "\n"})
    out, n = [], -1
    for section in sections or []:
        section = dict(section)
        blocks = []
        for block in section.get("blocks") or []:
            n += 1
            blocks.append(block)
            blocks.extend(insert_at.get(n, []))
        section["blocks"] = blocks
        out.append(section)
    return out, pending_figures, pending_tables


def _section_end_positions(sections):
    """Flat block index of each section's last block: the declaration anchor ``anchor_floats`` uses.

    Index i is section i, including the level-0 lead-in, because the extractor emits sections in
    document order and records the ordinal each float was declared in.
    """
    ends, n = [], -1
    for section in sections or []:
        blocks = section.get("blocks") or [] if isinstance(section, dict) else []
        n += len(blocks)
        ends.append(n)
    return ends


def _flat_blocks(sections):
    """Every block of every section, in document order: the coordinate ``anchor_floats`` uses."""
    for section in sections or []:
        for block in section.get("blocks") or []:
            yield block


def insert_label(latex, label):
    """Put a \\label back inside an equation environment whose SIR copy had it stripped."""
    if not latex:
        return ""
    m = None
    for m in re.finditer(r"\\end\s*\{[^}]+\}", latex):
        pass
    if m is None:
        return latex + BS + "label{" + citekey(label) + "}"
    return latex[:m.start()] + BS + "label{" + citekey(label) + "}" + latex[m.start():]


DEFAULT_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "render_templates")

_MACRO_DEF_NAME_RE = re.compile(
    r"^\\(?:providecommand|DeclareMathOperator)\s*\{\s*\\([A-Za-z]+)\s*\}"
    r"|^\\def\s*\\([A-Za-z]+)")
# Colours are referenced by bare name (\cellcolor{ColorHigh}), so the \name test in select_macros
# can never see them used; they are matched and kept separately.
_DEFINECOLOR_RE = re.compile(r"^\\definecolor\s*\{\s*([^{}]+?)\s*\}")
_WALK_SKIP_KEYS = {"macros", "source", "extracted_at", "schema_version", "paper_id",
                   "source_format", "parse_warnings"}


def _sir_strings(obj):
    """Every string in a SIR except provenance/bookkeeping (notably ``macros`` itself)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in _WALK_SKIP_KEYS:
                yield from _sir_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _sir_strings(v)


_TIKZ_RE = re.compile(
    r"(?<![\\])\\(?:tikz\b|draw\b|fill\b|node\b|coordinate\b|shade\b|clip\b"
    r"|begin\s*\{tikzpicture\}|begin\s*\{scope\}|begin\s*\{pgfpicture\}|"
    r"begin\s*\{axis\}|begin\s*\{semilogyaxis\}|begin\s*\{loglogaxis\}|pgf\w*)")


def sanitize_tables(tables):
    """Replace table bodies the target classes cannot compile with a visible placeholder.

    A SIR table's ``latex`` is verbatim from the source, so a paper whose "tables" are TikZ
    diagrams (1709.08492) arrives as drawing instructions: undefined ``\\draw``/``\\tikz`` plus
    paper-local arrow macros. Unlike prose, that cannot be escaped into validity, and one such
    table sinks the whole document ("That makes 100 errors"). A placeholder keeps the caption
    and the numbering and is honest about what is missing.

    Returns ``(tables, dropped_count)``.
    """
    out, dropped = [], 0
    for tab in tables or []:
        if not isinstance(tab, dict):
            continue
        latex = tab.get("latex") or ""
        if _TIKZ_RE.search(latex):
            tab = dict(tab)
            label = tab.get("label") or "table"
            tab["latex"] = (BS + "fbox{" + BS + "textit{[Table " + protect_tex(str(label))
                            + ": diagram not renderable in this class]}}")
            dropped += 1
        out.append(tab)
    return out, dropped


def unbalanced_spans(sir):
    """How many math_spans the renderer had to re-split (see _recover_dollar_span)."""
    count = 0
    stack = [sir]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "math_spans" and isinstance(v, list):
                    count += sum(1 for sp in v if _unbalanced_dollar_span(sp))
                else:
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return count


def cjk_total(sir):
    """How many non-Latin characters the prose fold will replace, for the parse warning."""
    return sum(1 for s in _sir_strings(sir) for ch in s
               if _is_cjk(ch) or 0xFF01 <= ord(ch) <= 0xFF5E or _COMBINING[0] <= ord(ch) <= _COMBINING[1])


def unmapped_unicode(sir):
    """Characters this renderer would leave untouched: non-ASCII, not in the map, not a sentinel.

    Reporting them is the difference between "the paper compiled" and "the paper compiled because
    the reader silently lost a symbol".
    """
    found = set()
    for s in _sir_strings(sir):
        for ch in s:
            if ord(ch) > 127 and ch not in UNICODE_MATH_MAP and ch not in "«»":
                found.add(ch)
    return sorted(found)


def sanitize_verbatim(latex):
    r"""Scrub layout dumps and CJK from a field the renderer emits byte-for-byte.

    A table body, an equation and a preserved macro are LaTeX that must not be re-escaped, so the
    text-mode choke point in ``protect_tex_unicode`` never sees them. Two things are taken out and
    nothing else: a *marked* coordinate dump (``text[[...]]`` or ``\todo{[[...]]}``) and a CJK*
    environment wrapper, whose contents are folded to a marker. A bare ``[[1,2,3,4]]`` survives,
    because inside a tabular that is a matrix and not a dump.
    """
    if not latex:
        return ""
    s = _CJK_ENV_RE.sub(" ", str(latex))
    return fold_cjk(strip_bbox(s, marked_only=True))


_VERBATIM_KEYS = ("latex",)


def sanitize_sir_verbatim(sir):
    """Apply :func:`sanitize_verbatim` to every verbatim LaTeX field in one SIR, in place.

    Returns ``(bbox_dumps, cjk_chars)`` so the caller can report what it removed.
    """
    dumps, cjk = 0, 0
    stack = [sir]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k in _VERBATIM_KEYS and isinstance(v, str):
                    fixed = sanitize_verbatim(v)
                    if fixed != v:
                        dumps += len(BBOX_MARKED_RE.findall(v))
                        cjk += sum(1 for ch in v if _is_cjk(ch) or _COMBINING[0] <= ord(ch)
                                   <= _COMBINING[1] or 0xFF01 <= ord(ch) <= 0xFF5E)
                        node[k] = fixed
                elif k == "macros" and isinstance(v, list):
                    node[k] = [sanitize_verbatim(m) if isinstance(m, str) else m for m in v]
                else:
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return dumps, cjk


_MACRO_ONE_ARG_DEF_RE = re.compile(
    r"^\\(?:providecommand|newcommand|renewcommand|def)\s*\{?\s*(\\[A-Za-z@]+)\s*\}?\s*\[1\]")
_DUMP_SHAPED_BODY_RE = re.compile(r"\[\[\s*#1\s*\]\]")


def neutralise_dump_macros(definitions):
    r"""Redefine a preserved one-argument macro whose body wraps its argument in [[..]].

    1706.03762's source defines ``\providecommand{\todo}[1]{\textcolor{red}{[[#1]]}}`` and the SIR
    preserves it verbatim, so every ``\todo`` call in the body would print its own coordinate-style
    marker in the rebuilt PDF. Scrubbing text cannot help - the brackets come from the definition,
    not from the argument - and that is the leak tmp/s13/bbox_class_proof.py found. The macro is
    replaced by one that discards its argument, which is also what the source's own
    ``\usepackage[disable]{todonotes}`` asks for.

    The trade-off is stated rather than hidden: a macro that legitimately renders a closed interval
    as ``[[#1]]`` would lose its brackets too. Numbers and text survive; only the doubling does.
    """
    out = []
    for d in definitions or []:
        if isinstance(d, str) and "[[" in d and _DUMP_SHAPED_BODY_RE.search(d):
            m = _MACRO_ONE_ARG_DEF_RE.match(d.strip())
            if m:
                out.append("\\providecommand{" + m.group(1) + "}[1]{}")
                continue
        out.append(d)
    return out


def select_macros(definitions, sir):
    """Keep only the definitions the rendered document can actually reach.

    Unused definitions are not just noise: a macro that collides with a package command, or that
    needs a package the template does not load, would break a paper that otherwise compiles. The
    closure is transitive because ``\\norm`` may be defined in terms of ``\\set``.

    ``\\definecolor`` lines skip the reachability test and are always emitted: a colour is
    referenced by bare name (``\\cellcolor{ColorHigh}``), which the backslash-matching below can
    never see, and an unused colour definition cannot break anything. Dropping them cost
    2402.12424 108 errors per format ("Undefined color 'ColorHigh'").
    """
    by_name, colours = {}, {}
    for d in definitions or []:
        if not isinstance(d, str):
            continue
        d = d.strip()
        cm = _DEFINECOLOR_RE.match(d)
        if cm:
            colours.setdefault(cm.group(1), d)
            continue
        m = _MACRO_DEF_NAME_RE.search(d)
        name = (m.group(1) or m.group(2)) if m else None
        if name:
            by_name.setdefault(name, d)
    blob = "\n".join(_sir_strings(sir))

    def mentions(name, text):
        return re.search("\\\\" + re.escape(name) + r"(?![A-Za-z])", text) is not None

    if not by_name:
        return list(colours.values())
    used, frontier = set(), [n for n in by_name if mentions(n, blob)]
    while frontier:
        name = frontier.pop()
        if name in used:
            continue
        used.add(name)
        for other in by_name:
            if other not in used and mentions(other, by_name[name]):
                frontier.append(other)
    return [d for n, d in by_name.items() if n in used] + list(colours.values())


def _env(templates_dir=None):
    from jinja2 import Environment, FileSystemLoader
    ed = Environment(loader=FileSystemLoader(os.path.abspath(templates_dir or
                                                              DEFAULT_TEMPLATES_DIR)),
                     autoescape=False, keep_trailing_newline=True,
                     trim_blocks=False, lstrip_blocks=False)
    ed.filters["tex"] = protect_tex
    ed.filters["texu"] = protect_tex_unicode
    ed.filters["citekey"] = citekey
    ed.filters["graphpath"] = graphic_path
    ed.globals["insert_label"] = insert_label
    ed.globals["render_with_math"] = render_with_math
    ed.globals["float_width"] = float_width
    ed.globals["FLOAT_PLACEMENT"] = FLOAT_PLACEMENT
    return ed


def build_context(sir, fmt, warnings):
    """Normalise one SIR into the template variables, collecting warnings instead of crashing."""
    dumps, verbatim_cjk = sanitize_sir_verbatim(sir)
    if dumps:
        warnings.append(f"{dumps} bounding-box coordinate dump(s) removed from verbatim LaTeX")
    if verbatim_cjk:
        warnings.append(f"{verbatim_cjk} CJK character(s) in verbatim LaTeX replaced by [CJK]")
    md = dict(sir.get("metadata") or {})
    title = (md.get("title") or "").strip()
    if not title:
        warnings.append("metadata.title empty -> 'Untitled'")
        title = "Untitled"
    authors = [a for a in (md.get("authors") or []) if isinstance(a, dict)]
    if not authors:
        warnings.append("metadata.authors empty -> 'Anonymous'")
        authors = [{"name": "Anonymous", "affiliation": None, "email": None}]
    if not any(a.get("affiliation") for a in authors):
        warnings.append("no author affiliations in SIR")
    abstract = (md.get("abstract") or "").strip()
    if not abstract:
        warnings.append("metadata.abstract empty")
    sections = [s for s in (sir.get("sections") or []) if isinstance(s, dict)]
    if not sections:
        warnings.append("sections empty -> placeholder Introduction")
        sections = [{"level": 1, "heading": "Introduction",
                     "blocks": [{"type": "paragraph", "text": "No sections were recovered."}]}]
    refs = [r for r in (sir.get("references") or []) if isinstance(r, dict)]
    if not refs:
        warnings.append("references empty -> empty thebibliography")
    tables, tikz_tables = sanitize_tables(sir.get("tables") or [])
    if tikz_tables:
        warnings.append(f"{tikz_tables} table(s) were TikZ/PGF diagrams -> placeholder body")
    sections, pending_figures, pending_tables = anchor_floats(
        annotate_block_separators(sections), sir.get("figures") or [], tables)
    broken = unbalanced_spans(sir)
    if broken:
        warnings.append(f"{broken} math span(s) with unbalanced $ re-split into text + math")
    leftover = unmapped_unicode(sir)
    if leftover:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in leftover[:12])
        more = "" if len(leftover) <= 12 else f" (+{len(leftover) - 12} more)"
        warnings.append(f"{len(leftover)} non-ASCII character(s) left as-is by UNICODE_MATH_MAP: "
                        f"{shown}{more}")
    cjk = cjk_total(sir)
    if cjk:
        warnings.append(f"{cjk} CJK or combining character(s) in the source: the target classes "
                        f"load no CJK package, so they are folded to [CJK] markers and the output "
                        f"is incomplete for the non-Latin parts")
    return {
        "metadata": {"title": title, "authors": authors, "abstract": abstract,
                     "abstract_math_spans": md.get("abstract_math_spans") or [],
                     "keywords": md.get("keywords") or []},
        "sections": sections,
        "figures": sir.get("figures") or [],
        "tables": tables,
        "pending_figures": pending_figures,
        "pending_tables": pending_tables,
        "equations": sir.get("equations") or [],
        "references": refs,
        "macros": neutralise_dump_macros(select_macros(sir.get("macros") or [], sir)),
        "format": fmt,
        "paper_id": sir.get("paper_id") or "",
    }


def render(sir_path, fmt, templates_dir=None):
    """(tex_string, info dict). Raises only for a bad format name or unreadable SIR."""
    warnings = []
    if fmt not in TEMPLATES:
        raise ValueError(f"unknown format {fmt!r}; expected one of {sorted(TEMPLATES)}")
    with open(sir_path, "r", encoding="utf-8") as fh:
        sir = json.load(fh)
    ctx = build_context(sir, fmt, warnings)
    tex = _env(templates_dir).get_template(TEMPLATES[fmt]).render(**ctx)
    if not tex.strip():
        warnings.append("template produced empty output")
    return tex, {"success": bool(tex.strip()), "format": fmt,
                 "paper_id": ctx["paper_id"], "warnings": warnings}


def render_file(sir_path, fmt, out_path, templates_dir=None):
    tex, info = render(sir_path, fmt, templates_dir)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(tex)
    info["output"] = out_path.replace("\\", "/")
    info["bytes"] = len(tex.encode("utf-8"))
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description="SIR JSON -> LaTeX (Session 7 full-template renderer)")
    ap.add_argument("--sir", required=True, help="path to <paper_id>_<source_format>.json")
    ap.add_argument("--format", required=True, choices=sorted(TEMPLATES))
    ap.add_argument("--output", required=True, help=".tex path to write")
    ap.add_argument("--templates-dir", default=None,
                    help="directory holding paper_*.j2 (default: <this file>/render_templates)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    info = render_file(a.sir, a.format, a.output, a.templates_dir)
    if not a.quiet:
        print(f"{info['paper_id']} {info['format']}: {info['bytes']} bytes "
              f"warnings={len(info['warnings'])}")
        for w in info["warnings"]:
            print("  warn:", w)
    return 0 if info["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
