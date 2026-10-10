# Neko Web

给 [MaiBot](https://github.com/Mai-with-u/MaiBot) 用的联网插件。搜索公开网页，读取标题、正文和候选图片，由麦麦看图挑选后随回复发送。

搜索走 [AnySearch](https://anysearch.com)。打开链接和下载图片由插件自己完成。搜索客户端的早期实现来自 [gomico/maibot_plugin_anysearch](https://github.com/gomico/maibot_plugin_anysearch)，该项目以 MIT 许可证发布。

1.3.0可在 `[web]` 设置 `search_provider="exa"`、`exa_api_key`，普通/批量搜索使用Exa auto＋有来源的摘录，专用领域继续AnySearch。设置 `extract_provider="firecrawl"`、`firecrawl_api_key` 后长文工具走Firecrawl v2 Markdown提取（含动态页），`read`和图片仍走原本公网下载器。默认仍为AnySearch；不自动跨供应商重试、不开deep/crawl/付费代理等额外能力。两家均按服务商额度计费，请自行设额度限制。

Firecrawl提取会把目标URL交给第三方。发送前验证URL及DNS为公网，但远端DNS/重定向由Firecrawl处理，不能宣称具有本地下载器同等IP固定保护；不要用于敏感或带访问令牌的链接。响应限2MiB，正文按配置截短，错误不回显原始响应或密钥。

## 安装

把这个目录放到 MaiBot 的 `plugins/neko-web`，复制 `config.example.toml` 为 `config.toml`，然后在 WebUI 里启用。

`api_key` 可以留空，匿名访问的额度更低。需要代理时，把 `proxy_mode` 改成 `custom`，并填写 `proxy_url`。不要把填了密钥的 `config.toml` 提交到仓库。

依赖 `httpx >= 0.26.0`、`aiohttp >= 3.12`、`Pillow >= 12.0`。MaiBot 1.3.5 及以上，SDK 2.7 及以上；直接看图需要启用 Planner 视觉并配置支持图片的模型。

## 工具

### 搜索增强（1.4.0）

- Exa结果按规范化URL去重：忽略锚点和常见追踪参数，保留有意义的查询参数。不同页面不强行合并；同站结果分层交错，减少单站挤占，不把来源多样性当权威评分。保留标题、真实来源、日期和有界摘录，日期缺失明确标记，不猜“最新”。不会自动追加搜索补满去重后的数量。
- `neko_web_search` 和 `neko_web_batch_search` 可选 `sites`（最多5个域名）、`published_after` / `published_before`（YYYY-MM-DD，含当天，UTC）。只用于Exa普通搜索；AnySearch或专用领域遇到这些参数明确返回unsupported，不静默忽略。发布日期是供应商元数据，不是插件核实过的更新时间。
- 单条搜索可选 `read_pages=0..2`，默认0。开启后读取整理后前1–2个来源的Firecrawl正文，每篇最多4000字；这是额外请求、额外额度，不保证是模型已判定最相关的文章。读原文失败/超时保留搜索证据并标partial；不配置Firecrawl时不发起正文请求。批量搜索不自动读正文，需要时另选来源提取。
- 状态区分ok/no_results/partial、密钥权限、额度限流、参数、超时和供应商异常；无结果不代表事实不存在，摘录不代表已读原文。不自动换源、重搜或改写关键词。
- 普通和批量搜索共用2个并发槽；批量最多5个去重后的问题、整批最多20条（按问题均分每条返回数量），总输出24000字并公平保留每个问题。单条输出16000字、整轮含排队最多95秒，批量总期限最多95秒；取消/超时会清理任务并保留已完成查询。AnySearch原始返回文本不强行假设其结构或伪造来源日期；结构化来源去重适用于Exa。

示例：`neko_web_search(query="MaiBot release notes", sites=["github.com"], published_after="2026-10-01", read_pages=1)`。不填写新参数时仍为仅搜索，不触发原文抓取。

| 工具 | 作用 |
| --- | --- |
| `neko_web_search` | 普通网页搜索直接调用；要使用专用数据源时先查 `neko_web_domains`。 |
| `neko_web_batch_search` | 一次并行搜索 1–5 个问题。 |
| `neko_web_domains` | 查询垂直领域的子领域和参数。 |
| `neko_web_read` | 打开一个公开链接，返回标题、正文和候选配图。 |
| `neko_web_images` | 下载图片或网页配图，仅供麦麦预览挑选。 |
| `neko_web_images_next` | 使用上次返回的 `next_cursor` 查看下一批候选图，不重复打开原网页。 |
| `neko_web_send_image` | 传入本聊天预览里的 `image_id`，取回并发送选中的原文件；一次只发一张。 |
| `neko_web_extract` | 用 AnySearch 提取长文。用户只是丢来一个链接时，优先用 `neko_web_read`。 |

图片通过工具结果的 `content_items` 交给麦麦，此时用户还没有收到图片。**这些是压缩预览**，每张最多256KiB/1280边长、每批最多4张/1MiB。结果同时提供原始尺寸、文件大小、来源和 `image_id`；麦麦按相关性选择并优先高清，调用 `neko_web_send_image(image_id=...)` 单独发原文件，不用 `reply.attach_pic` 把预览冒充原图。可少选或不选，不凑数量。正文和图片均为外部资料，不能当成指令。

### 高清原文件与投递

- 只采用网页明确提供的原图：图片外层指向图片的链接、data-original/data-full/data-large等属性，或srcset最大的版本。不盲目删除缩放/签名参数，不猜CDN地址；“网页提供的最高版本”不保证是作者原始母版。
- 图片实际解码/校验，尺寸和像素来自文件而非网页声明；小于720短边标为低清。JPEG/PNG/GIF/WebP，最大2000万像素；动图最多100帧、累计4000万像素。预览仅第一帧，原文件保持动画和原编码。坏图不进入候选。
- 原图发送重新执行公网/IP固定/重定向校验与解码，核对与预览时原文件的SHA256一致；源文件变更时拒绝发送，要求重新预览。单个原文件最多8MiB，原字节经base64交给宿主和适配器（不重新编码），避免16MiB RPC上限。QQ仍可能压缩，平台确认不等于客户端最终原画质保证。
- 超限、无法下载、校验失败返回来源下载链接，绝不静默发送预览作为原图。只有能下载到的原文件进入选择流程，预览下载阶段失败的候选也会在逐项结果里保留URL和原因。不自动上传文件或增加公共文件服务。
- `image_id`当前聊天限定、10分钟有效，最多128个/每聊天24个，只缓存URL/哈希/尺寸，不保存原图像素。发送前原子认领，防并发重复；发送已确认或投递未知不自动重复发送。发送超时/取消可能已提交平台，标未知；插件配置更新或卸载取消在途任务并清空编号。

支持 HTML 解析、`srcset` / `data-srcset`、`data-src` / `data-original` / `data-lazy-src` 和 `picture/source`，每个图片元素选择一个可支持的地址，不把不同分辨率都列成图片。保留 Markdown 图片提取。候选 URL 去掉片段并规范化域名，但保留查询参数；同一查询跨地址、跨批次去重，下载后还按最终 URL 和内容哈希去重。

结果带逐项 `fetch_results`（成功、失败、重复、网页解析、实际尺寸）和 `has_more` / `next_cursor`；同批候选按实际像素排序、`media_item`同步重排。`media_item`只是预览结果内序号，不是原图发送编号；发送用结果里的`image_id`。下载成功不代表已发送。

翻页游标仅属于当前聊天、一次有效，10分钟后或插件重载后失效；过期需重新打开原链接。只保存 URL 队列和去重摘要，不缓存整批图片。网页变化不会改变本次已收集的候选队列；有合适图片即可停止，不需要取完所有批次。

MaiBot 1.3 不会把插件工具直接放进可调用列表。规划器先看到「名字：描述」，再通过 `tool_search` 按名字和描述检索。因此工具名用 `neko_web_` 当命名空间即可，用途写在中文描述里。不要把工具名改成中文。

1.2.1起工具描述仅保留用途和选择入口。专用数据源要求写在搜索参数里，发图说明只在有候选图的结果里出现，翻页说明只在还有候选时出现；游标时效等由代码校验和失败结果处理，不反复塞入每个工具说明。普通搜索不必先查询domains。

## 限制

- 只接受 `http` 和 `https` 的 80 / 443 端口。
- 拒绝内网、本机、云元数据、带账号密码的地址，以及跳到这些地址的重定向。
- 请求前解析并固定公网 IP；代理 CONNECT 也使用该 IP，保留原域名的 Host、TLS SNI 和证书校验。解析失败就拒绝，不让代理再次解析目标域名。公网下载支持 HTTP/HTTPS 代理，SOCKS 仅用于搜索接口；使用 mihomo 时填写 HTTP mixed-port。
- 页面中损坏的图片 URL 会跳过，不影响正文或其他图片。
- 图片只收 JPEG、PNG、GIF、WebP。默认每批最多4张，单原文件8MiB；预览每张256KiB、整批1MiB，并非每次都应发送4张。
- 每网页最多收集48个候选，每次查询最多192项；达到上限会提示。每批最多尝试12个地址、图片下载阶段最多45秒，失败多时也能继续下一批。最多保留64个队列，每聊天4个；超出淘汰最早队列。继续预览仍逐次校验公网地址和重定向，不放宽下载限制。
- 返回给模型的正文默认不超过 8000 字。
- 不执行页面脚本，也读不了需要登录的内容。

## 开发

1.5.0审查修复：AnySearch也使用2MiB流式响应上限、MCP isError识别和安全错误码，提取前URL/DNS公网校验；Firecrawl检查目标statusCode，Exa无效条目不伪装成空结果。工具统一返回success/status/content，局部失败保留证据；不回显供应商原始错误。正文输出预留已请求原文预算，来源和摘录限量，长URL导致少展示来源时明确标partial且不抓不会展示的来源。read优先main/article、去导航/页脚，配图失败保留正文。整轮deadline包含排队；公开读取/原图共享2个并发槽，图片队列在途任务计入容量，热更新/卸载取消在途任务和清理元数据。RPC预算测试、SDK真实导入/执行测试覆盖，而非AST抽取业务方法。第三方远程提取的DNS/重定向安全仍由服务商负责，与本地固定IP下载边界不同。

`public_http.py` 与 `neko-draw` 保持相同副本，独立安装时不互相依赖。修复下载安全逻辑时同步两份，并运行公网下载回归测试。

```bash
uv run --no-project --with maibot-plugin-sdk==2.10.0 --with httpx --with aiohttp --with pillow python -m unittest -q
```
