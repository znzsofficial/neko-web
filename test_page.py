import unittest

from page import page_text

HTML = """
<!doctype html>
<html>
<head>
  <title>示例 文章</title>
  <meta name="description" content="这是摘要">
  <meta property="og:image" content="https://cdn.test/cover.png">
  <script>secret = "不要出现"</script>
  <style>.x { color: red }</style>
</head>
<body>
  <article><h1>标题</h1><p>第一段正文。</p></article>
</body>
</html>
"""


class PageTextTests(unittest.TestCase):
    def test_html_drops_script_and_keeps_title(self):
        title, text = page_text(HTML.encode(), "text/html; charset=utf-8", 8000)
        self.assertEqual(title, "示例 文章")
        self.assertNotIn("这是摘要", text)
        self.assertIn("第一段正文", text)
        self.assertNotIn("不要出现", text)
        self.assertNotIn("color", text)

    def test_text_is_truncated(self):
        title, text = page_text("<p>一二三四五</p>".encode(), "text/html", 4)
        self.assertEqual(title, "")
        self.assertIn("…（正文已截断）", text)
        self.assertNotIn("五", text)

    def test_json_is_pretty(self):
        title, text = page_text('{"name":"麦麦"}'.encode(), "application/json", 8000)
        self.assertEqual(title, "")
        self.assertIn("麦麦", text)
        self.assertIn("\n", text)


if __name__ == "__main__":
    unittest.main()
