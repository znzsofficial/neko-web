import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
import httpx

from images import FetchedImage, HtmlPage, ImageFetchError, make_policy
from preview import PreviewPager


class PagerTests(IsolatedAsyncioTestCase):
    async def test_batches_dedupe_and_scope(self):
        pager = PreviewPager()
        urls = [f'https://example.com/{n}' for n in range(10)]
        token = pager.create('chat-a', urls + [urls[0] + '#fragment'])
        async def load(client, url, policy):
            if url.endswith('/5'):
                raise ImageFetchError('blocked')
            data = b'duplicate' if url.endswith(('/2', '/3')) else url.encode()
            return FetchedImage(data, 'image/png', url)
        with patch('preview.load_image', side_effect=load) as mock:
            with self.assertRaises(ImageFetchError):
                await pager.batch(None, 'chat-b', token, make_policy())
            first = await pager.batch(None, 'chat-a', token, make_policy())
            self.assertEqual(len(first[0]), 4)
            self.assertTrue(first[2])
            with self.assertRaises(ImageFetchError):
                await pager.batch(None, 'chat-a', token, make_policy())
            second = await pager.batch(None, 'chat-a', first[2], make_policy())
        self.assertEqual(mock.call_count, 10)
        self.assertEqual(len(second[0]), 4)
        self.assertFalse(second[2])
        self.assertTrue(any(d['status'] == 'duplicate' for d in first[1]))
        self.assertTrue(any(d['status'] == 'failed' for d in second[1]))

    async def test_page_only_discovered_once_and_fetch_is_lazy(self):
        pager = PreviewPager()
        token = pager.create('a', ['https://example.com/page'])
        calls = []
        async def load(client, url, policy):
            calls.append(url)
            if url.endswith('page'):
                raise HtmlPage(''.join(f'<img src="/{i}.png">' for i in range(9)).encode(), url)
            return FetchedImage(url.encode(), 'image/png', url)
        with patch('preview.load_image', side_effect=load):
            first = await pager.batch(None, 'a', token, make_policy())
            self.assertEqual(len(calls), 5)
            second = await pager.batch(None, 'a', first[2], make_policy())
            third = await pager.batch(None, 'a', second[2], make_policy())
        self.assertEqual([len(b[0]) for b in (first, second, third)], [4, 4, 1])
        self.assertEqual(calls.count('https://example.com/page'), 1)

    async def test_expiration_capacity_and_concurrency(self):
        now = [0]
        pager = PreviewPager(clock=lambda: now[0])
        token = pager.create('a', ['https://example.com/image'])
        now[0] = 601
        with self.assertRaises(ImageFetchError):
            await pager.batch(None, 'a', token, make_policy())
        for _ in range(6):
            pager.create('a', [])
        self.assertEqual(len(pager.queues), 4)
        for i in range(70):
            pager.create(str(i), [])
        self.assertEqual(len(pager.queues), 64)
        token = pager.create('a', ['https://example.com/image'])
        async def load(*args):
            await asyncio.sleep(0)
            return FetchedImage(b'a', 'image/png', 'https://example.com/image')
        with patch('preview.load_image', side_effect=load):
            results = await asyncio.gather(*(pager.batch(None, 'a', token, make_policy()) for _ in range(2)), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ImageFetchError) for r in results), 1)

    async def test_failed_batch_still_has_cursor_and_caps_attempts(self):
        pager = PreviewPager()
        token = pager.create('a', [f'https://example.com/{n}' for n in range(20)])
        with patch('preview.load_image', side_effect=ImageFetchError('blocked')) as mock:
            batch = await pager.batch(None, 'a', token, make_policy())
        self.assertEqual(mock.call_count, 12)
        self.assertEqual(len(batch[1]), 12)
        self.assertTrue(batch[2])
        self.assertEqual(batch[3], 8)

    async def test_continuation_still_rejects_private_addresses(self):
        pager = PreviewPager()
        token = pager.create('a', ['https://example.com/ok', 'http://127.0.0.1/secret',
                                 'https://example.com/redirect'])
        calls = []
        def transport(request):
            calls.append(str(request.url))
            if request.url.path == '/redirect':
                return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest'})
            return httpx.Response(200, content=b'\x89PNG\r\n\x1a\nfixture', headers={'content-type': 'image/png'})
        policy = make_policy(max_images=1, resolver=lambda host: ['1.1.1.1'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            first = await pager.batch(client, 'a', token, policy)
            self.assertEqual(len(first[0]), 1)
            last = await pager.batch(client, 'a', first[2], policy)
        self.assertFalse(last[0])
        self.assertEqual(len(calls), 2)
        self.assertEqual([d['status'] for d in last[1]], ['failed', 'failed'])
