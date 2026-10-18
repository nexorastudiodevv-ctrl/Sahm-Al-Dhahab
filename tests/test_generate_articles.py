import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.generate_articles import (
    SITEMAP_NS,
    SafeArticleHTML,
    generate_site,
    slug_for,
)


class GenerateArticlesTests(unittest.TestCase):
    def test_slug_is_stable_readable_and_unique_per_firebase_id(self):
        first = slug_for("سعر الذهب في مصر", "first-id")
        self.assertRegex(first, r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{10}$")
        self.assertNotEqual(first, slug_for("سعر الذهب في مصر", "second-id"))
        self.assertEqual(first, slug_for("سعر الذهب في مصر", "first-id"))

    def test_sanitizer_removes_executable_markup_and_keeps_article_formatting(self):
        parser = SafeArticleHTML()
        parser.feed(
            '<h2 onclick="alert(1)">عنوان</h2>'
            '<p>نص <strong>مهم</strong> '
            '<a href="javascript:alert(1)">رابط</a></p>'
            '<script>alert(2)</script>'
        )
        content, plain_text = parser.finish()
        self.assertIn("<h2>عنوان</h2>", content)
        self.assertIn("<strong>مهم</strong>", content)
        self.assertIn("<a>رابط</a>", content)
        self.assertNotIn("onclick", content)
        self.assertNotIn("javascript:", content)
        self.assertNotIn("alert", content)
        self.assertIn("عنوان", plain_text)

    def test_generates_static_page_index_card_and_sitemap_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article_dir = root / "articles"
            article_dir.mkdir()
            (article_dir / "index.html").write_text(
                "<main><!-- PUBLISHED_ARTICLES_START -->"
                "<!-- PUBLISHED_ARTICLES_END --></main>",
                encoding="utf-8",
            )
            sitemap_path = root / "sitemap.xml"
            sitemap_path.write_text(
                f'<?xml version="1.0"?><urlset xmlns="{SITEMAP_NS}">'
                f"<url><loc>https://nexorastudio-eg.com/</loc></url></urlset>",
                encoding="utf-8",
            )
            entries = {
                "firebase-id-123": {
                    "title": 'عنوان "مهم" <اختبار>',
                    "content": "<h2>التفاصيل</h2><p>محتوى مفيد.</p>",
                    "summary": "ملخص المقال.",
                    "image": "https://example.com/gold.jpg",
                    "timestamp": 1792340000000,
                }
            }

            self.assertEqual(generate_site(root, entries), 1)
            slug = slug_for(entries["firebase-id-123"]["title"], "firebase-id-123")
            page_path = article_dir / "posts" / slug / "index.html"
            page = page_path.read_text(encoding="utf-8")
            self.assertIn("<meta name=\"robots\" content=\"index, follow", page)
            self.assertIn('rel="canonical" href="https://nexorastudio-eg.com/articles/posts/', page)
            self.assertIn('"@type":"Article"', page)
            self.assertIn('content="سهم الذهب"', page)
            self.assertIn("| سهم الذهب</title>", page)
            self.assertIn('"name":"سهم الذهب"', page)
            self.assertIn("&lt;اختبار&gt;", page)
            self.assertIn("<h2>التفاصيل</h2>", page)

            index = (article_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn(f'href="posts/{slug}/"', index)
            sitemap = ET.parse(sitemap_path).getroot()
            locations = [
                item.text
                for item in sitemap.findall(f"{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc")
            ]
            self.assertIn(f"https://nexorastudio-eg.com/articles/posts/{slug}/", locations)
            self.assertIn("https://nexorastudio-eg.com/", locations)

    def test_generated_sitemap_replaces_stale_generated_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article_dir = root / "articles"
            article_dir.mkdir()
            (article_dir / "index.html").write_text(
                "<main><!-- PUBLISHED_ARTICLES_START -->"
                "<!-- PUBLISHED_ARTICLES_END --></main>",
                encoding="utf-8",
            )
            sitemap_path = root / "sitemap.xml"
            sitemap_path.write_text(
                f'<urlset xmlns="{SITEMAP_NS}">'
                "<url><loc>https://nexorastudio-eg.com/articles/posts/deleted/</loc></url>"
                "<url><loc>https://nexorastudio-eg.com/</loc></url></urlset>",
                encoding="utf-8",
            )

            generate_site(root, {})
            sitemap = ET.parse(sitemap_path).getroot()
            locations = [
                item.text
                for item in sitemap.findall(f"{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc")
            ]
            self.assertNotIn(
                "https://nexorastudio-eg.com/articles/posts/deleted/",
                locations,
            )
            self.assertIn("https://nexorastudio-eg.com/", locations)


if __name__ == "__main__":
    unittest.main()
