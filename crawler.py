from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Response

BASE_URL = "https://kyou.net.cn/songs"
CF_PROBE_URL = "https://kyou.net.cn/api/tags/tree"

PRIMARY_TAGS = {"综合", "读谱", "硬抗", "拆谱", "定位", "多指"}
SECONDARY_TAGS = {
    "差速", "脑裂", "多面下落", "变速/闪现", "变速", "闪现", "面海", "扫线",
    "长条藏键", "慢流速", "非线性下落", "判定线干扰", "复杂节奏",
    "长纵连", "长连点/交互", "长连点", "交互", "快交互", "双押海", "宽排键", "连点爆发",
    "全换", "反手", "锁手", "锚键", "刹车", "频繁切轨", "倒打", "蓝夹黄", "蓝夹红",
    "浮现式", "叠", "乱", "切", "楼梯", "3k", "4k", "5k", "6k", "拍砖", "对拍", "对切",
}
ALL_KNOWN_TAGS = PRIMARY_TAGS | SECONDARY_TAGS

NAME_KEYS = {
    "name", "title", "song", "songname", "song_name", "music", "musicname", "music_name",
    "track", "trackname", "track_name", "曲名", "歌曲名",
}
SONG_ID_KEYS = {"songid", "song_id", "musicid", "music_id", "trackid", "track_id"}
CHART_ID_KEYS = {"chartid", "chart_id", "scoreid", "score_id", "谱面id"}
DIFF_KEYS = {"difficulty", "diff", "difficultyname", "difficulty_name", "rank", "谱面难度", "难度"}
CONST_KEYS = {"constant", "const", "rating", "level", "difficultyvalue", "difficulty_value", "定数"}
ALIAS_HINTS = ("alias", "aka", "nickname", "nick", "othername", "other_name", "别名", "俗称")
TAG_HINTS = ("tag", "label", "标签", "属性", "feature")
VOTE_HINTS = ("vote", "votes", "votecount", "vote_count", "count", "score", "like", "赞", "票")
PRIMARY_HINTS = ("primary", "main", "major", "主标签", "主属性", "主")
SECONDARY_HINTS = ("secondary", "sub", "minor", "detail", "次标签", "副标签", "次")

UI_EXCLUDE = {
    "刷新", "清空所有缓存", "标签展示的机制", "搜索", "筛选", "排序",
    "综合", "读谱", "硬抗", "拆谱", "定位", "多指",
    "EZ", "HD", "IN", "AT", "Legacy", "SP", "?", "×", "关闭",
}

def norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9_\u4e00-\u9fff]", "", str(key).lower())

def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)

def sha(obj: Any) -> str:
    return hashlib.sha256(safe_json(obj).encode("utf-8")).hexdigest()

def scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None

def as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float, bool)):
        return str(v)
    return ""

def pick(d: dict, keys: set[str]) -> Any:
    for k, v in d.items():
        if norm_key(k) in keys and scalar(v):
            return v
    return None

def first_name(d: dict) -> str:
    for k, v in d.items():
        nk = norm_key(k)
        if nk in NAME_KEYS and isinstance(v, str) and 1 <= len(v.strip()) <= 200:
            return v.strip()
    return ""

def extract_alias_values(v: Any) -> list[str]:
    out: list[str] = []
    if isinstance(v, str):
        # Preserve aliases containing commas when possible, but split obvious lists.
        parts = re.split(r"[\n、;；|]+", v)
        out += [x.strip() for x in parts if x.strip()]
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, str):
                if x.strip():
                    out.append(x.strip())
            elif isinstance(x, dict):
                n = first_name(x)
                if n:
                    out.append(n)
                else:
                    for kk in ("alias", "nickname", "name", "title", "text", "value"):
                        if kk in x and isinstance(x[kk], str) and x[kk].strip():
                            out.append(x[kk].strip())
                            break
    elif isinstance(v, dict):
        for x in v.values():
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
    return list(dict.fromkeys(out))

def vote_number(d: dict) -> int | float | None:
    preferred = []
    fallback = []
    for k, v in d.items():
        nk = norm_key(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if any(h in nk for h in VOTE_HINTS):
            preferred.append(v)
        elif nk in {"value", "num", "number"}:
            fallback.append(v)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return None

def tag_name_from_obj(d: dict) -> str:
    for k, v in d.items():
        nk = norm_key(k)
        if isinstance(v, str) and (
            nk in {"tag", "tagname", "tag_name", "label", "labelname", "label_name", "text", "name", "title", "标签"}
            or any(h in nk for h in TAG_HINTS)
        ):
            s = v.strip()
            if 0 < len(s) <= 60:
                return s
    return ""

def infer_tag_type(tag: str, path: str, d: dict | None = None) -> str:
    if tag in PRIMARY_TAGS:
        return "primary"
    if tag in SECONDARY_TAGS:
        return "secondary"
    low = norm_key(path)
    if any(norm_key(h) in low for h in SECONDARY_HINTS):
        return "secondary"
    if any(norm_key(h) in low for h in PRIMARY_HINTS):
        return "primary"
    if d:
        for k, v in d.items():
            nk = norm_key(k)
            sv = str(v).lower()
            if "type" in nk or "category" in nk or "kind" in nk:
                if any(h in sv for h in ("secondary", "sub", "minor", "次", "副")):
                    return "secondary"
                if any(h in sv for h in ("primary", "main", "major", "主")):
                    return "primary"
    return "unknown"

@dataclass(frozen=True)
class SongRow:
    song_id: str
    name: str
    source: str

@dataclass(frozen=True)
class AliasRow:
    song_id: str
    song_name: str
    alias: str
    source: str

@dataclass(frozen=True)
class ChartRow:
    chart_id: str
    song_id: str
    song_name: str
    difficulty: str
    constant: str
    source: str

@dataclass(frozen=True)
class VoteRow:
    chart_id: str
    song_id: str
    song_name: str
    difficulty: str
    tag_type: str
    tag: str
    votes: str
    source: str
    path: str

class Normalizer:
    def __init__(self) -> None:
        self.songs: set[SongRow] = set()
        self.aliases: set[AliasRow] = set()
        self.charts: set[ChartRow] = set()
        self.votes: set[VoteRow] = set()

    def add_blob(self, blob: Any, source: str) -> None:
        self._walk(blob, source, "$", {})

    def _walk(self, obj: Any, source: str, path: str, ctx: dict[str, str]) -> None:
        if isinstance(obj, dict):
            local = dict(ctx)

            name = first_name(obj)
            sid = pick(obj, SONG_ID_KEYS)
            cid = pick(obj, CHART_ID_KEYS)
            diff = pick(obj, DIFF_KEYS)
            const = pick(obj, CONST_KEYS)

            if name:
                # A name next to chart/difficulty info is more likely song context than a generic nested label.
                if sid is not None or any(norm_key(k) in DIFF_KEYS | CONST_KEYS for k in obj.keys()) or any(
                    any(x in norm_key(k) for x in ("chart", "difficulty", "谱面", "alias", "别名")) for k in obj.keys()
                ):
                    local["song_name"] = name
            if sid is not None:
                local["song_id"] = str(sid)
            if cid is not None:
                local["chart_id"] = str(cid)
            if diff is not None:
                local["difficulty"] = str(diff)
            if const is not None:
                local["constant"] = str(const)

            song_name = local.get("song_name", "")
            song_id = local.get("song_id", "")
            if song_name and (song_id or any(any(h in norm_key(k) for h in ALIAS_HINTS) for k in obj.keys())):
                self.songs.add(SongRow(song_id, song_name, source))

            # Aliases
            for k, v in obj.items():
                nk = norm_key(k)
                if any(norm_key(h) in nk for h in ALIAS_HINTS):
                    for alias in extract_alias_values(v):
                        if alias and alias != song_name:
                            self.aliases.add(AliasRow(song_id, song_name, alias, source))

            # Chart
            if local.get("chart_id") or local.get("difficulty"):
                if song_name or song_id:
                    self.charts.add(
                        ChartRow(
                            local.get("chart_id", ""),
                            song_id,
                            song_name,
                            local.get("difficulty", ""),
                            local.get("constant", ""),
                            source,
                        )
                    )

            # Tag object: {name/tag: "...", votes/count: N}
            tname = tag_name_from_obj(obj)
            if tname:
                vnum = vote_number(obj)
                looks_taggy = (
                    tname in ALL_KNOWN_TAGS
                    or any(any(h in norm_key(k) for h in TAG_HINTS) for k in obj.keys())
                    or any(any(h in norm_key(k) for h in VOTE_HINTS) for k in obj.keys())
                )
                if looks_taggy:
                    self.votes.add(
                        VoteRow(
                            local.get("chart_id", ""),
                            song_id,
                            song_name,
                            local.get("difficulty", ""),
                            infer_tag_type(tname, path, obj),
                            tname,
                            "" if vnum is None else str(vnum),
                            source,
                            path,
                        )
                    )

            # Map-shaped votes: {"综合": 12, "定位": 8} or {"tags": {"综合": 12}}
            for k, v in obj.items():
                ks = str(k).strip()
                if ks in ALL_KNOWN_TAGS and isinstance(v, (int, float)) and not isinstance(v, bool):
                    self.votes.add(
                        VoteRow(
                            local.get("chart_id", ""),
                            song_id,
                            song_name,
                            local.get("difficulty", ""),
                            infer_tag_type(ks, path, obj),
                            ks,
                            str(v),
                            source,
                            f"{path}.{k}",
                        )
                    )

            for k, v in obj.items():
                self._walk(v, source, f"{path}.{k}", local)

        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self._walk(v, source, f"{path}[{i}]", ctx)

class Recorder:
    def __init__(self, out: Path, normalizer: Normalizer) -> None:
        self.out = out
        self.normalizer = normalizer
        self.raw_dir = out / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.seen_hashes: set[str] = set()
        self.records: list[dict[str, Any]] = []
        self.pending: set[asyncio.Task] = set()

    def schedule_response(self, response: Response) -> None:
        task = asyncio.create_task(self._handle_response(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def drain(self) -> None:
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)

    async def _handle_response(self, response: Response) -> None:
        try:
            req = response.request
            rt = req.resource_type
            if rt not in ("xhr", "fetch", "document"):
                return
            ct = (response.headers.get("content-type") or "").lower()
            if not any(x in ct for x in ("json", "javascript", "text/plain", "text/html")):
                return
            text = await response.text()
            if not text or len(text) > 25_000_000:
                return
            parsed: Any
            try:
                parsed = json.loads(text)
            except Exception:
                # Keep text only when it looks relevant.
                if not any(x in text for x in ("综合", "读谱", "硬抗", "拆谱", "定位", "多指", "alias", "vote", "tag", "别名", "标签")):
                    return
                parsed = {"_text": text}

            h = sha(parsed)
            if h in self.seen_hashes:
                return
            self.seen_hashes.add(h)

            n = len(self.records) + 1
            raw_path = self.raw_dir / f"response_{n:05d}.json"
            raw_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

            meta = {
                "url": response.url,
                "status": response.status,
                "method": req.method,
                "resource_type": rt,
                "content_type": ct,
                "post_data": req.post_data,
                "raw_file": str(raw_path.relative_to(self.out)),
                "sha256": h,
            }
            self.records.append(meta)
            self.normalizer.add_blob(parsed, response.url)
        except Exception:
            return



async def warm_up_cloudflare(page: Page, out: Path, timeout_ms: int = 45000) -> dict[str, Any]:
    """Navigate to a protected API endpoint so Cloudflare can run its browser challenge.

    fetch/XHR challenges return HTML to JavaScript and are never executed. A top-level
    navigation gives Cloudflare a real page where its challenge script can run and, on
    success, set the clearance cookie for subsequent API requests.
    """
    result: dict[str, Any] = {
        "url": CF_PROBE_URL,
        "attempted": True,
        "passed": False,
        "status": None,
        "content_type": "",
        "final_url": "",
        "title": "",
        "body_prefix": "",
        "clearance_cookie": False,
    }

    try:
        response = await page.goto(CF_PROBE_URL, wait_until="domcontentloaded", timeout=90000)
        if response is not None:
            result["status"] = response.status
            result["content_type"] = (response.headers.get("content-type") or "").lower()

        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            await page.wait_for_timeout(1000)
            try:
                title = await page.title()
                body = (await page.locator("body").inner_text())[:4000]
            except Exception:
                title, body = "", ""

            cookies = await page.context.cookies()
            has_clearance = any(c.get("name") == "cf_clearance" for c in cookies)
            challenge = (
                "Just a moment" in title
                or "Enable JavaScript and cookies to continue" in body
                or "cf-chl" in (await page.content())[:12000]
            )

            result.update({
                "final_url": page.url,
                "title": title,
                "body_prefix": body[:1000],
                "clearance_cookie": has_clearance,
            })

            # The API JSON may be rendered as plain text in the browser. Any non-challenge
            # page after the direct navigation is enough to retry the app.
            if not challenge:
                result["passed"] = True
                break

        (out / "cloudflare.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            (out / "cloudflare.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        return result
    except Exception as e:
        result["error"] = repr(e)
        (out / "cloudflare.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result


async def wait_for_song_page(page: Page, retries: int = 3) -> str:
    """Wait for the song page; retry its own retry button when API startup races."""
    last_body = ""
    for attempt in range(retries + 1):
        try:
            await page.wait_for_timeout(1500 if attempt == 0 else 1000)
            last_body = await page.locator("body").inner_text()
        except Exception:
            last_body = ""

        if "无法加载曲目列表" not in last_body and "加载失败" not in last_body:
            return last_body

        if attempt < retries:
            try:
                retry = page.get_by_text("重试", exact=True)
                if await retry.count():
                    await retry.first.click(timeout=3000)
                    await page.wait_for_timeout(2000)
                    continue
            except Exception:
                pass
            await page.reload(wait_until="domcontentloaded", timeout=90000)

    return last_body

async def dump_web_storage(page: Page, out: Path, normalizer: Normalizer) -> None:
    storage = await page.evaluate("""() => {
      const read = (s) => {
        const o = {};
        for (let i = 0; i < s.length; i++) {
          const k = s.key(i);
          o[k] = s.getItem(k);
        }
        return o;
      };
      return {localStorage: read(localStorage), sessionStorage: read(sessionStorage)};
    }""")
    (out / "web_storage.json").write_text(json.dumps(storage, ensure_ascii=False, indent=2), encoding="utf-8")

    for group_name, group in storage.items():
        for k, v in group.items():
            if not isinstance(v, str):
                continue
            try:
                obj = json.loads(v)
            except Exception:
                continue
            normalizer.add_blob(obj, f"{group_name}:{k}")

async def dump_indexeddb(page: Page, out: Path, normalizer: Normalizer) -> None:
    data = await page.evaluate("""async () => {
      if (!indexedDB.databases) return {error: "indexedDB.databases() unavailable", databases: []};
      const dbs = await indexedDB.databases();
      const result = [];
      for (const info of dbs) {
        if (!info.name) continue;
        const opened = await new Promise((resolve) => {
          const req = indexedDB.open(info.name);
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => resolve(null);
        });
        if (!opened) continue;
        const stores = {};
        for (const storeName of Array.from(opened.objectStoreNames)) {
          try {
            const rows = await new Promise((resolve) => {
              const tx = opened.transaction(storeName, "readonly");
              const req = tx.objectStore(storeName).getAll();
              req.onsuccess = () => resolve(req.result);
              req.onerror = () => resolve([]);
            });
            stores[storeName] = rows;
          } catch (e) {
            stores[storeName] = {error: String(e)};
          }
        }
        opened.close();
        result.push({name: info.name, version: info.version, stores});
      }
      return {databases: result};
    }""")
    (out / "indexeddb.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    normalizer.add_blob(data, "indexedDB")

async def auto_scroll(page: Page) -> None:
    stable = 0
    prev = -1
    for _ in range(120):
        h = await page.evaluate("document.documentElement.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        await page.wait_for_timeout(250)
        nh = await page.evaluate("document.documentElement.scrollHeight")
        if nh == prev == h:
            stable += 1
        else:
            stable = 0
        prev = nh
        if stable >= 5:
            break
    await page.evaluate("window.scrollTo(0, 0)")

async def candidate_clickables(page: Page, limit: int) -> list[dict[str, Any]]:
    return await page.evaluate(
        """(limit) => {
          const mainTags = new Set(["综合","读谱","硬抗","拆谱","定位","多指"]);
          const exclude = new Set(["刷新","清空所有缓存","标签展示的机制","搜索","筛选","排序","综合","读谱","硬抗","拆谱","定位","多指","?","×","关闭"]);
          const els = Array.from(document.querySelectorAll('a,button,[role="button"],[tabindex],[class*="cursor-pointer"]'));
          const out = [];
          const seen = new Set();
          for (const el of els) {
            const r = el.getBoundingClientRect();
            if (r.width < 20 || r.height < 16) continue;
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim();
            if (!text || text.length > 180 || exclude.has(text)) continue;
            const hasImg = !!el.querySelector('img,picture,[style*="background-image"]');
            const hasTag = Array.from(mainTags).some(x => text.includes(x));
            const cls = typeof el.className === 'string' ? el.className : '';
            let score = 0;
            if (hasImg) score += 5;
            if (hasTag) score += 4;
            if (cls.includes('cursor-pointer')) score += 2;
            if (el.tagName === 'A') score += 1;
            if (/\\b(EZ|HD|IN|AT|SP|Legacy)\\b/i.test(text)) score += 2;
            if (text.length >= 3 && text.length <= 80) score += 1;
            if (score < 3) continue;
            const key = text.slice(0,120);
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({text:key, tag:el.tagName, cls, score});
          }
          out.sort((a,b) => b.score-a.score);
          return out.slice(0, limit);
        }""",
        limit,
    )

async def click_explore(page: Page, out: Path, max_clicks: int, delay_ms: int) -> int:
    # Scan viewport-by-viewport instead of once. This also works when the song list is virtualized.
    clicked = 0
    seen_texts: set[str] = set()
    all_candidates: list[dict[str, Any]] = []
    details_file = (out / "detail_snapshots.ndjson").open("w", encoding="utf-8")

    try:
        await page.evaluate("window.scrollTo(0, 0)")
        stable_bottom_rounds = 0
        last_y = -1

        while clicked < max_clicks and stable_bottom_rounds < 6:
            candidates = await candidate_clickables(page, 160)
            for c in candidates:
                if clicked >= max_clicks:
                    break
                text = c["text"]
                if text in UI_EXCLUDE or text in seen_texts:
                    continue
                seen_texts.add(text)
                all_candidates.append(c)

                try:
                    # Prefer exact text. If the list is virtualized, the candidate is currently in DOM.
                    loc = page.get_by_text(text, exact=True)
                    if await loc.count() == 0:
                        loc = page.locator("a,button,[role=button],[tabindex],[class*='cursor-pointer']").filter(has_text=text)
                    if await loc.count() == 0:
                        continue

                    el = loc.first
                    await el.click(timeout=2200)
                    await page.wait_for_timeout(delay_ms)
                    clicked += 1

                    snap = await page.evaluate("""() => {
                      const sel = [
                        '[role="dialog"]',
                        '[class*="modal"]',
                        '[class*="dialog"]',
                        '[class*="drawer"]',
                        '[class*="sheet"]'
                      ].join(',');
                      const nodes = Array.from(document.querySelectorAll(sel)).filter(x => {
                        const s = getComputedStyle(x);
                        const r = x.getBoundingClientRect();
                        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 10 && r.height > 10;
                      });
                      const text = nodes.length
                        ? nodes.map(x => (x.innerText || '').trim()).filter(Boolean).join('\\n---\\n')
                        : (document.body.innerText || '').slice(0, 12000);
                      return text.slice(0, 20000);
                    }""")
                    if any(x in snap for x in ("标签", "别名", "综合", "读谱", "硬抗", "拆谱", "定位", "多指")):
                        details_file.write(json.dumps({"clicked": text, "snapshot": snap}, ensure_ascii=False) + "\n")
                        details_file.flush()

                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(80)

                    if urlparse(page.url).path != "/songs":
                        await page.go_back(wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(250)

                except Exception:
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    continue

            # Advance roughly one viewport. New virtualized cards will enter the DOM.
            state = await page.evaluate("""() => {
              const y = window.scrollY;
              const h = window.innerHeight;
              const maxY = Math.max(0, document.documentElement.scrollHeight - h);
              const nextY = Math.min(maxY, y + Math.max(500, Math.floor(h * 0.82)));
              window.scrollTo(0, nextY);
              return {y, nextY, maxY};
            }""")
            await page.wait_for_timeout(180)

            if state["nextY"] >= state["maxY"] - 2 and state["nextY"] == last_y:
                stable_bottom_rounds += 1
            elif state["nextY"] >= state["maxY"] - 2:
                stable_bottom_rounds += 1
            else:
                stable_bottom_rounds = 0
            last_y = state["nextY"]

        (out / "click_candidates.json").write_text(
            json.dumps(all_candidates, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        details_file.close()

    return clicked

def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def dataclass_rows(rows: set) -> list[dict[str, Any]]:
    return [asdict(x) for x in sorted(rows, key=lambda x: tuple(str(v) for v in asdict(x).values()))]

def write_outputs(out: Path, normalizer: Normalizer, recorder: Recorder, clicked: int, started_at: float) -> None:
    songs = dataclass_rows(normalizer.songs)
    aliases = dataclass_rows(normalizer.aliases)
    charts = dataclass_rows(normalizer.charts)
    votes = dataclass_rows(normalizer.votes)

    # Remove obvious false-positive tag names when we have enough known tags to anchor the dataset.
    votes = [
        r for r in votes
        if r["tag"] in ALL_KNOWN_TAGS
        or any(x in r["path"].lower() for x in ("tag", "label", "标签"))
    ]

    for name, data in (
        ("songs.json", songs),
        ("aliases.json", aliases),
        ("charts.json", charts),
        ("tag_votes.json", votes),
    ):
        (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    write_csv(out / "songs.csv", songs)
    write_csv(out / "aliases.csv", aliases)
    write_csv(out / "charts.csv", charts)
    write_csv(out / "tag_votes.csv", votes)

    (out / "network.json").write_text(json.dumps(recorder.records, ensure_ascii=False, indent=2), encoding="utf-8")

    endpoints = sorted({
        (r["method"], r["url"])
        for r in recorder.records
        if r["resource_type"] in ("xhr", "fetch")
    })
    (out / "endpoints.txt").write_text(
        "\n".join(f"{m}\t{u}" for m, u in endpoints),
        encoding="utf-8",
    )

    manifest = {
        "source": BASE_URL,
        "started_unix": started_at,
        "finished_unix": time.time(),
        "clicked_candidates": clicked,
        "raw_responses": len(recorder.records),
        "songs_rows": len(songs),
        "aliases_rows": len(aliases),
        "charts_rows": len(charts),
        "tag_vote_rows": len(votes),
        "known_primary_tags": sorted(PRIMARY_TAGS),
        "known_secondary_tags": sorted(SECONDARY_TAGS),
        "note": "raw/ + indexeddb.json + web_storage.json are retained so parser rules can be refined without recrawling.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

async def run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started_at = time.time()

    normalizer = Normalizer()
    recorder = Recorder(out, normalizer)

    cf_result: dict[str, Any] = {}
    body = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headful)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
        )
        page = await context.new_page()
        page.on("response", recorder.schedule_response)

        print(f"[0/6] Cloudflare 预热：{CF_PROBE_URL}")
        cf_result = await warm_up_cloudflare(page, out, args.cf_timeout_ms)
        print(json.dumps(cf_result, ensure_ascii=False))

        print(f"[1/6] 打开 {BASE_URL}")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        body = await wait_for_song_page(page, retries=3)

        print("[2/6] 自动滚动，触发列表懒加载")
        if "无法加载曲目列表" not in body and "加载失败" not in body:
            await auto_scroll(page)
            await page.wait_for_timeout(800)
            body = await page.locator("body").inner_text()

        (out / "body.txt").write_text(body, encoding="utf-8")
        (out / "page.html").write_text(await page.content(), encoding="utf-8")

        print("[3/6] 读取 localStorage / sessionStorage / IndexedDB")
        await dump_web_storage(page, out, normalizer)
        try:
            await dump_indexeddb(page, out, normalizer)
        except Exception as e:
            (out / "indexeddb_error.txt").write_text(str(e), encoding="utf-8")

        clicked = 0
        if args.deep:
            print(f"[4/6] 深度探索：最多点击 {args.max_clicks} 个疑似歌曲卡片")
            clicked = await click_explore(page, out, args.max_clicks, args.delay_ms)
            await recorder.drain()
            # Clicks may populate browser caches.
            await dump_web_storage(page, out, normalizer)
            try:
                await dump_indexeddb(page, out, normalizer)
            except Exception:
                pass
        else:
            print("[4/6] 跳过深度点击（加 --deep 可启用）")

        await recorder.drain()
        await browser.close()

    print("[5/6] 归一化并写出 CSV / JSON")
    write_outputs(out, normalizer, recorder, clicked, started_at)

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["cloudflare"] = cf_result
    manifest["page_load_failed"] = ("无法加载曲目列表" in body or "加载失败" in body)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

    print("[6/6] 完整性检查")
    if not cf_result.get("passed") or manifest["page_load_failed"]:
        print("错误：Cloudflare 挑战未通过，曲目 API 仍被拦截。诊断文件已保留。", file=sys.stderr)
        return 3
    if manifest["raw_responses"] == 0:
        print("错误：没有捕获到数据响应。", file=sys.stderr)
        return 2
    if manifest["songs_rows"] == 0 and manifest["charts_rows"] == 0 and manifest["tag_vote_rows"] == 0:
        print("错误：已通过页面加载，但归一化结果仍为空；请检查 raw/ 后补字段映射。", file=sys.stderr)
        return 4
    return 0

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="抓取 kyou.net.cn/songs 的歌曲、谱面、别名与标签投票")
    ap.add_argument("--out", default="output", help="输出目录")
    ap.add_argument("--deep", action="store_true", help="自动点击疑似歌曲卡片，触发详情/投票懒加载")
    ap.add_argument("--max-clicks", type=int, default=2200, help="深度模式最多点击数量")
    ap.add_argument("--delay-ms", type=int, default=250, help="每次点击后的等待，默认 250ms")
    ap.add_argument("--headful", action="store_true", help="显示浏览器窗口；GitHub Actions 建议配合 xvfb-run")
    ap.add_argument("--cf-timeout-ms", type=int, default=45000, help="等待 Cloudflare 浏览器挑战的最长时间")
    return ap.parse_args()

if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
