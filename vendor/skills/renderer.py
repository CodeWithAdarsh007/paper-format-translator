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
    """protect_tex + escape_unicode, for text-mode fields that carry no math_spans."""
    return escape_unicode(protect_tex(s))


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
    return p


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


def select_macros(definitions, sir):
    """Keep only the definitions the rendered document can actually reach.

    Unused definitions are not just noise: a macro that collides with a package command, or that
    needs a package the template does not load, would break a paper that otherwise compiles. The
    closure is transitive because ``\\norm`` may be defined in terms of ``\\set``.
    """
    by_name = {}
    for d in definitions or []:
        if not isinstance(d, str):
            continue
        m = _MACRO_DEF_NAME_RE.search(d.strip())
        name = (m.group(1) or m.group(2)) if m else None
        if name:
            by_name.setdefault(name, d.strip())
    if not by_name:
        return []
    blob = "\n".join(_sir_strings(sir))

    def mentions(name, text):
        return re.search("\\\\" + re.escape(name) + r"(?![A-Za-z])", text) is not None

    used, frontier = set(), [n for n in by_name if mentions(n, blob)]
    while frontier:
        name = frontier.pop()
        if name in used:
            continue
        used.add(name)
        for other in by_name:
            if other not in used and mentions(other, by_name[name]):
                frontier.append(other)
    return [d for n, d in by_name.items() if n in used]


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
    return ed


def build_context(sir, fmt, warnings):
    """Normalise one SIR into the template variables, collecting warnings instead of crashing."""
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
    broken = unbalanced_spans(sir)
    if broken:
        warnings.append(f"{broken} math span(s) with unbalanced $ re-split into text + math")
    leftover = unmapped_unicode(sir)
    if leftover:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in leftover[:12])
        more = "" if len(leftover) <= 12 else f" (+{len(leftover) - 12} more)"
        warnings.append(f"{len(leftover)} non-ASCII character(s) left as-is by UNICODE_MATH_MAP: "
                        f"{shown}{more}")
    return {
        "metadata": {"title": title, "authors": authors, "abstract": abstract,
                     "abstract_math_spans": md.get("abstract_math_spans") or [],
                     "keywords": md.get("keywords") or []},
        "sections": annotate_block_separators(sections),
        "figures": sir.get("figures") or [],
        "tables": tables,
        "equations": sir.get("equations") or [],
        "references": refs,
        "macros": select_macros(sir.get("macros") or [], sir),
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
