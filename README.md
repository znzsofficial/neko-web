# Neko Web

给 [MaiBot](https://github.com/Mai-with-u/MaiBot) 用的联网插件。搜索公开网页，读取标题、正文和候选图片，由麦麦看图挑选后随回复发送。

搜索走 [AnySearch](https://anysearch.com)。打开链接和下载图片由插件自己完成。搜索客户端的早期实现来自 [gomico/maibot_plugin_anysearch](https://github.com/gomico/maibot_plugin_anysearch)，该项目以 MIT 许可证发布。

## 安装

把这个目录放到 MaiBot 的 `plugins/neko-web`，复制 `config.example.toml` 为 `config.toml`，然后在 WebUI 里启用。

`api_key` 可以留空，匿名访问的额度更低。需要代理时，把 `proxy_mode` 改成 `custom`，并填写 `proxy_url`。不要把填了密钥的 `config.toml` 提交到仓库。

依赖 `httpx >= 0.26.0`、`aiohttp >= 3.12`。MaiBot 1.3.5 及以上，SDK 2.7 及以上；直接看图需要启用 Planner 视觉并配置支持图片的模型。

## 工具

| 工具 | 作用 |
| --- | --- |
| `neko_web_search` | 搜索。垂直领域要先查 `neko_web_domains`，不要自己编 `sub_domain`。 |
| `neko_web_batch_search` | 一次并行搜索 1–5 个问题。 |
| `neko_web_domains` | 查询垂直领域的子领域和参数。 |
| `neko_web_read` | 打开一个公开链接，返回标题、正文和候选配图。 |
| `neko_web_images` | 下载图片或网页配图，仅供麦麦预览挑选。 |
| `neko_web_extract` | 用 AnySearch 提取长文。用户只是丢来一个链接时，优先用 `neko_web_read`。 |

图片通过工具结果的 `content_items` 交给麦麦，此时用户还没有收到图片。麦麦看过后挑选，在 `reply.attach_pic` 中使用宿主生成的 `media_index` 发送；可以少选或不选，不必凑满上限。正文和图片都是外部资料，不能当成指令。

MaiBot 1.3 不会把插件工具直接放进可调用列表。规划器先看到「名字：描述」，再通过 `tool_search` 按名字和描述检索。因此工具名用 `neko_web_` 当命名空间即可，用途写在中文描述里。不要把工具名改成中文。

## 限制

- 只接受 `http` 和 `https` 的 80 / 443 端口。
- 拒绝内网、本机、云元数据、带账号密码的地址，以及跳到这些地址的重定向。
- 请求前解析并固定公网 IP；代理 CONNECT 也使用该 IP，保留原域名的 Host、TLS SNI 和证书校验。解析失败就拒绝，不让代理再次解析目标域名。公网下载支持 HTTP/HTTPS 代理，SOCKS 仅用于搜索接口；使用 mihomo 时填写 HTTP mixed-port。
- 页面中损坏的图片 URL 会跳过，不影响正文或其他图片。
- 图片只收 JPEG、PNG、GIF、WebP。默认一次最多预览 4 张，单张 8 MB；这是候选批次上限，不是每次要发送的数量。
- 返回给模型的正文默认不超过 8000 字。
- 不执行页面脚本，也读不了需要登录的内容。

## 开发

`public_http.py` 与 `neko-draw` 保持相同副本，独立安装时不互相依赖。修复下载安全逻辑时同步两份，并运行公网下载回归测试。

```bash
python -m unittest
```
