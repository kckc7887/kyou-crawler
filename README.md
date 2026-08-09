# kyou-crawler v4

抓取 `https://kyou.net.cn` 的 Phigros 定数表、别名以及谱面标签投票。

v4 不再依赖 `/songs` 页面 DOM。根据站点当前前端 bundle 逆出的真实接口直接取数：

- `GET /api/songs/songlist`：歌曲、别名、各难度定数
- `GET /api/tags/tree`：标签目录
- `POST /api/tags/top-batch`：批量谱面标签票数、主标签汇总
- `GET /api/tags/tree?songId=...&difficulty=...`：单谱完整投票（仅作为备用）

## 为什么要“隔离浏览器上下文”

GitHub Hosted Runner 上，Cloudflare 对 `/songs` 内部的 `fetch` 会返回 403，但干净 Chromium 上下文的第一个顶层 API 请求可以通过。v4 因此让关键 API 调用在新的浏览器上下文中执行。

对于批量 POST，v4 有两种尝试：

1. 把顶层文档导航在浏览器请求层改写成 JSON POST；
2. 使用本地 fulfilled 的同源 bootstrap 页面，让 POST fetch 成为新上下文的第一个真实网络请求。

这样不需要逐个点击 1600+ 首歌。

## 输出

成功时 `output/` 包含：

- `songs.csv/json`
- `aliases.csv/json`
- `charts.csv/json`
- `tag_votes.csv/json`
- `tag_catalog.csv/json`
- `data.json`：按歌曲聚合的完整数据
- `manifest.json`
- `api_attempts.json`：每次 API 尝试及 HTTP 状态
- `raw/songlist.json`
- `raw/tag_catalog.json`
- `raw/top_batch/*.json`

`charts.csv` 的 `main_label` 会按网站前端当前规则计算：主属性前两名占比差不超过 10% 时显示“综合”；最高主属性票数低于 20 时 `main_label_question=true`。

`tag_votes.csv` 中：

- `tag_type=primary`：五个真实主属性（读谱、硬抗、拆谱、定位、多指）的汇总票数；
- `tag_type=secondary`：细标签票数；
- “综合”不是后端标签节点，而是前端根据主属性票数推导出的显示标签，所以放在 `charts.main_label`，不会伪造为一条投票。

## 本地运行

```powershell
py -m venv .venv; .\.venv\Scripts\Activate.ps1; py -m pip install -r .\requirements.txt; py -m playwright install chromium; py .\crawler.py --headful --out output
```

## GitHub Actions

定时任务默认只使用批量接口，避免在批量接口失败时自动产生数千次逐谱请求。

如果需要人工验证逐谱 GET 备用方案，在 Actions 的 **Run workflow** 中勾选 `tree_fallback`。这会显著增加请求量，不建议作为小时级定时策略。

## 完整性

只有歌曲、谱面和标签数据完整时脚本才返回 0。失败时 Actions 会变红，但 `if: always()` 仍会上传诊断 Artifact，便于继续定位。
