import unittest

from images import CANDIDATES_PER_PAGE, image_urls_in_document, normalize_image_url


class ImageParserTests(unittest.TestCase):
    base = "https://news.test/articles/post"

    def parse(self, text, limit=8):
        return image_urls_in_document(text, self.base, limit)

    def test_modern_attributes_entities_and_unquoted_values(self):
        self.assertEqual(self.parse('''
            <META property=og:image content="//CDN.test/cover.jpg?a=1&amp;b=2">
            <img src=/placeholder.gif data-src=/lazy.jpg>
            <img data-original="/original.png?a=1&amp;b=2">
            <img data-lazy-src=/other.webp>
            <img title="a > b" src=/plain.gif>
        '''), [
            "https://cdn.test/cover.jpg?a=1&b=2",
            "https://news.test/lazy.jpg",
            "https://news.test/original.png?a=1&b=2",
            "https://news.test/other.webp",
            "https://news.test/plain.gif",
        ])

    def test_one_best_supported_srcset_candidate(self):
        self.assertEqual(self.parse('''
            <img src=/fallback.jpg srcset="/small.jpg 320w, /large.jpg 1200w, /unsupported.avif 2000w">
            <img src=/pixel.gif srcset="/placeholder.jpg 2x"
                 data-srcset="/normal.webp 1x, /retina.webp 2x, /bad.webp nonsense">
            <img srcset="data:image/png;base64,AAAA 3x, /safe.png 1x">
        '''), ["https://news.test/large.jpg", "https://news.test/retina.webp", "https://news.test/safe.png"])

    def test_picture_selects_first_supported_source_and_not_fallback(self):
        self.assertEqual(self.parse('''
            <picture>
                <source type=image/avif srcset="/opaque-format 2000w">
                <source type=image/webp data-srcset="/small.webp 400w, /large.webp 1000w">
                <source type=image/jpeg srcset="/alternative.jpg 1200w">
                <img src=/fallback.jpg>
            </picture>
            <picture><source type=image/svg+xml srcset=/vector>
                <img data-src=/fallback.png></picture>
            <source srcset=/not-in-picture.jpg>
            <img src=/outside.jpg>
        '''), ["https://news.test/large.webp", "https://news.test/fallback.png", "https://news.test/outside.jpg"])

    def test_dedup_normalizes_case_fragments_but_preserves_query(self):
        self.assertEqual(self.parse('''
            <img src="HTTPS://CDN.TEST/Image.jpg?q=One#first">
            <meta name=twitter:image content="https://cdn.test/Image.jpg?q=One#second">
            <img src="https://cdn.test/Image.jpg?q=Two">
            ![duplicate](https://CDN.test/Image.jpg?q=One#third)
        '''), ["https://cdn.test/Image.jpg?q=One", "https://cdn.test/Image.jpg?q=Two"])
        self.assertEqual(normalize_image_url("../Image.jpg?q=A%2FB#part", self.base),
                         "https://news.test/Image.jpg?q=A%2FB")
        self.assertEqual(normalize_image_url("HTTPS://CDN.TEST:443/a?x=1#hash"),
                         "https://cdn.test:443/a?x=1")

    def test_no_inline_scripts_comments_or_attribute_markup(self):
        self.assertEqual(self.parse('''
            <script>const html = '<img src="https://evil.test/script.jpg">';
                const md = '![x](https://evil.test/script.png)';</script>
            <style>/* ![x](https://evil.test/style.jpg) */</style>
            <!-- <img src=/comment.jpg> ![x](https://evil.test/comment.png) -->
            <div data-template='<img src=/attribute.jpg>'></div>
            <textarea><img src=/textarea.jpg></textarea>
            ![visible](https://cdn.test/markdown.webp)
            <img src=/real.png>
        '''), ["https://news.test/real.png", "https://cdn.test/markdown.webp"])

    def test_invalid_urls_and_unsupported_images_are_skipped(self):
        invalid = ["javascript:alert(1)", "data:image/png;base64,AAAA", "file:///tmp/a.png",
                   "http://localhost/a.jpg", "http://127.0.0.1/a.jpg", "http://[::1]/a.jpg",
                   "https://user:pass@cdn.test/a.jpg", "https://cdn.test:8443/a.jpg",
                   "https://[broken/a.jpg", "https://cdn.test/a\n.jpg", "", "https://cdn.test/" + "a" * 2001]
        for url in invalid:
            with self.subTest(url=url):
                self.assertIsNone(normalize_image_url(url))
                self.assertEqual(self.parse(f'<img src="{url}">'), [])
        self.assertEqual(self.parse('''
            <img src=/vector.svg><img src=/movie.mp4><img src=/pixel.gif width=1>
            <img srcset="http://localhost/bad.jpg 4x, /ok.jpg 1x">
            <img data-src="javascript:bad" src=/fallback.jpg>
            ![invalid](http://127.0.0.1/private.png)
        '''), ["https://news.test/ok.jpg", "https://news.test/fallback.jpg"])

    def test_limits_and_unclosed_picture(self):
        html = "".join(f"<img src=/{i}.jpg>" for i in range(100))
        self.assertEqual(CANDIDATES_PER_PAGE, 48)
        self.assertEqual(len(self.parse(html, 1000)), 48)
        self.assertEqual(len(self.parse(html)), 8)
        self.assertEqual(self.parse(html, 0), [])
        self.assertEqual(self.parse(html, -1), [])
        self.assertEqual(self.parse(""), [])
        self.assertEqual(self.parse('<picture><source srcset="/best.jpg 2x"><img src=/fallback.jpg>'),
                         ["https://news.test/best.jpg"])


if __name__ == "__main__":
    unittest.main()
