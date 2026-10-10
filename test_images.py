import unittest

import httpx

from images import (
    ImageFetchError,
    collect_images,
    image_urls_in_document,
    sniff_image,
    validate_url,
)

from public_http import is_public_ip
from test_support import image_bytes

PNG = image_bytes()


def public_resolver(host: str) -> list[str]:
    if host in {"127.0.0.1", "10.1.2.3", "169.254.169.254"}:
        return [host]
    if host.endswith(".test"):
        return ["1.1.1.1"]
    raise ImageFetchError("无法解析域名")


class AddressTests(unittest.TestCase):
    def test_rejects_local_and_odd_ports(self):
        self.assertIsNone(validate_url("https://cdn.test/a.png"))
        self.assertIsNotNone(validate_url("http://127.0.0.1/a.png"))
        self.assertIsNotNone(validate_url("http://10.1.2.3/a.png"))
        self.assertIsNotNone(validate_url("http://169.254.169.254/latest/meta-data"))
        self.assertIsNotNone(validate_url("http://localhost/a.png"))
        self.assertIsNotNone(validate_url("https://cdn.test:8443/a.png"))
        self.assertIsNotNone(validate_url("https://user:pass@cdn.test/a.png"))
        self.assertIsNotNone(validate_url("file:///etc/passwd"))
        self.assertFalse(is_public_ip("192.168.1.1"))
        self.assertTrue(is_public_ip("1.1.1.1"))

    def test_sniff_and_page_images(self):
        self.assertEqual(sniff_image(PNG), "image/png")
        self.assertIsNone(sniff_image(b"<svg></svg>"))
        html = """
        <meta property="og:image" content="https://cdn.test/cover.png">
        <img src="/pixel.gif" width="1" height="1">
        <img src="/photo.jpg">
        <img src="logo.svg">
        ![alt](https://cdn.test/md.webp)
        """
        self.assertEqual(
            image_urls_in_document(html, "https://news.test/post"),
            [
                "https://cdn.test/cover.png",
                "https://news.test/photo.jpg",
                "https://cdn.test/md.webp",
            ],
        )


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_to_loopback_is_not_followed(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers['host'])
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret.png"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            images, notes = await collect_images(client, ["https://page.test/go"], resolver=public_resolver)
        self.assertEqual(images, [])
        self.assertEqual(seen, ["page.test"])
        self.assertIn("内网", notes[0])

    async def test_page_og_image_is_downloaded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers['host'] == "news.test":
                html = b'<meta property="og:image" content="https://cdn.test/cover.png">'
                return httpx.Response(200, content=html, headers={"content-type": "text/html"})
            if request.url.path == "/cover.png":
                return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            images, notes = await collect_images(client, ["https://news.test/post"], resolver=public_resolver)
        self.assertEqual(notes, [])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mime, "image/jpeg")
        self.assertEqual(images[0].original_size, len(PNG))

    async def test_oversized_image_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"\xff\xd8\xff" + b"x" * 50, headers={"content-type": "image/jpeg"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            images, notes = await collect_images(
                client,
                ["https://cdn.test/big.jpg"],
                resolver=public_resolver,
                max_image_bytes=20,
            )
        self.assertEqual(images, [])
        self.assertIn("大小", notes[0])


if __name__ == "__main__":
    unittest.main()
