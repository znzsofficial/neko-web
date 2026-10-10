"""Chat-scoped, bounded metadata for selected originals. Never retain pixels."""
import secrets
import time
from dataclasses import dataclass


@dataclass
class Original:
    scope: str
    url: str
    digest: str
    width: int
    height: int
    expires: float
    state: str = 'ready'


class OriginalRegistry:
    TTL = 600
    MAX_ITEMS = 128
    MAX_PER_CHAT = 24

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.items = {}

    def clear(self):
        self.items.clear()

    def prune(self):
        for key, item in list(self.items.items()):
            if item.expires <= self.clock() and item.state != 'sending':
                del self.items[key]

    def add(self, scope, image):
        self.prune()
        for key, item in self.items.items():
            if item.scope == scope and item.url == image.source and item.digest == image.original_sha256 and item.state == 'ready':
                return key
        same = [key for key, item in self.items.items() if item.scope == scope and item.state != 'sending']
        while len([item for item in self.items.values() if item.scope == scope]) >= self.MAX_PER_CHAT:
            if not same:
                raise ValueError('本聊天原图任务已满')
            del self.items[same.pop(0)]
        while len(self.items) >= self.MAX_ITEMS:
            key = next((k for k, item in self.items.items() if item.state != 'sending'), None)
            if key is None:
                raise ValueError('原图任务已满')
            del self.items[key]
        token = secrets.token_urlsafe(18)
        self.items[token] = Original(scope, image.source, image.original_sha256,
                                     image.width, image.height, self.clock() + self.TTL)
        return token

    def claim(self, scope, token):
        self.prune()
        item = self.items.get(token)
        if item is None or item.scope != scope or item.expires <= self.clock():
            raise ValueError('图片编号无效或已过期，请重新预览')
        if item.state != 'ready':
            raise ValueError('该图片已发送、正在发送或投递状态未确认，不自动重复发送')
        item.state = 'sending'
        return item
