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
