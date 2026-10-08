"""Tiny Markdown -> HTML renderer for the reports this tool writes (headings, tables, blockquotes,
lists, paragraphs, **bold**, _italic_, `code`, [links](https://...)).

Report text contains untrusted Play Store reviews, so everything is HTML-escaped first and only
http(s) links are turned into anchors. Not a general Markdown implementation.
"""

from __future__ import annotations

import html
import re

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?![\w])")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    codes: list[str] = []

    def keep(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"
    t = _CODE.sub(keep, text)
    t = html.escape(t, quote=True)
    t = _LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>', t)
    t = _BOLD.sub(r"<strong>\1</strong>", t)
    t = _ITALIC.sub(r"<em>\1</em>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{html.escape(codes[int(m.group(1))])}</code>", t)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    parts = re.split(r"(?<!\\)\|", row)
    return [p.strip().replace("\\|", "|") for p in parts]


def _row(tag: str, cells: list[str], aligns: list[str]) -> str:
    return "".join(f'<{tag}{" class=num" if k < len(aligns) and aligns[k] else ""}>{_inline(c)}</{tag}>'
                   for k, c in enumerate(cells))


def render(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    para: list[str] = []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush_para()
            i += 1
            continue
        if m := re.match(r"^(#{1,4})\s+(.*)$", s):
            flush_para()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{3,}", lines[i + 1].strip()):
            flush_para()
            head = _cells(s)
            aligns = ["right" if c.strip().endswith(":") else "" for c in _cells(lines[i + 1])]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            body = "".join(f"<tr>{_row('td', r, aligns)}</tr>" for r in rows)
            out.append(f'<div class="table"><table><thead><tr>{_row("th", head, aligns)}</tr></thead>'
                       f"<tbody>{body}</tbody></table></div>")
            continue
        if s.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline(q) for q in quote if q) + "</blockquote>")
            continue
        if re.match(r"^\s*[-*]\s+", line):
            flush_para()
            items: list[tuple[int, str]] = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                items.append((indent, re.sub(r"^\s*[-*]\s+", "", lines[i])))
                i += 1
            html_list, depth = [], 0
            for indent, text in items:
                level = 1 if indent >= 2 else 0
                while depth < level + 1:
                    html_list.append("<ul>")
                    depth += 1
                while depth > level + 1:
                    html_list.append("</ul>")
                    depth -= 1
                html_list.append(f"<li>{_inline(text)}</li>")
            html_list.append("</ul>" * depth)
            out.append("".join(html_list))
            continue
        para.append(s)
        i += 1
    flush_para()
    return "\n".join(out)
