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


# protect_tex() escapes the "_" inside «MATH_<i>» as "\_", so the pattern must accept both forms.
_PLACEHOLDER_RE = re.compile(r"«MATH\\?_(\d+)»")


def math_delimited(span):
    """A span dict -> raw LaTeX wrapped in its original delimiters. Unknown delimiters fall back
    to ``$...$``, which is valid for any math-mode content."""
    raw = span.get("raw")
    raw = "" if raw is None else str(raw)
    delims = span.get("delims") or "$"
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
        return protect_tex(text)
    spans = {}
    for span in math_spans:
        if not isinstance(span, dict):
            continue
        try:
            spans[int(span.get("index"))] = span
        except (TypeError, ValueError):
            continue
    escaped = protect_tex(text)
    if not spans:
        return _PLACEHOLDER_RE.sub("", escaped)

    def swap(m):
        span = spans.get(int(m.group(1)))
        return "" if span is None else math_delimited(span)

    return _PLACEHOLDER_RE.sub(swap, escaped)


def citekey(s):
    """A \\bibitem/\\ref argument must survive both bibtex and LaTeX, so keep the safe subset."""
    if s is None:
        return "ref"
    k = _KEY_BAD.sub("", str(s).strip())
    return k or "ref"


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
    return {
        "metadata": {"title": title, "authors": authors, "abstract": abstract,
                     "keywords": md.get("keywords") or []},
        "sections": sections,
        "figures": sir.get("figures") or [],
        "tables": sir.get("tables") or [],
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
