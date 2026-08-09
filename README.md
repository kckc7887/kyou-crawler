# kyou-crawler

抓取 `https://kyou.net.cn/songs` 当前页面实际加载的数据，并整理出：

- `songs.csv/json`：歌曲
- `aliases.csv/json`：别名
- `charts.csv/json`：谱面
- `tag_votes.csv/json`：主/次标签与票数
- `network.json` / `endpoints.txt`：实际捕获到的接口
- `raw/`：原始接口响应
- `web_storage.json`：localStorage / sessionStorage
- `indexeddb.json`：IndexedDB
- `detail_snapshots.ndjson`：深度模式点击详情后看到的文本快照

## 安装

PowerShell：

```powershell
py -m venv .venv; .\.venv\Scripts\Activate.ps1; py -m pip install -r .\requirements.txt; py -m playwright install chromium
```

## 运行

建议第一次直接深度抓取：

```powershell
.\.venv\Scripts\Activate.ps1; py .\crawler.py --deep
```

调试（显示 Chromium）：

```powershell
.\.venv\Scripts\Activate.ps1; py .\crawler.py --deep --headful
```

输出默认在 `output\`。

## 为什么同时保留 raw / IndexedDB

这个站点是动态页面，而且页面明确存在缓存行为。爬虫不把成功与否押在某一个 API 路径或某一套 DOM class 上：

1. 监听 `fetch/XHR/document`；
2. 自动滚动触发懒加载；
3. 抽取 localStorage / sessionStorage；
4. 抽取所有 IndexedDB 数据库；
5. `--deep` 时自动点击高概率歌曲卡片以触发详情、标签、评论等延迟请求；
6. 对所有结构化数据递归归一化。

即使站点字段改名，`raw/` 仍保留原始响应，后续只需修 parser，不需要重新猜接口。

## 建议的首次验证

跑完看 `output\manifest.json`。重点是：

- `raw_responses` > 0
- `songs_rows`
- `aliases_rows`
- `charts_rows`
- `tag_vote_rows`

以及打开 `output\endpoints.txt`，这里就是脚本实际观察到的数据请求。

如果归一化表某列为空，但 `raw/` 里有数据，说明只是字段名没命中规则，不代表没抓到。把对应 raw JSON 的一小段结构提供出来即可精确补 parser。


## v2：Cloudflare 处理

GitHub 托管 Runner 可能会让 `https://kyou.net.cn/api/tags/tree` 返回 Cloudflare 的 `403 Just a moment...`。

v2 会先把该 API 作为顶层页面打开，让浏览器有机会执行 Cloudflare 的浏览器挑战；通过后再进入 `/songs`。GitHub Actions 使用 `xvfb-run + --headful` 运行真实有界面 Chromium。

如果 Cloudflare 仍拒绝 GitHub 的机房 IP，任务会明确失败（不会再出现“绿了但 0 条数据”），同时 `if: always()` 保留完整 Artifact 供诊断。

运行结果新增：

- `cloudflare.json`：挑战通过情况、状态码、是否获得 `cf_clearance`
- `cloudflare.html`：挑战最后页面
- `manifest.json.page_load_failed`：曲目页是否仍加载失败

如果 v2 仍持续返回 Cloudflare 403，说明站点策略直接拒绝 GitHub 托管 Runner 的出口 IP。此时最稳定的做法是换 GitHub self-hosted runner（你的电脑/家里小主机）或其他允许访问该站点的固定运行环境，而不是继续堆解析规则。

## v3 诊断/直连模式

v3 针对 GitHub Hosted Runner 上出现的特殊情况：`/api/tags/tree` 作为顶层页面访问能返回 200 JSON，但 `/songs` 内部的 `fetch` 仍可能加载失败。

新增输出：

- `tag_catalog.json`：标签目录（不会再误记为谱面投票）
- `api_traffic.json`：所有同源 `/api/*` 请求的状态码/类型/响应前缀
- `scripts/`：页面实际加载的同源 JS bundle
- `frontend_api_strings.json`：从 JS bundle 中静态提取的 `/api/...` 字符串及上下文
- `api_probes.json`：对发现的安全只读 API 进行顶层 GET 导航探测的结果
- `browser_events.json`：console/pageerror/requestfailed 诊断

只读 API 探测会跳过名称中包含登录、用户、投票、评论、更新、删除、上传等明显有副作用的接口。
