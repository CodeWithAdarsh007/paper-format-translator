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
# Characters the flattener may leave bare that are legal prose but not legal LaTeX text mode.
#
# Angle brackets: OT1 -- the encoding IEEEtran still reaches for the body font -- has no glyph for
# either one, so a bare bracket is dropped at typeset time even when it survived extraction, and
# \textless / \textgreater are kernel commands needing no extra package. separate_urls() has
# already turned any URL in angle brackets into \url{...} before this runs, so what is left here is
# markup-like prose: 2501.12948 sn-article.tex:260's think tags and the chat-template tokens.
#
# A bare caret is math-only, so in text mode LaTeX reports "Missing $ inserted", opens math mode, and
# math mode then runs past the end of the field into the next control sequence. On 2303.08774 that
# swallowed the \item of \bibitem{Gray2017GPUKF} (main.bbl entry 34, whose predecessor's title is
# "Self-attention does not need o(n^2) memory"), so the counter never advanced: the references page
# printed [33] twice and every later number came out one low. \^ would put a circumflex accent on
# the following letter instead, so the literal-caret command is the correct emit.
_SPECIAL_RE = re.compile("(?<![" + re.escape(BS) + "])([<>^])")
_SPECIAL_EMIT = {
    "<": BS + "textless{}",
    ">": BS + "textgreater{}",
    "^": BS + "textasciicircum{}",
}
_KEY_BAD = re.compile(r"[^A-Za-z0-9_:.\-\/]")
_LABEL_IN_TEX_RE = re.compile(r"\\label\s*\{([^{}]*)\}")


def protect_tex(s):
    """Escape the LaTeX catcode characters the flattener left bare, leaving escaped ones alone."""
    if not s:
        return ""
    out = _PROTECT_RE.sub(lambda g: BS + g.group(1), str(s))
    return _SPECIAL_RE.sub(lambda g: _SPECIAL_EMIT[g.group(1)], out)


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


# ------------------------------------------------- directive / macro-name leaks (Session 14A-1)
# The pre-storage pass in ``sir_extractor`` handles these at the source; this set is the emit-time
# backstop for text that reached the SIR by another route (v1 SIRs, verbatim table bodies,
# preserved paper-local macros, author blocks). The two lists are deliberately copies of the same
# tuples and ``test_scrubbers_agree_at_both_ends`` pins that, exactly as the bbox scrub does.
# Each pattern is a *bookkeeping* string: a directive with its own key=value shape, or a macro name
# that only makes sense as TeX. Ordinary prose matches none of them, which is why "footnote" is
# stripped only in its artifact forms ("2 footnote", "footnote []") and never as a plain word.
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
DIRECTIVE_LEAK_RES = tuple(re.compile(p) for p in DIRECTIVE_LEAK_PATTERNS)


def strip_directive_leaks(s):
    """Remove leaked directives and macro names from text about to be emitted."""
    if not s:
        return s
    s = str(s)
    if "[" not in s and "sec:" not in s and "lst:" not in s and "footnote" not in s \
            and "UTF8gbsn" not in s and "fnsymbolarabic" not in s:
        return s
    for rx in DIRECTIVE_LEAK_RES:
        s = rx.sub(" ", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip() if "\n" not in s else re.sub(r"[ \t]{2,}", " ", s)


# ------------------------------------------------------- duplicate / glued URLs (Session 14A-1)
# pylatexenc prints ``\href{u}{t}`` as ``ut``, so a link whose target is also its visible text is
# emitted twice with no separator (2005.14165's ``newser.comnewser.com``, 2303.08774's
# ``.../evals.../evals``, 2501.12948's model cards). The extractor now keeps only the visible
# argument; these two rules are the emit-time backstop for text that reached the SIR another way.
_GLUED_SCHEME_RE = re.compile(r"(?<=[A-Za-z0-9)!.\-])(https?://|mailto:)")
_DUP_URL_RE = re.compile(
    r"([A-Za-z0-9:/.\-?%=&+~#]{2,}?(?:\.(?:com|org|net|io|ai|dev|gov|edu)(?::\d+)?"
    r"(?:/[A-Za-z0-9:/.\-?%=&+~#]*)?))\1")


def separate_urls(s):
    """Collapse an immediately repeated URL, and un-glue a word from a scheme that follows it."""
    if not s or ("http://" not in s and "https://" not in s and "mailto:" not in s):
        return s
    s = str(s)
    for _ in range(3):                       # a^3 collapses to a, but two passes is the common case
        out = _DUP_URL_RE.sub(r"\1", s)
        out = _GLUED_SCHEME_RE.sub(r" \1", out)
        if out == s:
            break
        s = out
    return s




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
    """The one text-mode choke point: scrub, strip directive leaks, fold, escape, then map Unicode.

    Every prose field in every template reaches the PDF through here (directly, or as the
    placeholder-free part of ``render_with_math``), which is what makes the Session 13 scrub a
    class fix rather than a per-field patch: author names, keywords, headings, captions, list items,
    reference strings and abstracts are all covered by one call site.
    """
    return escape_unicode(protect_tex(
        fold_cjk(strip_directive_leaks(separate_urls(strip_bbox(s))))))


# protect_tex() escapes the "_" inside «MATH_<i>» and «XREF_<i>» as "\_", so the pattern must
# accept both forms.
_PLACEHOLDER_RE = re.compile(r"«(?:MATH|XREF)\\?_(\d+)»")
_PLACEHOLDER_SPLIT_RE = re.compile(r"(«(?:MATH|XREF)\\?_\d+»)")

# The prose footnote markers ``sir_extractor.extract_footnote_markers`` leaves around a note's
# argument. They carry no index into the span list, so they are resolved here rather than in
# ``math_delimited``, and they never need escaping (no catcode character in them).
_FOOT_OPEN_RE = re.compile(r"«FOOTOPEN@(\d+)»")
_FOOT_CLOSE_RE = re.compile(r"«FOOTCLOSE@(\d+)»")


def emit_footnote_markers(tex):
    """Turn the extractor's prose footnote marker pair into a real ``\\footnote{...}``.

    Balance is checked before anything is emitted: an unmatched pair would open a group LaTeX never
    closes and cost the whole document, so an unbalanced field falls back to the Session 14A-1 form
    (note text inline, no apparatus), which is always safe to compile.
    """
    if not tex or ("«FOOTOPEN@" not in tex and "«FOOTCLOSE@" not in tex):
        return tex
    if len(_FOOT_OPEN_RE.findall(tex)) != len(_FOOT_CLOSE_RE.findall(tex)):
        return _FOOT_CLOSE_RE.sub("", _FOOT_OPEN_RE.sub("", tex))
    # A lambda replacement, not a template string: re.sub interprets escapes in its replacement,
    # so a template of "\footnote{" would insert a form feed (\f) and print garbage.
    tex = _FOOT_OPEN_RE.sub(lambda m: BS + "footnote{", tex)
    return _FOOT_CLOSE_RE.sub("}", tex)


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
    if span.get("xref") is not None:
        # A citation or reference lifted out by ``extract_xref_spans``: already canonical LaTeX,
        # and wrapping it in ``$`` would be a math-mode error.
        return str(span["xref"])
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
        # is worse than dropping the span. Footnote markers need no span, so they are still
        # resolved here.
        return emit_footnote_markers(_PLACEHOLDER_RE.sub("", protect_tex_unicode(text)))
    spans = {}
    for span in math_spans:
        if not isinstance(span, dict):
            continue
        try:
            spans[int(span.get("index"))] = span
        except (TypeError, ValueError):
            continue
    if not spans:
        return emit_footnote_markers(_PLACEHOLDER_RE.sub("", protect_tex_unicode(text)))

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
    return emit_footnote_markers(_PLACEHOLDER_RE.sub(swap, "".join(parts)))


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
_INLINE_BLOCK_TYPES = {"citation", "figure_ref", "table_ref", "ref"}


# Emit-time copy of ``sir_extractor.ref_join``: a reference is assembled by the extractor, but a
# *partially* unresolvable one (two targets, one of whose section or listing the class never
# reproduces) can only be trimmed here, where the set of defined labels is known. The copy exists
# for the same reason the directive-leak list is duplicated: the test suite loads these modules by
# path and the renderer may be bound to a vendored copy. ``test_ref_join_agrees_at_both_ends``
# fails if the two implementations ever disagree.
_REF_WORD_PLURAL = {"Appendix": "Appendices", "Figure": "Figures", "Table": "Tables",
                    "Section": "Sections", "Equation": "Equations", "Listing": "Listings",
                    "Algorithm": "Algorithms", "Theorem": "Theorems"}
_REF_WORD_SINGULAR = {v: k for k, v in _REF_WORD_PLURAL.items()}
_REF_PIECE_RE = re.compile(
    r"(?:(%s)~)?\\ref\{([^{}]*)\}"
    % "|".join(sorted(set(_REF_WORD_PLURAL) | set(_REF_WORD_PLURAL.values()), key=len, reverse=True)))


def ref_join(parts):
    """Join ``(word, label)`` reference parts exactly as ``sir_extractor.ref_join`` does."""
    if not parts:
        return ""
    words = [(p.get("word") or "") for p in parts]
    labs = [(p.get("label") or "") for p in parts]
    if len(parts) == 1:
        return ("%s~\\ref{%s}" % (words[0], labs[0])) if words[0] else "\\ref{%s}" % labs[0]
    if len(set(words)) == 1 and words[0]:
        head = _REF_WORD_PLURAL.get(words[0], words[0] + "s")
        out = ["%s~\\ref{%s}" % (head, labs[0])]
        rest = labs[1:]
        for i, lab in enumerate(rest):
            out.append((" and~\\ref{%s}" if i == len(rest) - 1 else ", ~\\ref{%s}") % lab)
        return "".join(out)
    return ", ".join(("%s~\\ref{%s}" % (w, lab)) if w else ("\\ref{%s}" % lab)
                     for w, lab in zip(words, labs))


def split_ref_pieces(xref):
    """``"Figures~\\ref{a} and~\\ref{b}"`` -> the parts ``ref_join`` would have produced.

    ``ref_join`` prints one plural word for a same-family group and leaves the later targets
    bare, so the head word is propagated to them and singularised: the parts a trim rebuilds from
    must carry the family word, not the printed surface form.
    """
    out = []
    for w, lab in _REF_PIECE_RE.findall(xref or ""):
        if w:
            out.append({"word": _REF_WORD_SINGULAR.get(w, w), "label": lab})
        else:
            out.append({"word": out[-1]["word"] if out else "", "label": lab})
    return out


def ref_targets(block):
    """Every label a reference block points at, whichever SIR generation produced it."""
    parts = block.get("refs")
    if parts:
        return [p.get("label") for p in parts if isinstance(p, dict) and p.get("label")]
    lab = block.get("label") or ""
    return [k.strip() for k in re.split(r"[,;]", lab) if k.strip()]


def defined_labels(sections, figures, tables, equations, label_map=None):
    """Every ``\\label`` the rebuilt document will actually define.

    Deliberately generous: a target wrongly judged missing deletes a working reference, which is
    worse than the ``??`` it was meant to prevent. So float labels, section labels, equation labels,
    any ``\\label`` inside verbatim table or equation LaTeX, and the SIR's label map all count.
    """
    labs = set()

    def add(value):
        if value:
            key = citekey(value)
            if key and key != "ref":
                labs.add(key)

    for s in sections or []:
        if isinstance(s, dict) and (s.get("heading") or "").strip():
            add(s.get("label"))
            # \label commands the source wrote inside the section body (see _bind_region_labels):
            # the template emits them on the heading line, so they are defined and must not be
            # judged missing here.
            for extra in s.get("extra_labels") or []:
                add(extra)
    for coll in (figures or [], tables or []):
        for f in coll:
            if isinstance(f, dict):
                add(f.get("label"))
                # subfigure/subfloat panel labels, emitted inside the parent float by _shared.j2
                for panel in f.get("panel_labels") or []:
                    add(panel)
                add(_LABEL_IN_TEX_RE.search(f.get("latex") or ""))
    for e in equations or []:
        if isinstance(e, dict):
            add(e.get("label"))
            add(_LABEL_IN_TEX_RE.search(e.get("latex") or ""))
            for m in _LABEL_IN_TEX_RE.finditer(e.get("latex") or ""):
                add(m.group(1))
    for key in (label_map or {}):
        add(key)
    return labs


def drop_unresolved_refs(sections, figures, tables, equations, defined, warnings):
    """Remove a reference whose target the rebuilt document does not define.

    Emitting ``\\ref{sec:approach}`` for a section that has no such label prints ``??`` — the audit
    lists ``??`` as its own Priority-0 class, and 444 of them appeared the moment blank
    cross-references were turned into real ``\\ref`` calls. The sentence keeps its wording; the
    number that cannot exist is dropped, and the count is reported rather than hidden.
    """
    dropped = []

    for s in sections or []:
        if not isinstance(s, dict):
            continue
        keep, removed = [], 0
        for b in s.get("blocks") or []:
            if isinstance(b, dict) and b.get("type") in ("ref", "figure_ref", "table_ref"):
                # Put every target through citekey before comparing, exactly as _clean_span_refs
                # does for the inline path: 2402.12424 writes \label{tab: my-table} with a space
                # inside the braces, the template cleans the \label, and \ref{tab: my-table} vs
                # \label{tab:my-table} are the same key to LaTeX but not to this check.
                if b.get("xref") and re.search(r"\\ref\{[^{}]*[ \t][^{}]*\}", b["xref"]):
                    b = dict(b)
                    b["xref"] = re.sub(r"\\ref\{([^{}]*)\}",
                                       lambda m: "\\ref{%s}" % ",".join(
                                           citekey(x) for x in m.group(1).split(",") if x.strip()),
                                       b["xref"])
                    b["refs"] = split_ref_pieces(b["xref"])
                labs = ref_targets(b)
                bad = [l for l in labs if citekey(l) not in defined]
                if not bad:
                    keep.append(b)
                    continue
                if len(bad) == len(labs):
                    # Nothing in the reference resolves: drop the block, keyword and all, so the
                    # sentence loses the citation rather than printing "Section ," at its reader.
                    removed += 1
                    continue
                # Part of a multi-target reference resolves ("Figures 3 and 7" where only 3
                # exists): keep the targets the document defines instead of deleting the whole
                # reference, which is what the old single-label check did to every group ref.
                b = dict(b)
                parts = [p for p in (b.get("refs") or split_ref_pieces(b.get("xref") or ""))
                         if citekey(p.get("label") or "") in defined]
                b["refs"] = parts
                b["xref"] = ref_join(parts)
                b["label"] = parts[0].get("label") or ""
                keep.append(b)
                continue
            keep.append(b)
        # The list is always written back: a reference can be *rewritten* (citekey cleaning,
        # trimming one dead target of a group) without anything being dropped, and assigning only
        # when `removed` is set silently discarded those edits.
        if keep != s.get("blocks"):
            s["blocks"] = keep
        if removed:
            dropped.append(removed)
        _clean_span_refs(s.get("math_spans"), defined, dropped)
        for b in s.get("blocks") or []:
            if isinstance(b, dict):
                _clean_span_refs(b.get("math_spans"), defined, dropped, items=True)
    for coll in (figures or [], tables or []):
        for f in coll:
            if isinstance(f, dict):
                _clean_span_refs(f.get("math_spans"), defined, dropped)
    if dropped:
        warnings.append("%d cross-reference(s) point at a label this document does not define "
                        "(a section, listing or exhibit the source kept but the class does not "
                        "reproduce): dropped rather than printed as ??" % sum(dropped))
    return sections


def _clean_span_refs(spans, defined, dropped, items=False):
    """Normalise and, where needed, blank the ``xref`` spans in *spans*.

    The label inside ``\\ref{...}`` is put through ``citekey`` for the same reason the templates do
    it: 2005.14165's own label is ``figure:eval strategies`` with a space, and the float's
    ``\\label`` is emitted sanitised. Two spellings of the same target is exactly how LaTeX prints
    ``??``.
    """
    if not spans:
        return
    groups = spans if items and isinstance(spans, list) and spans and isinstance(spans[0], list) \
        else [spans]
    for group in groups:
        if not isinstance(group, list):
            continue
        for span in group:
            if not isinstance(span, dict) or "xref" not in span:
                continue
            xref = span["xref"]
            if not xref:
                continue
            xref = re.sub(r"\\ref\{([^{}]*)\}",
                          lambda m: "\\ref{%s}" % ",".join(
                              citekey(x) for x in m.group(1).split(",") if x.strip()), xref)
            xref = re.sub(r"\\cite(\[[^\]]*\])?\{([^{}]*)\}",
                          lambda m: "\\cite%s{%s}" % (m.group(1) or "", ",".join(
                              citekey(x) for x in m.group(2).split(",") if x.strip())), xref)
            span["xref"] = xref
            targets = re.findall(r"\\ref\{([^{}]*)\}", xref)
            if not targets:
                continue
            keys = [k.strip() for t in targets for k in t.split(",")]
            if all(citekey(k) in defined for k in keys):
                continue
            parts = [p for p in split_ref_pieces(xref) if citekey(p["label"]) in defined]
            if not parts:
                span["xref"] = ""
                dropped.append(1)
                continue
            span["xref"] = ref_join(parts)


def mark_appendix_break(sections):
    """Flag the first appendix section so a template emits ``\\appendix`` exactly once.

    The class then letters the sections that follow (A, B, C) and restarts float numbering, which
    is what the source's own ``\\appendix`` asked for. The flag itself comes from
    ``sir_extractor.build_sections``; a SIR without it (schema v1, or a paper with no appendix)
    renders exactly as before.
    """
    done = False
    for s in sections or []:
        if not isinstance(s, dict) or not s.get("appendix") or done or s.get("level", 1) != 1:
            continue
        s["_appendix_break"] = True
        done = True
    return sections


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


def float_placement(span=False):
    r"""The placement specifier a float may legally carry.

    ``H`` (float.sty, loaded by all three templates) is what keeps an ordinary float in the
    sequence of the paper, which is the Session 13 guarantee. A spanning float cannot have it:
    IEEEtran's ``figure*``/``table*`` accept only ``t``/``b``/``p``, and an unknown option is both a
    warning and a float that never appears. ``!t`` is the closest legal equivalent — top of the
    page, with the page-float restrictions overridden.
    """
    return "!t" if span else FLOAT_PLACEMENT


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
    ed.globals["float_placement"] = float_placement
    ed.globals["FLOAT_PLACEMENT"] = FLOAT_PLACEMENT
    return ed


def cite_author_labels(refs):
    """key (citekey-sanitised) -> the natbib author short-label, e.g. "Henighan et~al.".

    The ``\\bibitem`` optional label is the ground truth (it is what natbib prints before the number
    for a textual citation). ``\\citet`` is flattened to ``\\cite`` by the extractor because none of
    the three target classes loads natbib, so this is what lets the renderer put the surname back.
    Falls back to the first-author surname from the raw entry only when there is no label.
    """
    by_key = {}
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        key = r.get("key")
        if not key:
            continue
        st = r.get("structured") or {}
        label = (st.get("bbl_label") or "").strip()
        prefix = ""
        if label:
            prefix = label.split("(", 1)[0].strip()          # "Henighan et~al." from "...(2020)..."
        else:
            raw = (r.get("raw") or st.get("text") or "").strip()
            first = raw.split(",", 1)[0].strip()
            toks = first.split()
            if 2 <= len(toks) <= 4 and first.replace(" ", "").isalpha():
                prefix = toks[-1]
        if prefix:
            by_key[citekey(key)] = prefix
    return by_key


def resolve_textual_cites(nodes, by_key):
    """Rewrite the extractor's textual-cite spans so ``\\citet{X}`` prints "Author~\\cite{X}".

    A span the extractor flagged ``textual`` carried the author into running text in the source
    ("as in~\\citet{henighan2020scaling}" -> "as in Henighan et~al. [15]"). The author is looked up
    per key so ``\\citet{A,B}`` becomes "A [N], B [M]". Only spans carrying the flag are touched, so
    ordinary ``\\cite``/``\\citep`` render as a bare "[N]" exactly as before.
    """
    def walk(x):
        if isinstance(x, dict):
            if x.get("textual") and x.get("type") == "citation" and x.get("keys"):
                x["render"] = ", ".join(
                    (by_key.get(citekey(k), "") + "~" if by_key.get(citekey(k)) else "")
                    + "\\cite{%s}" % citekey(k) for k in x["keys"])
            elif x.get("textual") and x.get("kind") == "cite" and x.get("keys") and "xref" in x:
                parts = []
                for k in x["keys"]:
                    pre = by_key.get(citekey(k), "")
                    parts.append((pre + "~" if pre else "") + "\\cite{%s}" % citekey(k))
                if parts:
                    x["xref"] = ", ".join(parts)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(nodes)


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
    figures = sir.get("figures") or []
    equations = sir.get("equations") or []
    defined = defined_labels(sections, figures, tables, equations, sir.get("labels") or {})
    drop_unresolved_refs(sections, figures, tables, equations, defined, warnings)
    sections, pending_figures, pending_tables = anchor_floats(
        annotate_block_separators(mark_appendix_break(sections)),
        figures, tables)
    textual_authors = cite_author_labels(refs)
    if textual_authors:
        resolve_textual_cites(
            [sections, figures, tables, equations, md.get("abstract_math_spans") or []],
            textual_authors)
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
