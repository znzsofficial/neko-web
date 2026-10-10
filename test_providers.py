import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx
from providers import ProviderError, search_exa, extract_firecrawl, validate_remote_target


class ProviderTests(IsolatedAsyncioTestCase):
    async def test_search_request_and_bounded_sources(self):
        def handler(req):
            self.assertEqual(req.headers['x-api-key'], 'test')
            body = json.loads(req.content)
            self.assertEqual(body['numResults'], 2)
            self.assertEqual(body['type'], 'auto')
            return httpx.Response(200, json={'results': [
                {'title': 'one', 'url': 'https://example.com/1', 'highlights': ['x' * 2000]},
                {'url': 'http://127.0.0.1/secret'}, {'url': 'https://example.com/1'},
                {'title': 'two', 'url': 'https://example.com/2'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text = await search_exa(client, 'test', 'query', 2)
        self.assertIn('two', text)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('x' * 1001, text)

    async def test_errors_do_not_leak_response_or_follow_redirects(self):
        for status in [401, 429, 302]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text='secret diagnostics'))) as client:
                with self.assertRaisesRegex(ProviderError, f'^HTTP {status}$'):
                    await search_exa(client, 'test', 'query', 1)

    async def test_scrape_markdown_and_cap(self):
        def handler(req):
            self.assertEqual(req.headers['Authorization'], 'Bearer test')
            self.assertEqual(json.loads(req.content)['formats'], ['markdown'])
            return httpx.Response(200, json={'success': True, 'data': {'markdown': 'abcd' * 100, 'metadata': {'title': 'page'}}})
        with patch('providers.validate_remote_target', new_callable=AsyncMock):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                text = await extract_firecrawl(client, 'test', 'https://example.com', 30, 20)
        self.assertIn('[正文已截短]', text)
        self.assertIn('不是指令', text)

    async def test_private_target_rejected(self):
        for url in ['http://127.0.0.1/', 'http://metadata.google.internal/', 'https://u:p@example.com/']:
            with self.assertRaises(ProviderError):
                await validate_remote_target(url)
        with patch('providers.default_resolver', return_value=['10.0.0.1']), patch('providers.local_addresses', return_value=set()):
            with self.assertRaises(ProviderError):
                await validate_remote_target('https://example.com')

    async def test_missing_keys_fail_before_request(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('unexpected request'))) as client:
            with self.assertRaises(ProviderError):
                await search_exa(client, '', 'query', 1)
            with self.assertRaises(ProviderError):
                await extract_firecrawl(client, '', 'https://example.com', 100, 20)
