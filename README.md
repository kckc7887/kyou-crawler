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
