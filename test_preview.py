"""Exercise actual tool methods without importing the plugin SDK on Windows."""
import ast
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from urllib.parse import urlparse

from images import FetchedImage


class PreviewTests(IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).with_name('plugin.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'NekoWebPlugin')
        names = {'_image_preview_result', '_preview_fetched_images', 'handle_read', 'handle_fetch_images'}
        cls.bases = []
        cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        for n in cls.body:
            if n.name != '_image_preview_result':
                n.decorator_list = []
        module = ast.Module(body=[cls], type_ignores=[])
        self.ns = dict(base64=base64, Any=Any, List=List, urlparse=urlparse,
                       httpx=SimpleNamespace(AsyncClient=object))
        exec(compile(ast.fix_missing_locations(module), 'plugin.py', 'exec'), self.ns)
        self.plugin = self.ns['NekoWebPlugin']()
        self.plugin._check_enabled = lambda: None
        self.plugin._fetch_policy = lambda: None
        self.plugin._text_limit = lambda: 8000
        async def run(action, failure):
            return await action(None)
        self.plugin._run_public = run
        # Any accidental send will fail: no ctx or sender is supplied.
        self.image = FetchedImage(b'fixture-image', 'image/png', 'https://example.com/image.png')

    async def test_images_only_return_media_no_send(self):
        self.ns['collect_images'] = AsyncMock(return_value=([self.image], ['another URL failed']))
        result = await self.plugin.handle_fetch_images(['https://example.com/image.png'], stream_id='test')
        self.assertTrue(result['success'])
        self.assertIn('尚未发送', result['content'])
        self.assertIn('another URL failed', result['content'])
        self.assertIn('media_index', result['content'])
        item, = result['content_items']
        self.assertEqual(base64.b64decode(item['data']), self.image.data)
        self.assertEqual(item['mime_type'], self.image.mime)

    async def test_page_keeps_text_and_candidate_media(self):
        self.ns['read_public_page'] = AsyncMock(return_value=SimpleNamespace(
            title='Article', text='Body', images=[self.image], notes=[]))
        result = await self.plugin.handle_read('https://example.com/article', stream_id='test')
        self.assertIn('Body', result['content'])
        self.assertIn('尚未发送', result['content'])
        self.assertEqual(len(result['content_items']), 1)

    async def test_empty_images_fail_but_text_only_page_succeeds(self):
        self.ns['collect_images'] = AsyncMock(return_value=([], ['download failed']))
        result = await self.plugin.handle_fetch_images(['https://example.com/image'], stream_id='test')
        self.assertFalse(result['success'])
        self.assertEqual(result['content_items'], [])
        self.ns['read_public_page'] = AsyncMock(return_value=SimpleNamespace(
            title='Article', text='Body', images=[], notes=[]))
        page = await self.plugin.handle_read('https://example.com/article', stream_id='test')
        self.assertTrue(page['success'])
        self.assertIn('Body', page['content'])
