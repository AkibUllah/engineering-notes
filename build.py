#!/usr/bin/env python3
"""Build the static site from posts/*.md into _site/.

Usage:
    python build.py                 # build into _site/
    python build.py --serve [PORT]  # build, then serve at http://127.0.0.1:PORT/engineering-notes/

Configuration comes from environment variables so the same script works locally and in CI:
    SITE_URL   absolute URL of the site root   (default https://akibullah.github.io/engineering-notes/)
    BASE_PATH  path prefix for internal links  (default /engineering-notes/)
"""

from __future__ import annotations

import html
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import format_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template

import markdown
from pygments.formatters import HtmlFormatter

ROOT = Path(__file__).resolve().parent
POSTS_DIR = ROOT / "posts"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
OUT_DIR = ROOT / "_site"

SITE_TITLE = "Engineering Notes"
SITE_AUTHOR = "Akib Jafor"
SITE_DESCRIPTION = (
    "Deep dives into the design decisions, trade-offs and tests behind my open-source "
    "backend projects: payments, queues, rate limiting, auth, retrieval and agents."
)
SITE_URL = os.environ.get("SITE_URL", "https://akibullah.github.io/engineering-notes/").rstrip("/") + "/"
BASE_PATH = "/" + os.environ.get("BASE_PATH", "/engineering-notes/").strip("/") + "/"
if BASE_PATH == "//":
    BASE_PATH = "/"

WORDS_PER_MINUTE = 230
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs"

FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FENCE_RE = re.compile(r"^```[^\n]*\n.*?^```[ \t]*$", re.S | re.M)
MERMAID_RE = re.compile(r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)


@dataclass
class Post:
    slug: str
    title: str
    date: date
    summary: str
    tags: list[str]
    repo: str
    body_md: str
    html: str = ""
    words: int = 0
    prose_words: int = 0
    has_mermaid: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{BASE_PATH}posts/{self.slug}/"

    @property
    def absolute_url(self) -> str:
        return f"{SITE_URL}posts/{self.slug}/"

    @property
    def reading_minutes(self) -> int:
        return max(1, math.ceil(self.words / WORDS_PER_MINUTE))

    @property
    def display_date(self) -> str:
        return f"{self.date.day} {self.date:%b %Y}"


def parse_front_matter(text: str, source: Path) -> tuple[dict[str, str], str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        raise SystemExit(f"{source}: missing front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise SystemExit(f"{source}: bad front matter line {line!r}")
        meta[key.strip()] = value.strip()
    for required in ("title", "date", "summary", "tags", "repo"):
        if required not in meta:
            raise SystemExit(f"{source}: front matter needs '{required}'")
    return meta, text[match.end() :]


def count_words(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", text))


def render_markdown(body: str) -> tuple[str, bool]:
    """Markdown to HTML. Mermaid fences become <pre class="mermaid"> for the client renderer;
    every other fence is highlighted by Pygments at build time."""
    diagrams: list[str] = []

    def stash(match: re.Match[str]) -> str:
        diagrams.append(match.group(1))
        return f"\nMERMAIDBLOCK{len(diagrams) - 1}\n"

    body = MERMAID_RE.sub(stash, body)
    md = markdown.Markdown(
        extensions=["fenced_code", "codehilite", "tables", "toc", "sane_lists", "smarty", "attr_list"],
        extension_configs={
            "codehilite": {"css_class": "highlight", "guess_lang": False},
            "toc": {"permalink": "#", "permalink_title": "Link to this section"},
            "smarty": {"smart_angled_quotes": False},
        },
        output_format="html",
    )
    out = md.convert(body)
    for i, source in enumerate(diagrams):
        block = f'<figure class="diagram"><pre class="mermaid">{html.escape(source)}</pre></figure>'
        out = out.replace(f"<p>MERMAIDBLOCK{i}</p>", block)
    # Tables scroll horizontally on narrow screens instead of widening the page.
    out = out.replace("<table>", '<div class="table-wrap"><table>').replace(
        "</table>", "</table></div>"
    )
    # External links open normally but announce themselves to assistive tech via rel.
    out = re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" rel="noopener"', out)
    return out, bool(diagrams)


def load_posts() -> list[Post]:
    posts: list[Post] = []
    for path in sorted(POSTS_DIR.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"), path)
        slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)
        post = Post(
            slug=slug,
            title=meta["title"],
            date=date.fromisoformat(meta["date"]),
            summary=meta["summary"],
            tags=[t.strip() for t in meta["tags"].split(",") if t.strip()],
            repo=meta["repo"],
            body_md=body,
            extra={k: v for k, v in meta.items() if k not in {"title", "date", "summary", "tags", "repo"}},
        )
        post.html, post.has_mermaid = render_markdown(body)
        post.words = count_words(body)
        prose = FENCE_RE.sub("", body)
        prose = "\n".join(l for l in prose.splitlines() if not l.lstrip().startswith("|"))
        post.prose_words = count_words(prose)
        posts.append(post)
    # Newest first; posts on the same day keep a stable order via the optional "order" key.
    posts.sort(key=lambda p: (p.date, int(p.extra.get("order", "0"))), reverse=True)
    return posts


def template(name: str) -> Template:
    return Template((TEMPLATES_DIR / name).read_text(encoding="utf-8"))


def tag_list(tags: list[str]) -> str:
    items = "".join(f'<li class="tag">{html.escape(t)}</li>' for t in tags)
    return f'<ul class="tags" aria-label="Tags">{items}</ul>'


def page(*, title: str, description: str, canonical: str, body: str, mermaid: bool, og_type: str) -> str:
    mermaid_script = ""
    if mermaid:
        mermaid_script = template("mermaid.html").substitute(cdn=MERMAID_CDN)
    return template("base.html").substitute(
        title=html.escape(title),
        description=html.escape(description, quote=True),
        canonical=canonical,
        base=BASE_PATH,
        site_title=SITE_TITLE,
        author=SITE_AUTHOR,
        og_type=og_type,
        body=body,
        mermaid=mermaid_script,
        year=datetime.now(timezone.utc).year,
    )


def render_index(posts: list[Post]) -> str:
    items = []
    for p in posts:
        items.append(
            template("index_item.html").substitute(
                url=p.url,
                title=html.escape(p.title),
                iso_date=p.date.isoformat(),
                display_date=p.display_date,
                minutes=p.reading_minutes,
                summary=html.escape(p.summary),
                tags=tag_list(p.tags),
                repo=html.escape(p.repo),
            )
        )
    body = template("index.html").substitute(items="\n".join(items), base=BASE_PATH)
    return page(
        title=f"{SITE_TITLE} by {SITE_AUTHOR}",
        description=SITE_DESCRIPTION,
        canonical=SITE_URL,
        body=body,
        mermaid=False,
        og_type="website",
    )


def render_post(post: Post, newer: Post | None, older: Post | None) -> str:
    def nav_link(p: Post | None, label: str, cls: str) -> str:
        if p is None:
            return ""
        return (
            f'<a class="{cls}" href="{p.url}"><span class="nav-label">{label}</span>'
            f"<span>{html.escape(p.title)}</span></a>"
        )

    body = template("post.html").substitute(
        title=html.escape(post.title),
        iso_date=post.date.isoformat(),
        display_date=post.display_date,
        minutes=post.reading_minutes,
        tags=tag_list(post.tags),
        repo=html.escape(post.repo),
        repo_url=f"https://github.com/AkibUllah/{post.repo}",
        content=post.html,
        base=BASE_PATH,
        newer=nav_link(newer, "Newer", "nav-newer"),
        older=nav_link(older, "Older", "nav-older"),
    )
    return page(
        title=f"{post.title} · {SITE_TITLE}",
        description=post.summary,
        canonical=post.absolute_url,
        body=body,
        mermaid=post.has_mermaid,
        og_type="article",
    )


def render_feed(posts: list[Post]) -> str:
    def rfc822(d: date) -> str:
        return format_datetime(datetime(d.year, d.month, d.day, 9, 0, tzinfo=timezone.utc))

    items = []
    for p in posts:
        categories = "".join(f"<category>{html.escape(t)}</category>" for t in p.tags)
        items.append(
            "<item>"
            f"<title>{html.escape(p.title)}</title>"
            f"<link>{p.absolute_url}</link>"
            f'<guid isPermaLink="true">{p.absolute_url}</guid>'
            f"<pubDate>{rfc822(p.date)}</pubDate>"
            f"<description>{html.escape(p.summary)}</description>"
            f"{categories}"
            "</item>"
        )
    last = rfc822(posts[0].date) if posts else rfc822(date.today())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n<channel>'
        f"<title>{SITE_TITLE}</title>"
        f"<link>{SITE_URL}</link>"
        f'<atom:link href="{SITE_URL}feed.xml" rel="self" type="application/rss+xml"/>'
        f"<description>{html.escape(SITE_DESCRIPTION)}</description>"
        "<language>en</language>"
        f"<lastBuildDate>{last}</lastBuildDate>"
        + "".join(items)
        + "</channel>\n</rss>\n"
    )


def render_sitemap(posts: list[Post]) -> str:
    urls = [f"<url><loc>{SITE_URL}</loc></url>"] + [
        f"<url><loc>{p.absolute_url}</loc><lastmod>{p.date.isoformat()}</lastmod></url>" for p in posts
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(urls)
        + "</urlset>\n"
    )


def pygments_css() -> str:
    light = HtmlFormatter(style="friendly").get_style_defs(".highlight")
    dark = HtmlFormatter(style="github-dark").get_style_defs(".highlight")
    dark_block = "\n".join("  " + line for line in dark.splitlines())
    return (
        "/* Written by build.py from Pygments styles 'friendly' (light) and 'github-dark'. */\n"
        f"{light}\n"
        f"@media (prefers-color-scheme: dark) {{\n{dark_block}\n}}\n"
    )


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build() -> list[Post]:
    posts = load_posts()
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir()
    shutil.copytree(STATIC_DIR, OUT_DIR, dirs_exist_ok=True)
    write(OUT_DIR / "pygments.css", pygments_css())
    write(OUT_DIR / "index.html", render_index(posts))
    for i, post in enumerate(posts):
        newer = posts[i - 1] if i > 0 else None
        older = posts[i + 1] if i + 1 < len(posts) else None
        write(OUT_DIR / "posts" / post.slug / "index.html", render_post(post, newer, older))
    write(OUT_DIR / "feed.xml", render_feed(posts))
    write(OUT_DIR / "sitemap.xml", render_sitemap(posts))
    write(
        OUT_DIR / "404.html",
        page(
            title=f"Not found · {SITE_TITLE}",
            description="Page not found.",
            canonical=SITE_URL,
            body=f'<main class="wrap"><h1>Not found</h1><p><a href="{BASE_PATH}">Back to all articles</a></p></main>',
            mermaid=False,
            og_type="website",
        ),
    )
    (OUT_DIR / ".nojekyll").touch()
    for p in posts:
        print(f"  {p.date}  {p.prose_words:>5} words  {p.reading_minutes:>2} min  {p.slug}")
    print(f"built {len(posts)} posts into {OUT_DIR.relative_to(ROOT)}/")
    return posts


def serve(port: int) -> None:
    """Serve _site/ under BASE_PATH so links behave exactly as on GitHub Pages."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(OUT_DIR), **kwargs)

        def translate_path(self, path: str) -> str:
            if BASE_PATH != "/" and path.startswith(BASE_PATH):
                path = "/" + path[len(BASE_PATH) :]
            return super().translate_path(path)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving on http://127.0.0.1:{port}{BASE_PATH}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    build()
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8080)
