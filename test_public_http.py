import unittest
from unittest.mock import patch

import httpx

from images import collect_images, validate_url
from page import read_public_page
from public_http import PublicClient, download_public, is_public_ip
from test_images import PNG


class PublicDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_is_pinned_before_request_and_hostname_is_preserved(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, content=PNG)
        with patch('public_http.default_resolver', return_value=['1.1.1.1']) as resolve:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                _, _, final = await download_public(client, 'https://cdn.test/a.png')
        resolve.assert_called_once_with('cdn.test')
        self.assertEqual(seen[0].url.host, '1.1.1.1')
        self.assertEqual(seen[0].headers['Host'], 'cdn.test')
        self.assertEqual(seen[0].extensions['sni_hostname'], 'cdn.test')
        self.assertEqual(final, 'https://cdn.test/a.png')

    async def test_private_dns_and_unresolved_proxy_targets_never_send(self):
        def handler(request):
            self.fail('request sent before address validation')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for ips in ([], ['1.1.1.1', '127.0.0.1']):
                images, notes = await collect_images(client, ['https://dns.test/x'],
                    resolver=lambda host, ips=ips: ips, allow_unresolved=True, check_peer=False)
                self.assertFalse(images)
                self.assertTrue(notes)

    async def test_relative_redirect_keeps_original_host(self):
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path == '/a':
                return httpx.Response(302, headers={'location': '/b'})
            return httpx.Response(200, content=PNG)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _, _, final = await download_public(client, 'https://cdn.test/a', resolver=lambda host:['1.1.1.1'])
        self.assertEqual(final, 'https://cdn.test/b')
        self.assertTrue(all(r.headers['Host'] == 'cdn.test' for r in seen))

    async def test_bad_image_urls_do_not_discard_body_or_valid_images(self):
        def handler(request):
            if request.url.path == '/good.png':
                return httpx.Response(200, content=PNG)
            return httpx.Response(200, headers={'content-type':'text/html'}, content=b'''
                <title>Valid page</title><p>Useful body</p>
                <img src="https://cdn.test:bad/a"><img src="http://[broken">
                <img src="/good.png">''')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await read_public_page(client, 'https://page.test/', resolver=lambda host:['1.1.1.1'])
        self.assertEqual(result.title, 'Valid page')
        self.assertIn('Useful body', result.text)
        self.assertEqual(len(result.images), 1)

    async def test_proxy_transport_uses_ip_and_original_tls_name(self):
        class Content:
            async def iter_chunked(self, size): yield b'body'
        class Response:
            status = 200
            headers = {}
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        captured = {}
        class Session:
            def request(self, method, url, **kwargs):
                captured.update(url=url, **kwargs)
                return Response()
        client = PublicClient(proxy='http://127.0.0.1:7890')
        client.session = Session()
        await download_public(client, 'https://cdn.test/image', resolver=lambda host:['1.1.1.1'])
        self.assertEqual(captured['url'], 'https://1.1.1.1/image')
        self.assertEqual(captured['server_hostname'], 'cdn.test')
        self.assertEqual(captured['headers']['Host'], 'cdn.test')
        self.assertIs(captured['ssl'], True)
        self.assertIs(captured['allow_redirects'], False)
        self.assertEqual(captured['proxy'], 'http://127.0.0.1:7890')

    def test_malformed_and_multicast_addresses_are_rejected(self):
        for url in ('http://[broken', 'https://cdn.test:bad/a', 'http://224.0.0.1/', 'http://[ff02::1]/'):
            self.assertIsNotNone(validate_url(url))
        self.assertFalse(is_public_ip('224.0.0.1'))
        self.assertFalse(is_public_ip('::ffff:127.0.0.1'))
