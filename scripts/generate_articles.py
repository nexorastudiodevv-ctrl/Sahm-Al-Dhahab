#!/usr/bin/env python3
"""Generate static, indexable pages for articles published in Firebase."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

BASE_URL = "https://nexorastudio-eg.com"
FIREBASE_DATABASE_URL = "https://goldhub-1fdb1-default-rtdb.firebaseio.com"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
START_MARKER = "<!-- PUBLISHED_ARTICLES_START -->"
END_MARKER = "<!-- PUBLISHED_ARTICLES_END -->"
ALLOWED_TAGS = {
    "a", "blockquote", "br", "em", "h2", "h3", "h4", "hr", "i", "img",
    "li", "ol", "p", "pre", "s", "strong", "u", "ul",
}
VOID_TAGS = {"br", "hr", "img"}
DROP_CONTENT_TAGS = {"iframe", "math", "object", "script", "style", "svg"}
ARABIC_TRANSLITERATION = str.maketrans({
    "ء": "a", "آ": "a", "أ": "a", "ؤ": "w", "إ": "i", "ئ": "y",
    "ا": "a", "ب": "b", "ة": "h", "ت": "t", "ث": "th", "ج": "j",
    "ح": "h", "خ": "kh", "د": "d", "ذ": "dh", "ر": "r", "ز": "z",
    "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
    "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l",
    "م": "m", "ن": "n", "ه": "h", "و": "w", "ى": "a", "ي": "y",
    "پ": "p", "چ": "ch", "ژ": "zh", "گ": "g",
})


def safe_url(value: str, *, image: bool = False) -> str | None:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme:
        if parsed.scheme.lower() not in ({"http", "https"} if image else {"http", "https", "mailto"}):
            return None
        return candidate
    if parsed.netloc or candidate.startswith("//"):
        return None
    if image and not candidate.startswith("/"):
        return None
    return candidate or None


class SafeArticleHTML(HTMLParser):
    """Keep useful editor markup while dropping executable HTML and attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.text: list[str] = []
        self.stack: list[str] = []
        self.skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip_stack:
            if tag in DROP_CONTENT_TAGS:
                self.skip_stack.append(tag)
            return
        if tag in DROP_CONTENT_TAGS:
            self.skip_stack.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return

        attributes = dict(attrs)
        rendered_attrs = ""
        if tag == "a":
            href = safe_url(attributes.get("href") or "")
            if href:
                rendered_attrs += f' href="{html.escape(href, quote=True)}"'
                rendered_attrs += ' rel="noopener noreferrer"'
        elif tag == "img":
            src = safe_url(attributes.get("src") or "", image=True)
            if not src:
                return
            rendered_attrs = (
                f' src="{html.escape(src, quote=True)}"'
                f' alt="{html.escape(attributes.get("alt") or "", quote=True)}"'
                ' loading="lazy"'
            )

        self.output.append(f"<{tag}{rendered_attrs}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_stack:
            if tag in self.skip_stack:
                while self.skip_stack:
                    skipped = self.skip_stack.pop()
                    if skipped == tag:
                        break
            return
        if tag not in self.stack:
            return
        while self.stack:
            current = self.stack.pop()
            self.output.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.skip_stack:
            return
        self.output.append(html.escape(data))
        self.text.append(data)

    def finish(self) -> tuple[str, str]:
        while self.stack:
            self.output.append(f"</{self.stack.pop()}>")
        return "".join(self.output), " ".join(" ".join(self.text).split())


def slug_for(title: str, article_id: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).translate(ARABIC_TRANSLITERATION)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "article"
    suffix = hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:70].rstrip('-')}-{suffix}"


def article_datetime(article: dict) -> datetime | None:
    timestamp = article.get("timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        try:
            return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def make_summary(article: dict, plain_content: str) -> str:
    candidate = article.get("summary")
    summary = " ".join(str(candidate or plain_content).split())
    if len(summary) <= 160:
        return summary
    shortened = summary[:157].rsplit(" ", 1)[0].rstrip(" ،؛:") or summary[:157]
    return shortened + "..."


def json_for_html(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render_article_page(article_id: str, article: dict) -> tuple[str, str, str, datetime | None]:
    title = str(article.get("title") or "").strip()
    raw_content = str(article.get("content") or "").strip()
    if not title:
        raise ValueError("missing title")
    if not raw_content:
        raise ValueError("missing content")

    sanitizer = SafeArticleHTML()
    sanitizer.feed(raw_content)
    content, plain_content = sanitizer.finish()
    if not plain_content:
        raise ValueError("content contains no readable text")

    slug = slug_for(title, article_id)
    page_path = f"articles/posts/{slug}/"
    canonical = f"{BASE_URL}/{page_path}"
    summary = make_summary(article, plain_content)
    published = article_datetime(article)
    iso_date = published.date().isoformat() if published else None
    image = safe_url(str(article.get("image") or ""), image=True)
    if image and image.startswith("/"):
        image = urljoin(f"{BASE_URL}/", image.lstrip("/"))

    metadata = [
        f"<title>{html.escape(title)} | سهم الذهب</title>",
        f'<meta name="description" content="{html.escape(summary, quote=True)}" />',
        '<meta name="robots" content="index, follow, max-image-preview:large" />',
        f'<link rel="canonical" href="{html.escape(canonical, quote=True)}" />',
        '<meta property="og:type" content="article" />',
        '<meta property="og:site_name" content="سهم الذهب" />',
        '<meta property="og:locale" content="ar_EG" />',
        f'<meta property="og:title" content="{html.escape(title, quote=True)}" />',
        f'<meta property="og:description" content="{html.escape(summary, quote=True)}" />',
        f'<meta property="og:url" content="{html.escape(canonical, quote=True)}" />',
    ]
    if image:
        metadata.append(f'<meta property="og:image" content="{html.escape(image, quote=True)}" />')

    structured_data = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": summary,
        "author": {"@type": "Organization", "name": "سهم الذهب"},
        "publisher": {"@id": f"{BASE_URL}/#organization"},
        "mainEntityOfPage": canonical,
        "inLanguage": "ar",
    }
    if iso_date:
        structured_data["datePublished"] = iso_date
        structured_data["dateModified"] = iso_date
    if image:
        structured_data["image"] = image
    metadata.append(
        '<script type="application/ld+json">'
        f"{json_for_html(structured_data)}</script>"
    )

    image_markup = ""
    if image:
        image_markup = (
            f'<img class="cover" src="{html.escape(image, quote=True)}" '
            f'alt="{html.escape(title, quote=True)}" />'
        )
    date_markup = ""
    if iso_date:
        date_markup = (
            f'<p class="meta"><time datetime="{iso_date}">{iso_date}</time></p>'
        )
    page = f"""<!doctype html>
<html lang="ar" dir="rtl">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    {'    '.join(metadata)}
    <style>
      :root{{color-scheme:dark;--bg:#090f1c;--panel:#0f172a;--text:#f8fafc;--muted:#94a3b8;--accent:#06b6d4;--border:rgba(148,163,184,.2)}}
      *{{box-sizing:border-box}}body{{margin:0;font-family:"Segoe UI",Tahoma,Geneva,Verdana,sans-serif;background:linear-gradient(135deg,#090f1c,#05070c);color:var(--text);line-height:1.9}}a{{color:var(--accent)}}.container{{max-width:900px;margin:0 auto;padding:40px 20px 80px}}.article{{background:rgba(15,23,42,.92);border:1px solid var(--border);border-radius:24px;padding:24px 24px 32px;box-shadow:0 12px 40px rgba(0,0,0,.2)}}.cover{{width:100%;height:auto;border-radius:18px;display:block;margin:18px 0}}.meta{{color:var(--muted);font-size:14px}}h1{{font-size:clamp(2rem,4vw,2.6rem);line-height:1.5}}h2{{font-size:1.4rem;margin-top:1.6em}}.content img{{max-width:100%;height:auto}}.content a{{overflow-wrap:anywhere}}
    </style>
  </head>
  <body>
    <main class="container">
      <a href="/articles/">← العودة إلى مقالات سهم الذهب</a>
      <article class="article">
        {date_markup}
        <h1>{html.escape(title)}</h1>
        {image_markup}
        <div class="content">{content}</div>
      </article>
    </main>
  </body>
</html>
"""
    return slug, page_path, page, published


def render_card(title: str, summary: str, image: str | None, href: str) -> str:
    image_markup = ""
    if image:
        safe_image = image if image.startswith("http") else urljoin(f"{BASE_URL}/", image.lstrip("/"))
        image_markup = (
            f'<img src="{html.escape(safe_image, quote=True)}" '
            f'alt="{html.escape(title, quote=True)}" loading="lazy" />'
        )
    return f"""<article class="card">
  <a href="{html.escape(href, quote=True)}">{image_markup}</a>
  <div class="content">
    <span class="eyebrow">مقال جديد</span>
    <h2><a href="{html.escape(href, quote=True)}">{html.escape(title)}</a></h2>
    <p class="muted">{html.escape(summary)}</p>
  </div>
</article>"""


def update_article_index(index_path: Path, cards: list[str]) -> None:
    source = index_path.read_text(encoding="utf-8")
    if source.count(START_MARKER) != 1 or source.count(END_MARKER) != 1:
        raise ValueError(f"{index_path} must contain one pair of generated article markers")
    start = source.index(START_MARKER) + len(START_MARKER)
    end = source.index(END_MARKER)
    if end < start:
        raise ValueError("article index markers are in the wrong order")
    generated = "\n" + "\n".join(cards) + "\n" if cards else "\n"
    index_path.write_text(source[:start] + generated + source[end:], encoding="utf-8")


def update_sitemap(sitemap_path: Path, entries: list[tuple[str, datetime | None]]) -> None:
    tree = ET.parse(sitemap_path)
    root = tree.getroot()
    if root.tag != f"{{{SITEMAP_NS}}}urlset":
        raise ValueError("sitemap root must be a sitemap.org urlset")
    ET.register_namespace("", SITEMAP_NS)

    loc_tag = f"{{{SITEMAP_NS}}}loc"
    url_tag = f"{{{SITEMAP_NS}}}url"
    generated_prefix = f"{BASE_URL}/articles/posts/"
    for url_element in list(root):
        loc = url_element.find(loc_tag)
        if loc is not None and (loc.text or "").startswith(generated_prefix):
            root.remove(url_element)

    for path, published in entries:
        url_element = ET.SubElement(root, url_tag)
        ET.SubElement(url_element, loc_tag).text = f"{BASE_URL}/{path}"
        if published:
            ET.SubElement(url_element, f"{{{SITEMAP_NS}}}lastmod").text = published.date().isoformat()

    ET.indent(tree, space="  ")
    tree.write(sitemap_path, encoding="UTF-8", xml_declaration=True)


def generate_site(site_root: Path, articles: object) -> int:
    if articles is None:
        article_map: dict[str, dict] = {}
    elif isinstance(articles, dict):
        article_map = articles
    else:
        raise ValueError("Firebase articles value must be a JSON object or null")

    output_dir = site_root / "articles" / "posts"
    if output_dir.exists():
        if output_dir.resolve().parent != (site_root / "articles").resolve():
            raise ValueError("refusing to clean an unexpected generated-articles path")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rendered_cards: list[tuple[datetime, str, str]] = []
    sitemap_entries: list[tuple[str, datetime | None]] = []
    for article_id, article in article_map.items():
        if not isinstance(article, dict):
            raise ValueError(f"article {article_id!r} must be a JSON object")
        try:
            slug, page_path, page, published = render_article_page(str(article_id), article)
        except ValueError as error:
            raise ValueError(f"article {article_id!r}: {error}") from error
        page_dir = output_dir / slug
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(page, encoding="utf-8")
        timestamp = published or datetime.min.replace(tzinfo=timezone.utc)
        summary_parser = SafeArticleHTML()
        summary_parser.feed(str(article.get("summary") or article.get("content") or ""))
        _, plain_summary = summary_parser.finish()
        summary = make_summary({"summary": plain_summary}, plain_summary)
        image = safe_url(str(article.get("image") or ""), image=True)
        rendered_cards.append((
            timestamp,
            str(article.get("title") or "").strip(),
            render_card(str(article.get("title") or "").strip(), summary, image, f"posts/{slug}/"),
        ))
        sitemap_entries.append((page_path, published))

    rendered_cards.sort(key=lambda item: item[0], reverse=True)
    update_article_index(
        site_root / "articles" / "index.html",
        [card for _, _, card in rendered_cards],
    )
    update_sitemap(site_root / "sitemap.xml", sitemap_entries)
    print(f"Generated {len(sitemap_entries)} static article page(s).")
    return len(sitemap_entries)


def fetch_articles(database_url: str) -> object:
    endpoint = database_url.rstrip("/") + "/articles.json"
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "Sahm-Al-Dhahab-SEO-Publisher/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Firebase returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Firebase returned HTTP {error.code} while reading articles") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not connect to Firebase articles: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Firebase articles response was not valid JSON") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="site root containing articles/index.html and sitemap.xml",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        help="read an articles JSON fixture instead of contacting Firebase",
    )
    args = parser.parse_args()
    try:
        if args.data_file:
            articles = json.loads(args.data_file.read_text(encoding="utf-8"))
        else:
            database_url = os.environ.get("FIREBASE_DATABASE_URL", FIREBASE_DATABASE_URL)
            articles = fetch_articles(database_url)
        generate_site(args.site_root.resolve(), articles)
    except (OSError, ValueError, RuntimeError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"Article generation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
