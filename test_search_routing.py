import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from retrieval import search_filters, bounded_batch, MAX_READ_PAGES


class RoutingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).with_name('plugin.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'NekoWebPlugin')
        names = {'handle_search', 'handle_batch_search', '_vertical_arguments', '_parse_params'}
        cls.bases = []
        cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        for node in cls.body:
            if node.name not in {'_vertical_arguments', '_parse_params'}:
                node.decorator_list = []
        ns = dict(Any=Any, List=List, json=json, asyncio=asyncio, search_filters=search_filters,
                  bounded_batch=bounded_batch, MAX_READ_PAGES=MAX_READ_PAGES, VERTICAL_DOMAINS={'finance', 'general'})
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), 'plugin.py', 'exec'), ns)
        self.p = ns['NekoWebPlugin']()
        self.p.config = SimpleNamespace(web=SimpleNamespace(search_provider='exa', default_max_results=5, timeout_seconds=2))
        self.p._check_enabled = lambda: None
        self.p._search_slots = asyncio.Semaphore(2)
        self.p._provider_search = AsyncMock(return_value='results')
        self.p._call_api = AsyncMock(return_value='vertical')

    async def test_filters_and_read_pages_forwarded(self):
        result = await self.p.handle_search('query', sites=['example.com'], published_after='2026-01-01', read_pages=2)
        self.assertEqual(result, 'results')
        self.assertEqual(self.p._provider_search.call_args.args[2]['includeDomains'], ['example.com'])
        self.assertEqual(self.p._provider_search.call_args.args[3], 2)

    async def test_invalid_and_vertical_options_fail_before_network(self):
        for kwargs in [{'read_pages': True}, {'read_pages': 3}, {'sites': ['https://example.com']},
                       {'domain': 'finance', 'sub_domain': 'stock', 'read_pages': 1}]:
            text = await self.p.handle_search('query', **kwargs)
            self.assertTrue('invalid_request' in text or 'unsupported' in text)
        self.p._provider_search.assert_not_called()
        self.p._call_api.assert_not_called()

    async def test_vertical_keeps_anysearch(self):
        self.assertEqual(await self.p.handle_search('query', domain='finance', sub_domain='stock'), 'vertical')
        self.p._provider_search.assert_not_called()

    async def test_duplicate_queries_only_search_once(self):
        text = await self.p.handle_batch_search(['one', ' one ', 'two'])
        self.assertEqual(self.p._provider_search.await_count, 2)
        self.assertIn('查询：two', text)

    async def test_anysearch_does_not_silently_ignore_batch_filters(self):
        self.p.config.web.search_provider = 'anysearch'
        text = await self.p.handle_batch_search(['one'], sites=['example.com'])
        self.assertIn('unsupported', text)
        self.p._provider_search.assert_not_called()
