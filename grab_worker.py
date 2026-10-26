
# -*- coding: utf-8 -*-
"""cosplaytele.com 抓取器：按关键词搜文章 -> 抓 m3u8 -> 下载合并 mp4。

为什么单独一个文件：站点抓取和 X 搜索完全无关（一个是 Playwright 驱动浏览器，
一个是 twscrape 打 API），混在一起会让 x_worker.py 更难维护。

实测结论（2026-10-22）：
  · cosplaytele.com 是 WordPress，走系统代理 7897 才通（裸连 CONNECT_TIMEOUT）
  · 文章页的视频不在 HTML 里，是 cossora.stream 的 iframe 播放器
  · 播放器有 JS 混淆 + jwplayer.key + crypto-js 加密 + devtools 检测
  · **必须带 Referer: https://cosplaytele.com/... 才不返回 {"error":true}**
  · 加上 Referer 后 Playwright 能拿到 master_1920p.m3u8?token=...
  · m3u8 是标准 HLS，15s/分片，curl 直下 TS，ffmpeg concat 合并
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 外部依赖（ffmpeg/node/aria2/浏览器）统一由 xmf_paths 探测：
# 环境变量 > paths.json > PATH > 常见安装位置。发布版不含任何绝对路径。
import xmf_paths

NODE_EXE = xmf_paths.NODE
PW_MODULE = xmf_paths._pw_module_dir(HERE)
FFMPEG = xmf_paths.FFMPEG
FFPROBE = xmf_paths.FFPROBE
BROWSERS = xmf_paths.BROWSERS

PROXY = xmf_paths.PROXY
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")


def log_warn(msg):
    emit({"t": "progress", "stage": "warn", "note": str(msg)[:200]})


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(code, message):
    emit({"t": "result", "ok": False, "code": code, "error": message})


# ── 搜索 ────────────────────────────────────────────────────────────────────
SEARCH_JS = r"""
import { chromium } from "playwright";
const kw = process.argv[2];
const limit = parseInt(process.argv[3] || "12", 10);
const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--disable-blink-features=AutomationControlled","--no-sandbox",
         "--autoplay-policy=no-user-gesture-required"]
});
const ctx = await browser.newContext({
  userAgent: process.env.XMF_UA,
  viewport: { width: 1366, height: 768 },
  locale: "zh-CN", timezoneId: "Asia/Shanghai"
});
await ctx.addInitScript(() => {
  Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  window.chrome = { runtime: {} };
});
const page = await ctx.newPage();
const out = { items: [], error: null };
try {
  const url = "https://cosplaytele.com/?s=" + encodeURIComponent(kw);
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(2500);
  // 实测：搜索结果页没有 <article>，文章标题在 .post-title a 里（67 个）。
  // 光靠"像文章 URL"筛选会混进导航项（3-day / 7-day 之类），必须用真实选择器。
  const items = await page.evaluate((lim) => {
    const arts = [];
    const seen = new Set();
    const SEARCH_PAGES = /\/(category|tag|author|page|feed|top-search|explore-categories|best-cosplayer|24-hours|3-day|7-day|dmca|contact|about)\b/;
    for (const a of document.querySelectorAll(".post-title a, h2 a, h3 a")) {
      const h = a.href || "";
      if (!/^https:\/\/cosplaytele\.com\/[^\/]+\/?$/i.test(h)) continue;
      if (SEARCH_PAGES.test(h)) continue;
      if (h.replace(/\/$/,"") === "https://cosplaytele.com") continue;
      if (seen.has(h)) continue;
      seen.add(h);
      const card = a.closest("article, .post, .blog-item, li, div");
      let img = card ? card.querySelector("img") : null;
      if (!img) {
        // 缩略图可能在同一卡片的兄弟节点
        const p = a.parentElement?.parentElement;
        img = p ? p.querySelector("img") : null;
      }
      const t = (a.textContent || "").trim();
      if (!t || t.length < 3) continue;
      arts.push({
        url: h.replace(/\/$/, "") + "/",
        title: t.slice(0, 120).replace(/\s+/g, " "),
        thumb: img ? (img.currentSrc || img.src || img.getAttribute("data-src") || "") : ""
      });
      if (arts.length >= lim) break;
    }
    return arts;
  }, limit);
  out.items = items;
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
console.log("@@JSON@@" + JSON.stringify(out));
"""


# ── 抓 m3u8 ─────────────────────────────────────────────────────────────────
VIDEO_JS = r"""
import { chromium } from "playwright";
const postUrl = process.argv[2];
const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--disable-blink-features=AutomationControlled","--no-sandbox",
         "--autoplay-policy=no-user-gesture-required"]
});
const ctx = await browser.newContext({
  userAgent: process.env.XMF_UA,
  viewport: { width: 1366, height: 768 },
  locale: "zh-CN", timezoneId: "Asia/Shanghai"
});
await ctx.addInitScript(() => {
  Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  window.chrome = { runtime: {} };
});
const page = await ctx.newPage();
const out = { m3u8: null, embed: null, title: "", duration: 0, error: null };
try {
  // ① 打开文章页，找出 embed iframe
  // 代理偶尔抽风（实测同一篇文章有时 45s 超时），重试 2 次。
  let ok = false, lastErr = "";
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await page.goto(postUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
      ok = true; break;
    } catch (e) { lastErr = String(e.message).slice(0, 100); await page.waitForTimeout(2000); }
  }
  if (!ok) { out.error = "打开文章页失败（重试3次）：" + lastErr; throw new Error(out.error); }
  out.title = (await page.title() || "").slice(0, 140);
  const embed = await page.evaluate(() => {
    for (const f of document.querySelectorAll("iframe[src]")) {
      const s = f.src || "";
      if (s.includes("cossora.stream/embed/")) return s;
    }
    return null;
  });
  if (!embed) { out.error = "文章页没有找到 cossora 播放器"; }
  else {
    out.embed = embed;
    // ② 关键：带 Referer 打开播放器，否则返回 {"error":true,"message":"Unknown Error xD"}
    page.on("request", (r) => {
      const u = r.url();
      if (!out.m3u8 && /master_\d+p\.m3u8/.test(u)) out.m3u8 = u;
    });
    let ok2 = false, lastErr2 = "";
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        await page.goto(embed, { waitUntil: "domcontentloaded", timeout: 60000, referer: postUrl });
        ok2 = true; break;
      } catch (e) { lastErr2 = String(e.message).slice(0, 100); await page.waitForTimeout(2000); }
    }
    if (!ok2) { out.error = "打开播放器失败（重试3次）：" + lastErr2; }
    else await page.waitForTimeout(10000);
    const info = await page.evaluate(() => {
      const v = document.querySelector("video");
      let dur = 0;
      if (v && isFinite(v.duration)) dur = Math.round(v.duration);
      let file = null;
      if (window.jwplayer) { try { const p = window.jwplayer(); const it = p.getPlaylistItem?.(); if (it) file = it.file; } catch (e) {} }
      return { duration: dur, file };
    });
    out.duration = info.duration || 0;
    if (!out.m3u8 && info.file && /m3u8/.test(info.file)) out.m3u8 = info.file;
  }
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
console.log("@@JSON@@" + JSON.stringify(out));
"""


def io_open(path, mode):
    import codecs
    return codecs.open(path, mode, encoding="utf-8")


def _run_node(script, args, timeout=180):
    """把 JS 写到临时文件执行，返回 @@JSON@@ 后的内容。"""
    # 必须写进 PW_MODULE 目录：ESM 解析 node_modules 是从脚本所在目录往上找的，
    # 放 TEMP 会 ERR_MODULE_NOT_FOUND: Cannot find package 'playwright'。
    os.makedirs(PW_MODULE, exist_ok=True)
    sp = os.path.join(PW_MODULE, "_xmf_run_%d.mjs" % int(time.time() * 1000))
    with io_open(sp, "w") as f:
        f.write(script)
    try:
        env = dict(os.environ)
        env.update({"PLAYWRIGHT_BROWSERS_PATH": BROWSERS, "XMF_UA": UA, "XMF_PROXY": PROXY})
        p = subprocess.run([NODE_EXE, sp] + [str(a) for a in args],
                           cwd=PW_MODULE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
        out = (p.stdout or "")
        if "@@JSON@@" in out:
            return json.loads(out.split("@@JSON@@", 1)[1].strip())
        raise RuntimeError((out + (p.stderr or ""))[-400:])
    finally:
        try:
            os.unlink(sp)
        except Exception:
            pass


def _curl(url, referer, binary=False, timeout=60):
    """用系统 curl 走代理下载（Node 内置 fetch 不读系统代理）。"""
    cmd = ["curl", "-s", "--proxy", PROXY, "-A", "Mozilla/5.0",
           "-e", referer, "--max-time", str(timeout), "-L", url]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    if binary:
        return p.stdout
    return (p.stdout or b"").decode("utf-8", "replace")


# ── 中文 → 角色名（cosplaytele 是英文站，中文搜不到）────────────────────────
#
# 关键事实（实测）：cosplaytele 是英文站，直接搜「刻晴」「雷电将军」得到 0 条。
# 必须先转成角色/作品对应的英文名：刻晴 -> keqing，雷电将军 -> raiden shogun。
#
# 这张表覆盖原神/崩铁/明日方舟/蔚蓝档案等热门 cosplay 题材。
# 不在表里就原样搜索（不要机械音译，只会产生搜不到任何东西的乱码）。

CN_TO_EN = {
    # 原神
    "刻晴": "keqing", "雷电将军": "raiden shogun", "雷神": "raiden shogun",
    "八重神子": "yae miko", "甘雨": "ganyu", "胡桃": "hu tao",
    "神里绫华": "ayaka kamisato", "绫华": "ayaka", "申鹤": "shenhe",
    "夜兰": "yelan", "纳西妲": "nahida", "宵宫": "yoimiya",
    "雷电": "raiden", "影": "raiden", "心海": "kokomi",
    "妮露": "nilou", "迪希雅": "dehya", "芙宁娜": "furina",
    "水神": "furina", "娜维娅": "navia", "赛诺": "cyno",
    "温迪": "venti", "钟离": "zhongli", "凝光": "ningguang",
    "刻情": "keqing", "莫娜": "mona", "菲谢尔": "fischl",
    "优菈": "eula", "罗莎莉亚": "rosaria", "北斗": "beidou",
    "香菱": "xiangling", "云堇": "yunjin", "夜阑": "yelan",
    # 崩坏星穹铁道
    "卡芙卡": "kafka", "银狼": "silver wolf", "希儿": "seele",
    "布洛妮娅": "bronya", "符玄": "fu xuan", "镜流": "jingliu",
    "阮梅": "ruan mei", "花火": "sparkle", "黄泉": "acheron",
    "卡夫卡": "kafka", "停云": "tingyun", "素裳": "sushang",
    # 明日方舟
    "安洁莉娜": "angelina", "德克萨斯": "texas", "能天使": "exusiai",
    "阿米娅": "amiya", "陈": "chen", "W": "w arknights",
    # 蔚蓝档案
    "白子": "shiroko", "爱丽丝": "aris", "优香": "yuuka",
    "星野": "hoshino", "美游": "miyu",
    # 其他热门
    "初音": "hatsune miku", "初音未来": "hatsune miku",
    "明日香": "asuka", "绫波丽": "ayanami rei", "零": "rei",
    "蒂法": "tifa", "爱丽丝ff7": "aerith",
    "2b": "2b nier", "尼娅": "nier automata",
    "玛奇玛": "makima", "帕瓦": "power chainsaw",
    "电锯人": "chainsaw man", "间谍教室": "spy classroom",
    "雷姆": "rem", "拉姆": "ram", "艾米莉亚": "emilia",
    "萨布丽娜": "sabrina", "魔女": "witch",
    # ── 补充：成人 cos 圈高频角色（2026-10-25）──────────────────────────
    # 原词库只有 34 条，搜「千夏」会原样返回中文，
    # 而 cosplaytele 是英文站 → 字面命中 0 条，导致"一个都没加进来"。
    # 偶像大师 / 赛马娘 / 蔚蓝档案 / 东方 / 碧蓝航线 / 其他热门
    "千夏": "chinatsu", "千夏酱": "chinatsu",
    "涩谷凛": "shibuya rin", "岛村卯月": "shimamura uzuki",
    "本田未央": "honda mio", "鹭沢文香": "sagisawa fumika",
    "神崎兰子": "kanzaki ranko", "双叶杏": "futaba anzu",
    "前川未来": "maekawa miku", "舆水幸子": "koshimizu sachiko",
    "安部菜菜": "abe nana", "绪方智绘里": "ogata chieri",
    "城崎莉嘉": "kurosaki rika", "城崎美嘉": "kurosaki mika",
    "一之濑志希": "ichinose shiki", "宫本芙蕾德莉卡": "miyamoto frederica",
    "速水奏": "hayami kanade", "鹰富士茄子": "takafuji kako",
    "盐见周子": "shiomi syuko", "佐久间麻由": "sakuma mayu",
    # 赛马娘
    "特别周": "special week", "东海帝皇": "tokai teio",
    "小栗帽": "oguri cap", "目白麦昆": "mejiro mcqueen",
    "无声铃鹿": "silence suzuka", "大和赤骥": "daiwa scarlet",
    "黄金船": "gold ship",
    # 蔚蓝档案补充
    "阿罗娜": "arona", "日奈": "hina", "泉奈": "izuna",
    "若藻": "wakamo", "圣娅": "seia", "和纱": "kazuha",
    "睦月": "mutsuki", "佳代子": "kayoko", "遥香": "haruka",
    "阿露": "aru", "白洲梓": "shirasu azusa", "小春": "koharu",
    "花子": "hanako", "羽留奈": "haruna", "伊织": "iori",
    "千寻": "chihiro", "真纪": "maki",
    # 东方
    "灵梦": "reimu", "博丽灵梦": "reimu hakurei",
    "魔理沙": "marisa", "雾雨魔理沙": "marisa kirisame",
    "咲夜": "sakuya", "十六夜咲夜": "sakuya izayoi",
    "芙兰朵露": "flandre", "蕾米莉亚": "remilia",
    "琪露诺": "cirno", "帕秋莉": "patchouli",
    "幽幽子": "yuyuko", "妖梦": "youmu", "早苗": "sanae",
    # 碧蓝航线
    "贝尔法斯特": "belfast azur", "拉菲": "laffey",
    "独角兽": "unicorn azur", "雪风": "yukikaze",
    "高雄": "takao azur", "爱宕": "atago azur",
    "可畏": "formidable", "欧根亲王": "prinz eugen",
    # 其他热门
    "约尔": "yor forger", "约尔福杰": "yor forger",
    "蒂法": "tifa lockhart", "阿尼亚": "anya forger",
    "芙莉莲": "frieren", "菲伦": "fern frieren",
    "金克丝": "jinx", "亚丝娜": "asuna",
    "辉夜": "kaguya", "四宫辉夜": "kaguya shinomiya",
    "藤原千花": "chika fujiwara",
    "波奇": "bocchi", "后藤一里": "bocchi hitori",
    "虹夏": "nijika", "玛俐": "marnie pokemon",
}

# 通用题材词（中文 -> 英文）
CN_GENERIC = {
    "cos视频": "cosplay", "cos": "cosplay", "コスプレ": "cosplay",
    "护士": "nurse", "女仆": "maid", "学生": "school girl",
    "制服": "uniform", "旗袍": "cheongsam", "和服": "kimono",
    "泳装": "swimsuit", "比基尼": "bikini", "内衣": "lingerie",
    "白丝": "white stockings", "黑丝": "black stockings",
    "自慰": "masturbation", "露出": "exposed", "巨乳": "big boobs",
    "兔女郎": "bunny girl", "猫娘": "catgirl", "蕾丝": "lace",
}


def cn_to_en(text: str) -> list:
    """把中文关键词转成英文搜索词。返回候选列表（可能多个）。

    策略：
      1. 整串命中表 -> 返回对应英文名
      2. 表里的词出现在串中 -> 收集所有命中的英文名
      3. 都不命中 -> 原样返回（英文/日文关键词直接可用）
    """
    kw = str(text or "").strip()
    if not kw:
        return []
    low = kw.lower()

    # 已经是纯英文/数字：直接用
    if re.fullmatch(r"[A-Za-z0-9 _\-']+", kw):
        return [kw]

    out = []
    for table in (CN_TO_EN, CN_GENERIC):
        # 整串命中优先
        if kw in table:
            out.append(table[kw])
            continue
        for cn, en in table.items():
            if cn in kw and en not in out:
                out.append(en)
    if out:
        # 去重保序
        return list(dict.fromkeys(out))
    # 表里没有：原样返回，交给站内搜索（中文站能搜中文）
    return [kw]


# ── 游戏巴士（gamebus001.cc）────────────────────────────────────────────────
#
# 三个站里**最干净**的一个：纯 JSON API，不用浏览器，不用解密。
#   POST /api/video/page/1  {"keyword":"cos","page":1,"sort":"createdAt"}
#        -> {code:200, data:{total:897, list:[{id,title,coverUrl,duration,views,height}]}}
#   GET  /api/video/{id}    -> {data:{hlsUrl:"https://image.gamebus001.cc/.../index.m3u8", title, duration}}
#   GET  /api/video/categorise -> [{id,name}]
#
# 注意：站内搜索框用的也是 /api/video/page/1（带 keyword 字段），
# 没有独立的 /api/video/search（那个是 404）。
# 详情页播放器用 MSE（blob:），但 hlsUrl 在 API 里直接给了。

GAMEBUS_BASE = os.environ.get("XMF_GAMEBUS_BASE") or "https://gamebus001.cc"


def _http_post_json(url, payload, referer=None, timeout=30):
    """POST JSON（走系统代理）。返回 dict，失败返回 {}。"""
    import tempfile as _tf
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    bodyf = os.path.join(HERE, "data", "_gb_body.json")
    try:
        with io.open(bodyf, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
        cmd = ["curl", "-s", "--proxy", PROXY, "-A", UA,
               "-H", "Content-Type: application/json",
               "--max-time", str(timeout), "-L",
               "-X", "POST", "--data-binary", "@" + bodyf, url]
        if referer:
            cmd[6:6] = ["-e", referer]
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        return json.loads((p.stdout or b"{}").decode("utf-8", "replace"))
    except Exception:
        return {}
    finally:
        try:
            os.unlink(bodyf)
        except Exception:
            pass


def _http_get_json(url, referer=None, timeout=30):
    txt = _http_get(url, referer=referer, timeout=timeout)
    try:
        return json.loads(txt) if txt else {}
    except Exception:
        return {}


def gamebus_search(keyword, limit=12, sort="createdAt"):
    """搜索游戏巴士视频。返回 [{id,title,url,cover,duration,views}]。"""
    kw = str(keyword or "").strip()
    r = _http_post_json("%s/api/video/page/1" % GAMEBUS_BASE,
                        {"keyword": kw, "page": 1, "sort": sort},
                        referer=GAMEBUS_BASE + "/video/")
    data = r.get("data") or {}
    out = []
    for x in (data.get("list") or [])[:limit]:
        vid = x.get("id")
        if not vid:
            continue
        out.append({
            "id": str(vid),
            "title": str(x.get("title") or "")[:140],
            "url": "%s/video/%s" % (GAMEBUS_BASE, vid),
            "cover": x.get("coverUrl") or "",
            "duration": int(x.get("duration") or 0),
            "views": int(x.get("views") or 0),
            "site": "游戏巴士",
        })
    return out


def gamebus_hls(video_id):
    """取某个视频的 hlsUrl。返回 (hls, title, duration)。"""
    r = _http_get_json("%s/api/video/%s" % (GAMEBUS_BASE, video_id),
                       referer=GAMEBUS_BASE + "/video/")
    d = r.get("data") or {}
    return (d.get("hlsUrl") or "").strip(), str(d.get("title") or ""), int(d.get("duration") or 0)


# 「千夏和阿米娅的cos」这种要拆成多个关键词分别搜。
# 不拆的话整串去搜，一个都搜不到（实测：每日大赛搜「千夏和阿米娅」0 条，
# 分开搜「千夏」5 条、「阿米娅」1 条）。
_SPLIT_RE = re.compile(r"和|与|跟|、|,|，|/|／|\+|&|\band\b", re.I)


def split_keywords(s):
    """把「千夏和阿米娅」拆成 ['千夏','阿米娅']；拆不出来就返回单元素。"""
    raw = str(s or "").strip()
    if not raw:
        return []
    parts = [_NOISE_RE.sub("", x).strip() for x in _SPLIT_RE.split(raw)]
    parts = [x for x in parts if x]
    if parts:
        return parts
    one = _NOISE_RE.sub("", raw).strip()
    return [one] if one else [raw]


def detect_route(keyword: str) -> dict:
    """根据用户说法判断该搜哪个站。

    规则（用户明确要求的）：
      · 提到「每日大赛」→ 只搜 album（中文站）
      · 关键词含 cos / コス / コスプレ → cosplaytele + 推特，cosplaytele 优先
      · 其他 → 每日大赛（中文站，能搜中文）

    返回 {sites: [...], keyword: 实际搜索词, converted: [...]}
    """
    kw = str(keyword or "").strip()
    low = kw.lower()

    # 去掉"每日大赛"这类站点指名，剩下的才是搜索词
    cleaned = re.sub(r"(帮我|帮忙|给我|找找|搜|寻找|找)+", "", kw)
    cleaned = re.sub(r"(每日大赛|每日大賽)(里的|里面的|的|里)?", "", cleaned)
    cleaned = re.sub(r"^(一下|下)\s*", "", cleaned).strip()
    explicit_album = ("每日大赛" in kw) or ("每日大賽" in kw)

    has_cos = bool(re.search(r"cos|コス|cosplay|copslay", low))

    if explicit_album and not has_cos:
        return {"sites": ["album"], "keyword": cleaned or kw,
                "converted": [], "keywords": split_keywords(cleaned or kw)}
    if has_cos:
        # 中文转英文（cosplaytele 是英文站）
        conv = cn_to_en(cleaned or kw)
        # cos 类三个来源都查：cosplaytele（优先，用户要求）+ 游戏巴士 + 推特
        # 游戏巴士能直接搜中文，正好补上 cosplaytele 搜不到中文的缺口
        cn_kw = cleaned or kw
        # 游戏巴士的搜索是**短语精确匹配**：搜「胡桃」8 条，搜「胡桃 cos」0 条。
        # 所以给它一个去掉 cos/片/视频 这类噪声的干净词。
        # ⚠️ 游戏巴士的搜索是**连续短语**匹配：
        #    "原神胡桃" → 6 条（全是 cos原神胡桃，精准）
        #    "原神 胡桃" → 0 条（空格直接搜不到）
        # 所以去掉噪声词后还要把空格也删掉。
        cn_clean = re.sub(r"\s+", "", _NOISE_RE.sub("", cn_kw)) or cn_kw
        kws = split_keywords(cn_kw) or [cn_clean]
        # 2026-10-25 修复：**cos 也要搜每日大赛**。
        # 用户反馈「千夏和阿米娅的cos，每日大赛能搜到非常多」，
        # 而旧路由的 cos 分支只有 cosplaytele/gamebus/twitter，压根没搜 album。
        return {"sites": ["cosplaytele", "gamebus", "album", "twitter"],
                "keyword": conv[0] if conv else cleaned,
                "converted": conv,
                "keywords": kws,             # 中文关键词列表（逐个搜）
                "cnKeyword": cn_kw,          # 原样（日志/相关性用）
                "cnSearch": cn_clean}        # 给游戏巴士用（单关键词时）
    # 默认走每日大赛（中文站能搜中文）
    if explicit_album or re.search(r"[\u4e00-\u9fff]", kw):
        return {"sites": ["album"], "keyword": cleaned or kw,
                "converted": [], "keywords": split_keywords(cleaned or kw)}
    # 纯英文无 cos：两个站都试
    return {"sites": ["album", "cosplaytele"], "keyword": cleaned or kw,
            "converted": cn_to_en(kw), "keywords": split_keywords(cleaned or kw)}


# ── 相关性过滤 ──────────────────────────────────────────────────────────
#
# 为什么需要：cosplaytele 的站内搜索是"宽匹配" —— 搜 amiya 会把它最近的
# 8 条全返回（Cyrene / Furina / Castorice…），实测只有 1 条真匹配。
# 不筛就直接把无关内容发给群友了。
#
# 做法：把关键词拆成若干"必须命中"的 token（中文原词 + 英文/日文转换），
# 标题或 URL 里命中任意一个才算相关。

_NOISE_RE = re.compile(
    r"cosplay|cos|コスプレ|コス|的片|的|片|视频|資源|资源|写真|套图|合集", re.I)


def _norm(s):
    """归一化：小写 + 只留字母数字和汉字（去掉空格/连字符/下划线）。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(s or "").lower())


def build_match_tokens(plan):
    """从路由结果里抽出用于相关性判断的 token 列表。

    返回空列表 = 不做过滤（关键词本身没有可辨识成分时，宁可全给也别全杀）。
    """
    toks = set()

    # 多个中文关键词各自成 token（「千夏和阿米娅」→ 千夏 / 阿米娅）
    for one in (plan.get("keywords") or []):
        cc = _NOISE_RE.sub("", str(one or "")).strip()
        if cc:
            toks.add(_norm(cc))
    cn = _NOISE_RE.sub("", str(plan.get("cnKeyword") or "")).strip()
    if cn and not plan.get("keywords"):
        toks.add(_norm(cn))

    for c in (plan.get("converted") or []):
        cc = _NOISE_RE.sub("", str(c or "")).strip()
        if cc:
            toks.add(_norm(cc))

    k = _NOISE_RE.sub("", str(plan.get("keyword") or "")).strip()
    if k:
        toks.add(_norm(k))

    # 太短的 token（如 "a"、"ab"）会误伤，直接丢掉
    out = [t for t in toks if len(t) >= 3]
    # 汉字两个字也算有意义（如「初音」），单独放行
    out += [t for t in toks if len(t) == 2 and re.search(r"[\u4e00-\u9fff]", t)]
    return sorted(set(out))


def _latin_pattern(token):
    """给英文 token 生成"词边界 + 允许空格/连字符"的正则。

    为什么不能直接 `token in text`：
      搜「胡桃」→ token = hutao，而 coser 叫 **MeeHutao**，
      子串匹配会把「MeeHutao cosplay Cheshire (Azur Lane)」当成胡桃的内容。
      加上左边界 `(?<![a-z0-9])` 后，"Hutao" 前面是 "e" 就不再命中。

    同时允许字间分隔（见下面的正则），
    这样「Hu Tao」「hu_tao」都能匹配上。
    """
    body = r"[\s\-_]*".join(re.escape(ch) for ch in token)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])", re.I)


_TOKEN_PAT_CACHE = {}


def relevance_score(it, tokens):
    """0 = 不相关；1 = 相关；2 = 强相关（token 出现在 cos 之后的角色位）。

    为什么需要分级：游戏巴士的标题是「推特福利姬 绫衣胡桃 JK红绳…」这种，
    coser 名里就带「胡桃」，光靠布尔判断分不出"角色胡桃"和"coser 叫胡桃"。
    但真正的角色内容通常写成「cos原神胡桃」「cos土御门胡桃」——
    token 落在 "cos" 之后，给高分排前面。
    """
    if not tokens:
        return 1
    if not is_relevant(it, tokens):
        return 0
    title = str(it.get("title") or "")
    low = title.lower()
    idx = low.find("cos")
    if idx >= 0:
        tail = title[idx:]
        for t in tokens:
            if re.search(r"[\u4e00-\u9fff]", t):
                if t in tail:
                    return 2
            else:
                pat = _TOKEN_PAT_CACHE.get(t) or _latin_pattern(t)
                _TOKEN_PAT_CACHE[t] = pat
                if pat.search(tail):
                    return 2
    return 1


def is_relevant(it, tokens):
    """标题 / URL / 描述里命中任意 token 即算相关。

    中文 token 走子串匹配（汉字没有词边界问题）；
    英文 token 走词边界匹配（避免 MeeHutao 这类误判）。
    """
    if not tokens:
        return True
    title = str(it.get("title") or "")
    url = str(it.get("url") or "")
    desc = str(it.get("desc") or "")

    # cosplaytele 的标题格式是「{coser名} ({handle}) cosplay {角色} — {作品}」。
    # 前半段是**扮演者**的名字，不能拿来判断角色相关性 ——
    # 实测搜「胡桃」会命中 coser 叫「绫衣胡桃MeeHutao」的 Cheshire/Rem 帖子。
    # 只要 cosplay 前面是「... (handle)」这种形态，就只取 cosplay 之后的部分。
    head = title

    # ① 先剥掉标题开头的「{coser名} ({handle})」前缀。
    #    handle 判定：括号内是纯 ASCII 的短串（MeeHuTao69 / yomilkki / tiny.asababy）。
    #    这一步对**没有 cosplay 字样**的标题也生效 —— 实测
    #    「绫衣胡桃MeeHutao (MeeHuTao69) — Naked —」就是因为没有 cosplay
    #    而漏过了上一版过滤，把 coser 名里的"胡桃"当成了角色名。
    pm = re.match(r"^(.{0,60}?)\(([^)]{2,40})\)\s*[-—–:]?\s*(.*)$", title, re.S)
    if pm and re.fullmatch(r"[A-Za-z0-9_.\-]+", pm.group(2) or ""):
        head = pm.group(3) or ""

    # ② 再处理 cosplay 字样：前半段若是扮演者信息就丢掉
    m = re.search(r"cosplay", head, re.I)
    if m:
        pre = head[:m.start()].strip()
        if re.search(r"\([^)]{2,}\)\s*$", pre):
            head = head[m.start():]

    hay_norm = _norm(head) + "|" + _norm(url) + "|" + _norm(desc)
    hay_raw = head + " | " + url + " | " + desc
    for t in tokens:
        if re.search(r"[\u4e00-\u9fff]", t):
            # 中文：归一化后子串匹配
            if t in hay_norm:
                return True
        else:
            pat = _TOKEN_PAT_CACHE.get(t)
            if pat is None:
                pat = _latin_pattern(t)
                _TOKEN_PAT_CACHE[t] = pat
            if pat.search(hay_raw):
                return True
    return False


def filter_relevant(items, tokens, per_site_limit=3, exclude=None):
    """按相关性 + 每站限额筛。exclude 是已展示过的 URL 集合（「换一批」用）。"""
    exclude = exclude or set()
    dropped_irrelevant = 0
    dropped_seen = 0
    scored = []
    for idx, it in enumerate(items):
        u = str(it.get("url") or "")
        if u and u in exclude:
            dropped_seen += 1
            continue
        sc = relevance_score(it, tokens)
        if sc <= 0:
            dropped_irrelevant += 1
            continue
        scored.append((sc, idx, it))

    # 强相关优先（同分保持原顺序），再按站点限额
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    per_site = {}
    for sc, idx, it in scored:
        site = str(it.get("site") or "其他")
        if per_site_limit > 0 and per_site.get(site, 0) >= per_site_limit:
            continue
        per_site[site] = per_site.get(site, 0) + 1
        # __score 只用于排序，不要带出去（会污染快照 JSON 和卡片渲染）
        out.append(it)
    if dropped_irrelevant or dropped_seen:
        sys.stderr.write("[relevance] 关键词=%s 丢弃无关 %d 条 / 已看过 %d 条，保留 %d 条\n"
                         % (tokens, dropped_irrelevant, dropped_seen, len(out)))
    return out


# ── 每日大赛（album.sumjpknqx.cc）────────────────────────────────────────────
#
# 这个站和 cosplaytele 完全不同：
#   · Typecho 博客，站内搜索 /search/<kw>/ 纯 HTTP 就能拿（不用浏览器）
#   · 视频地址**直接写在 HTML 里**：DPlayer 的 data-config 属性
#       data-config='{"video":{"url":"https://hls.xxx/videos5/<id>/<id>.m3u8?auth_key=..."}}'
#   · HLS 有 AES-128 加密（EXT-X-KEY:METHOD=AES-128），但 ffmpeg 原生支持，
#     不用自己解密 —— 实测 478MB 视频 107 秒下完
#   · 每篇文章可能**多个视频**（多个 .dplayer 块）
#   · 标签在 data-video_tag_name 里（如 "推特,自慰,COS,每日大赛,CRYBABY0430"），
#     很适合做相关性判断
#   · 正文夹着大量广告链接，内容图在 pic.sugfew.cn/upload_0X/...

# 域名会换（实测 sumjpknqx.cc -> tadqmtxhg.cc），导航页专门发布最新地址。
# 策略：优先用缓存 -> 缓存过期或失效时从导航页抓 -> 都失败用硬编码兜底。
NAV_URL = os.environ.get("XMF_NAV_URL") or "https://www.wijpokci.xyz/"
# 硬编码兜底（2026-10-22 实测可用）
FALLBACK_BASES = [
    # 2026-10-25 实测：tadqmtxhg.cc / sumjpknqx.cc 全部 HTTP 000（域名已死）。
    # 从导航页 www.wijpokci.xyz 渲染后取到新域名 nsindvvb.cc（三条线路都通，200/262KB）。
    "https://bed.nsindvvb.cc",
    "https://attempt.nsindvvb.cc",
    "https://benefit.nsindvvb.cc",
    "https://accuse.tadqmtxhg.cc",
    "https://album.sumjpknqx.cc",
]
_DOMAIN_CACHE = os.path.join(HERE, "data", "album_domain.json")
_DOMAIN_TTL = 6 * 3600   # 6 小时


def _read_domain_cache():
    try:
        with io.open(_DOMAIN_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - float(d.get("at") or 0) < _DOMAIN_TTL:
            return d.get("base") or ""
    except Exception:
        pass
    return ""


def _write_domain_cache(base):
    try:
        os.makedirs(os.path.dirname(_DOMAIN_CACHE), exist_ok=True)
        with io.open(_DOMAIN_CACHE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"base": base, "at": time.time()}, ensure_ascii=False))
    except Exception:
        pass


def _probe_base(base, timeout=12):
    """验证某个域名真的能用（能搜到文章）。"""
    try:
        h = _http_get("%s/search/cos/" % base, timeout=timeout)
        return bool(re.search(r'/archives/\d+/', h))
    except Exception:
        return False


# ── 导航页自动发现新域名 ────────────────────────────────────────────────
#
# 为什么需要：这类站域名会换（2026-10-25 实测 tadqmtxhg.cc 全死，
# 换成 nsindvvb.cc）。候选池全死时，光靠探测救不回来，必须去导航页
# 问"现在的地址是什么"。
#
# 导航页 www.wijpokci.xyz 是纯 JS 渲染的，静态抓不到 —— 必须用浏览器。
# 但它渲染出来就是纯文本，形如：
#   每日大赛-线路一
#   bed.nsindvvb.cc
# 所以用 Playwright 打开，抓 innerText + 所有 a[href]，正则捞 *.cc 域名。
NAV_DOMAINS_CACHE = os.path.join(HERE, "data", "_album_domains.json")


def discover_from_nav(timeout_ms=45000):
    """渲染导航页，返回找到的可用 base 列表（按线路顺序）。

    失败返回 []。调用方负责写缓存。
    """
    script = os.path.join(PW_MODULE, "_nav_probe.mjs")
    js = r'''
import { chromium } from "playwright";
const navUrl = process.argv[2];
const out = { domains: [], error: null };
const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1400, height: 1000 }, locale: "zh-CN"
  });
  const page = await ctx.newPage();
  await page.goto(navUrl, { waitUntil: "domcontentloaded", timeout: 40000 });
  await page.waitForTimeout(5000);
  const set = new Set();
  // ① 从链接里取
  const links = await page.evaluate(() =>
    Array.from(document.querySelectorAll("a[href]")).map(a => a.href));
  for (const h of links) {
    try {
      const u = new URL(h);
      if (/\.(cc|com|net|xyz|vip|top|site|org|me|tv|io)$/i.test(u.hostname)) {
        set.add(u.origin);
      }
    } catch {}
  }
  // ② 从正文里取（导航页把域名写成纯文本）
  const text = await page.evaluate(() => document.body.innerText);
  const re = /(?:https?:\/\/)?([a-z0-9][a-z0-9\-]{1,40}\.(?:cc|com|net|xyz|vip|top|site|org|me|tv|io))\b/gi;
  let m;
  while ((m = re.exec(text)) !== null) {
    const host = m[1].toLowerCase();
    if (host.includes("wijpokci") || host.includes("google") || host.includes("cloudflare")) continue;
    set.add("https://" + host);
  }
  out.domains = Array.from(set);
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
console.log(JSON.stringify(out));
'''
    try:
        os.makedirs(PW_MODULE, exist_ok=True)
        with io.open(script, "w", encoding="utf-8") as f:
            f.write(js)
        env = dict(os.environ)
        env["XMF_PROXY"] = PROXY
        env["XMF_UA"] = UA
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
        p2 = subprocess.run([NODE_EXE, script, NAV_URL],
                            capture_output=True, timeout=int(timeout_ms / 1000) + 30,
                            cwd=PW_MODULE, env=env)
        raw = (p2.stdout or b"").decode("utf-8", "replace").strip()
        line = raw.split("\n")[-1] if raw else ""
        data = json.loads(line) if line.startswith("{") else {}
        doms = data.get("domains") or []
        if data.get("error"):
            sys.stderr.write("[album-nav] 渲染失败：%s\n" % data["error"])
        return doms
    except Exception as e:
        sys.stderr.write("[album-nav] 异常：%s\n" % str(e)[:150])
        return []


def refresh_album_bases():
    """从导航页刷新候选池，返回可用 base 列表（已写缓存）。"""
    doms = discover_from_nav()
    if not doms:
        return []
    alive = []
    for d in doms[:12]:          # 最多试 12 个，别太慢
        if _probe_base(d):
            alive.append(d)
            sys.stderr.write("[album-nav] ✅ %s\n" % d)
        else:
            sys.stderr.write("[album-nav] ✗ %s\n" % d)
        if len(alive) >= 3:      # 三个线路够用了
            break
    if alive:
        try:
            os.makedirs(os.path.dirname(NAV_DOMAINS_CACHE), exist_ok=True)
            io.open(NAV_DOMAINS_CACHE, "w", encoding="utf-8").write(
                json.dumps({"at": int(time.time()), "bases": alive}, ensure_ascii=False))
        except Exception:
            pass
    return alive


def _load_nav_cache():
    """读导航页发现的域名缓存（1 天内有效）。"""
    try:
        d = json.loads(io.open(NAV_DOMAINS_CACHE, encoding="utf-8").read())
        if int(time.time()) - int(d.get("at") or 0) < 86400:
            return d.get("bases") or []
    except Exception:
        pass
    return []


def discover_album_base(force=False):
    """取当前可用的每日大赛域名。

    导航页 www.wijpokci.xyz 是纯 JS 渲染的，静态抓不到链接 ——
    实测它把地址 Base64 + JS 解密后写进 DOM。
    所以这里**不解析导航页**，而是直接探测导航页里出现过的候选域名
    （把它们作为候选池），哪个能通就用哪个。这样更稳：
    导航页改版也不会影响，只要候选池里有活域名就行。
    """
    if not force:
        c = _read_domain_cache()
        if c:
            return c
    # ① 先试导航页缓存（上次成功发现的新域名）
    for base in _load_nav_cache():
        if _probe_base(base):
            _write_domain_cache(base)
            return base

    # ② 再试硬编码候选池
    for base in FALLBACK_BASES:
        if _probe_base(base):
            _write_domain_cache(base)
            return base

    # ③ 候选池全死 —— 去导航页问现在的地址
    #    （2026-10-25 实测：tadqmtxhg.cc 全挂，就是靠这一步救回来的）
    alive = refresh_album_bases()
    if alive:
        _write_domain_cache(alive[0])
        return alive[0]

    # ④ 彻底没办法：返回第一个，让上层报错（比静默用坏的强）
    return FALLBACK_BASES[0]


# 首次导入时尝试解析（失败就用兜底，不阻塞）
ALBUM_BASE = os.environ.get("XMF_ALBUM_BASE") or ""

# 正则里要匹配单双引号，用 chr() 拼避免多层转义把自己绕死
_Q = chr(34)
_AQ = chr(39)
_QUOTES = "[" + _Q + _AQ + "]"   # 字符类 ["']，匹配任意一种引号

# 2026-10-25 实测新结构：
#   <meta itemprop="url mainEntityOfPage" content="/archives/196041/" />
#   <a href="/archives/196041/" >
#       <div class="post-card" id="post-card-196041" >
#   标题在卡片内部（h2/h3/.post-card-title 之类），不在 <a> 的紧邻文本里，
#   所以这里只负责**抽 id 和链接**，标题另外从卡片区块里取（见 album_search）。
RE_ARCHIVE = re.compile(
    r'<a[^>]+href=' + _QUOTES + r'(?:https?://[a-z0-9.\-]+)?'
    r'(/archives/(\d+)/?)' + _QUOTES + r'[^>]*>', re.I)

RE_DPLAYER = re.compile(
    r'<div[^>]*class=' + _QUOTES + r'[^' + _Q + _AQ + r']*dplayer[^' + _Q + _AQ + r']*'
    + _QUOTES + r'([^>]*)>', re.I)


def _attr(attrs, name):
    """从一段 HTML 属性串里取 attr=value（单双引号都认）。"""
    m = re.search(name + r'=' + _QUOTES + r'([^' + _Q + _AQ + r'>]+)', attrs, re.I)
    return m.group(1).strip() if m else ""


def _http_get(url, referer=None, timeout=45):
    """纯 HTTP GET（走系统代理）。返回文本，失败返回空串。"""
    cmd = ["curl", "-s", "--proxy", PROXY, "-A", UA, "--max-time", str(timeout), "-L"]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        return (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def quote_safe(s):
    from urllib.parse import quote
    return quote(str(s), safe="")


def _album_base():
    """惰性解析域名（首次调用时才探测，避免导入就卡住）。"""
    global ALBUM_BASE
    if not ALBUM_BASE:
        ALBUM_BASE = discover_album_base()
    return ALBUM_BASE.rstrip("/")


def album_search(keyword, limit=12, pages=1):
    """在每日大赛站内搜索（支持翻页）。返回 [{url,title,id,cover}]。

    实测结构（2026-10-22）：
        <a href="/archives/198200/" >
            <div class="post-card" id="post-card-198200">
                <script>loadBannerDirect('https://pic.sugfew.cn/...jpeg', ...)</script>
                ... 标题文字 ...
    所以不能指望 <a> 里直接有标题 —— 要先定位 <a href="/archives/N/">，
    再往后截一段（到下一个 <a href="/archives/ 为止）找标题和封面。

    分页格式（实测）：`/search/<kw>/<page>/` 而不是 `/search/<kw>/page/<n>/`。
      第1页 /search/自慰/         -> 198218,198212,...
      第2页 /search/自慰/2/       -> 198112,198088,...（不同）
    注意 /search/<kw>?page=2 是**无效**的（返回第1页同样内容），
    所以必须用路径形式。
    """
    kw = str(keyword or "").strip()
    if not kw:
        return []
    base = _album_base()
    all_items = []
    seen_ids = set()
    pages = max(1, min(int(pages or 1), 20))
    for pg in range(1, pages + 1):
        if pg == 1:
            url = "%s/search/%s/" % (base, quote_safe(kw))
        else:
            url = "%s/search/%s/%d/" % (base, quote_safe(kw), pg)
        html = _http_get(url)
        if not html:
            break
        got = _parse_album_list(html, base)
        if not got:
            break   # 没有更多页了
        new = 0
        for it in got:
            if it["id"] in seen_ids:
                continue
            seen_ids.add(it["id"])
            all_items.append(it)
            new += 1
        if new == 0:
            break   # 这页全是重复 -> 已经到底
        if len(all_items) >= limit:
            break
    # 注意：搜索阶段**不取封面**（列表页要解密 30 张图，很慢）。
    # 封面由上层按需调用 fetch_detail_covers 取详情页正文图，
    # 而且有 cov_<id>.jpg 缓存，同样的条目不会重复取。
    return all_items[:limit]


COVER_JS = r"""
import { chromium } from "playwright";
import fs from "node:fs";

const searchUrl = process.argv[2];
const outDir = process.argv[3];
const want = JSON.parse(process.argv[4] || "[]");   // 想要的 id 列表
const out = { covers: [], error: null };

const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--disable-blink-features=AutomationControlled","--no-sandbox"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1366, height: 900 }, locale: "zh-CN"
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = await ctx.newPage();
  await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  // 等封面解密完成（load-success 是解密成功的标记）
  await page.waitForFunction(
    () => document.querySelectorAll(".blog-background.load-success").length > 0,
    { timeout: 25000 }
  ).catch(() => {});
  await page.waitForTimeout(2500);

  const cards = await page.evaluate(() => {
    const res = [];
    for (const card of document.querySelectorAll(".post-card")) {
      const bg = card.querySelector(".blog-background");
      if (!bg) continue;
      const st = bg.style.backgroundImage || getComputedStyle(bg).backgroundImage || "";
      const m = st.match(/url\(["']?(data:image\/[^"')]+)["']?\)/);
      res.push({
        id: (card.id || "").replace("post-card-", ""),
        data: m ? m[1] : ""
      });
    }
    return res;
  });

  fs.mkdirSync(outDir, { recursive: true });
  for (const c of cards) {
    if (!c.data || !c.data.startsWith("data:image")) continue;
    if (want.length && !want.includes(c.id)) continue;
    const b64 = c.data.split(",")[1] || "";
    if (b64.length < 2000) continue;
    try {
      const buf = Buffer.from(b64, "base64");
      const p = outDir + "\\cov_" + c.id + ".jpg";
      fs.writeFileSync(p, buf);
      out.covers.push({ id: c.id, path: p, bytes: buf.length });
    } catch (e) { /* skip */ }
  }
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
console.log("@@JSON@@" + JSON.stringify(out));
"""


DETAIL_COVER_JS = r"""
import { chromium } from "playwright";
import fs from "node:fs";

const jobs = JSON.parse(process.argv[2] || "[]");   // [{id, url}]
const outDir = process.argv[3];
const out = { covers: [], error: null };

const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--disable-blink-features=AutomationControlled","--no-sandbox"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1366, height: 900 }, locale: "zh-CN"
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = await ctx.newPage();
  fs.mkdirSync(outDir, { recursive: true });

  for (const job of jobs) {
    try {
      await page.goto(job.url, { waitUntil: "domcontentloaded", timeout: 45000 });
      // 等正文图解密（.post-content 里出现 data: 开头的 img）
      await page.waitForFunction(() => {
        const b = document.querySelector(".post-content");
        if (!b) return false;
        return [...b.querySelectorAll("img")].some(i => (i.src || "").startsWith("data:image"));
      }, { timeout: 20000 }).catch(() => {});
      await page.waitForTimeout(1200);

      const src = await page.evaluate(() => {
        const body = document.querySelector(".post-content");
        if (!body) return null;
        const imgs = [...body.querySelectorAll("img")].filter(i => (i.src || "").startsWith("data:image"));
        // 排除广告条（950x110 那种），优先取最大的一张
        const good = imgs.filter(i => i.naturalWidth >= 600 && i.naturalHeight >= 300);
        const pool = good.length ? good : imgs;
        if (!pool.length) return null;
        pool.sort((a, b) => (b.naturalWidth * b.naturalHeight) - (a.naturalWidth * a.naturalHeight));
        return pool[0].src;
      });
      if (src && src.startsWith("data:image")) {
        const b64 = src.split(",")[1] || "";
        if (b64.length > 2000) {
          const buf = Buffer.from(b64, "base64");
          const p = outDir + "\\cov_" + job.id + ".jpg";
          fs.writeFileSync(p, buf);
          out.covers.push({ id: job.id, path: p, bytes: buf.length });
        }
      }
    } catch (e) { /* 单条失败不影响其他 */ }
  }
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
console.log("@@JSON@@" + JSON.stringify(out));
"""


COSPLAYTELE_COVER_JS = r"""
import { chromium } from "playwright";
import fs from "node:fs";

const jobs = JSON.parse(process.argv[2] || "[]");   // [{id, url}]
const outDir = process.argv[3];
const out = { covers: [], error: null };

const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--disable-blink-features=AutomationControlled","--no-sandbox"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1366, height: 900 }, locale: "zh-CN"
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = await ctx.newPage();
  fs.mkdirSync(outDir, { recursive: true });

  for (const job of jobs) {
    try {
      // ⚠️ 单页 22 秒，不是 45 秒。
      // 实测 cosplaytele 卡住时每页耗满 45 秒，4 条就 180 秒 > 总预算，
      // 结果整批封面被丢弃，卡片里 A1-A4 全都没预览图。
      //
      // 再加一层重试：网络经代理会偶发超时/SSL 错（curl 侧实测 rc=28/35），
      // 同一个 URL 第二次通常就通。重试时必须用**新 page** —— 超时后的
      // page 状态是坏的，继续用只会连环失败。
      let navOk = false;
      for (let attempt = 0; attempt < 2 && !navOk; attempt += 1) {
        try {
          await page.goto(job.url, { waitUntil: "domcontentloaded", timeout: 22000 });
          navOk = true;
        } catch (e) {
          if (attempt === 1) throw e;
          try { await page.goto("about:blank", { timeout: 5000 }); } catch {}
        }
      }
      await page.waitForTimeout(1200);
      // cosplaytele 的正文图在 .entry-content 里（实测 2 张 900x1600）
      const src = await page.evaluate(() => {
        const box = document.querySelector(".entry-content") || document.querySelector("article");
        if (!box) return null;
        const imgs = [...box.querySelectorAll("img")]
          .filter(i => i.naturalWidth > 300 && /^https?:/.test(i.src || ""));
        if (!imgs.length) return null;
        // 取最大的一张当封面
        imgs.sort((a, b) => (b.naturalWidth * b.naturalHeight) - (a.naturalWidth * a.naturalHeight));
        return imgs[0].src;
      });
      if (src) {
        // 直接在页面里 fetch 二进制，避免再起一次请求被防盗链
        const b64 = await page.evaluate(async (u) => {
          try {
            const r = await fetch(u);
            const buf = await r.arrayBuffer();
            let binary = "";
            const bytes = new Uint8Array(buf);
            for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
            return btoa(binary);
          } catch (e) { return null; }
        }, src);
        if (b64 && b64.length > 2000) {
          const buf = Buffer.from(b64, "base64");
          const p = outDir + "\\ct_" + job.id + ".jpg";
          fs.writeFileSync(p, buf);
          out.covers.push({ id: job.id, path: p, bytes: buf.length });
        }
      }
    } catch (e) { /* 单条失败跳过 */ }
  }
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
console.log("@@JSON@@" + JSON.stringify(out));
"""


def fetch_cosplaytele_covers(items, outdir=None):
    """给 cosplaytele 条目取封面（进详情页拿正文第一张图）。

    cosplaytele 的搜索接口不返回封面，必须进详情页。
    实测：正文图在 .entry-content 里（2 张 900x1600），
    页面 main 里还有 16 张但多数是推荐位，所以只认 .entry-content。
    """
    if not items:
        return 0
    outdir = outdir or os.path.join(HERE, "data", "covers")
    os.makedirs(outdir, exist_ok=True)

    jobs = []
    for it in items:
        # 用 url 的路径部分当 id（cosplaytele 没数字 id）
        u = str(it.get("url") or "")
        tag = re.sub(r"[^A-Za-z0-9]", "", u.split("cosplaytele.com")[-1])[:30] or "x"
        p = os.path.join(outdir, "ct_%s.jpg" % tag)
        if os.path.exists(p) and os.path.getsize(p) > 2000:
            it["coverLocal"] = p
        elif u:
            jobs.append({"id": tag, "url": u})
    if not jobs:
        return sum(1 for it in items if it.get("coverLocal"))

    try:
        r = _run_node(COSPLAYTELE_COVER_JS, [json.dumps(jobs), outdir],
                      timeout=90 + 28 * len(jobs))
    except Exception:
        return sum(1 for it in items if it.get("coverLocal"))

    by_id = {str(c.get("id")): c.get("path") for c in (r.get("covers") or [])}
    for it in items:
        u = str(it.get("url") or "")
        tag = re.sub(r"[^A-Za-z0-9]", "", u.split("cosplaytele.com")[-1])[:30] or "x"
        p = by_id.get(tag)
        if p and os.path.exists(p):
            it["coverLocal"] = p
    return sum(1 for it in items if it.get("coverLocal"))


def fetch_gamebus_covers(items, outdir=None):
    """给游戏巴士条目取封面。

    这个最简单：搜索返回的 JSON 里就有 coverUrl，
    实测能直接下载（content-type: image/webp，76KB）。
    """
    if not items:
        return 0
    outdir = outdir or os.path.join(HERE, "data", "covers")
    os.makedirs(outdir, exist_ok=True)
    got = 0
    for it in items:
        cov = str(it.get("cover") or "")
        if not cov.startswith("http"):
            continue
        dest = os.path.join(outdir, "gb_%s.webp" % str(it.get("id") or "x")[:20])
        if os.path.exists(dest) and os.path.getsize(dest) > 2000:
            it["coverLocal"] = dest
            got += 1
            continue
        buf = _http_get_binary(cov, referer=GAMEBUS_BASE + "/video/", timeout=25)
        if buf and len(buf) > 2000:
            with open(dest, "wb") as f:
                f.write(buf)
            it["coverLocal"] = dest
            got += 1
    return got


def _http_get_binary(url, referer=None, timeout=25):
    """下载二进制内容（走代理）。"""
    cmd = ["curl", "-s", "--proxy", PROXY, "-A", UA, "--max-time", str(timeout), "-L"]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        return p.stdout or b""
    except Exception:
        return b""


def fetch_all_covers(items, outdir=None):
    """按站点分派：每日大赛走详情页、cosplaytele 走 .entry-content、
    游戏巴士走 coverUrl。三个站一起处理，返回拿到图的数量。

    ⚠️ 2026-10-25 修复：以前只按 URL 子串分派，URL 形态一变（相对路径、
    镜像域名）就整批漏掉；而且任何异常都被吞成 0，卡片里就"没有预览图"
    且完全不知道为什么。现在：
      · 分派同时看 url 和 it["site"]
      · 各站拿完后再做一次**通用 HTTP 兜底**（items 里带 cover URL 的直接下）
      · 失败写 stderr，上层能看见
    """
    if not items:
        return 0
    outdir = outdir or os.path.join(HERE, "data", "covers")

    def _is_album(it):
        u = str(it.get("url") or "")
        return "/archives/" in u or str(it.get("site") or "") == "每日大赛"

    def _is_ct(it):
        u = str(it.get("url") or "")
        return "cosplaytele.com" in u or str(it.get("site") or "") == "cosplaytele"

    def _is_gb(it):
        u = str(it.get("url") or "")
        return "gamebus001.cc" in u or str(it.get("site") or "") == "游戏巴士"

    album = [it for it in items if _is_album(it)]
    ct = [it for it in items if _is_ct(it)]
    gb = [it for it in items if _is_gb(it)]
    n = 0
    for name, group, fn in (("每日大赛", album, fetch_detail_covers),
                            ("cosplaytele", ct, fetch_cosplaytele_covers),
                            ("游戏巴士", gb, fetch_gamebus_covers)):
        if not group:
            continue
        try:
            got = fn(group, outdir)
            n += got
            sys.stderr.write("[cover] %s：%d/%d\n" % (name, got, len(group)))
        except Exception as e:
            sys.stderr.write("[cover] %s 失败：%s\n" % (name, str(e)[:160]))

    # 通用兜底：还没封面的，只要条目自带 cover URL 就直接 HTTP 下（不走浏览器）
    for it in items:
        if it.get("coverLocal"):
            continue
        cu = str(it.get("cover") or "").strip()
        if not cu.startswith("http"):
            continue
        aid = str(it.get("id") or "") or re.sub(r"[^A-Za-z0-9]", "", cu)[-16:]
        dst = os.path.join(outdir, "gen_%s.jpg" % aid)
        if os.path.exists(dst) and os.path.getsize(dst) > 2000:
            it["coverLocal"] = dst
            n += 1
            continue
        try:
            ok = _download_file(cu, dst)
            if ok and os.path.exists(dst) and os.path.getsize(dst) > 2000:
                it["coverLocal"] = dst
                n += 1
        except Exception:
            pass

    missing = sum(1 for it in items if not it.get("coverLocal"))
    if missing:
        sys.stderr.write("[cover] 仍有 %d/%d 条没封面\n" % (missing, len(items)))
    return n


def fetch_detail_covers(items, outdir=None):
    """为条目取**详情页正文第一张图**当预览。

    为什么走详情页而不是列表页：
      · 列表页要解密 30 张封面（每张都进 CSS background），慢且重
      · 详情页正文图本来就在 .post-content 里已解密，取第一张即可
      · 实测详情页 7 张正文图（800x450），另外 34 张 950x110 是广告条，
        按尺寸过滤能稳定排除广告

    缓存：按 id 存 cov_<id>.jpg，重复搜索不重取。
    """
    if not items:
        return 0
    outdir = outdir or os.path.join(HERE, "data", "covers")
    os.makedirs(outdir, exist_ok=True)

    jobs = []
    for it in items:
        aid = str(it.get("id") or "")
        if not aid:
            continue
        p = os.path.join(outdir, "cov_%s.jpg" % aid)
        if os.path.exists(p) and os.path.getsize(p) > 2000:
            it["coverLocal"] = p
        else:
            jobs.append({"id": aid, "url": it.get("url") or ""})
    if not jobs:
        return sum(1 for it in items if it.get("coverLocal"))

    try:
        r = _run_node(DETAIL_COVER_JS, [json.dumps(jobs), outdir], timeout=90 + 30 * len(jobs))
    except Exception:
        return sum(1 for it in items if it.get("coverLocal"))

    by_id = {str(c.get("id")): c.get("path") for c in (r.get("covers") or [])}
    for it in items:
        p = by_id.get(str(it.get("id") or ""))
        if p and os.path.exists(p):
            it["coverLocal"] = p
    return sum(1 for it in items if it.get("coverLocal"))


def cache_covers(items, outdir=None):
    """给候选条目补本地封面图（it["coverLocal"]）。

    关键发现（2026-10-22，踩了很久）：
      pic.sugfew.cn 的图**不能直接下载** —— 返回 Content-Type:
      binary/octet-stream，内容是加密的（所有文件头都是 3eaa708e，
      不是任何图片格式）。SnowLuma 发这种文件会报
      highway upload error_code=921。

      真正的图由页面 JS 解密后写进 CSS：
        <div class="blog-background load-success"
             style="background-image: url(&quot;data:image/jpeg;base64,...&quot;)">

      所以必须用 Playwright 打开搜索页，等 load-success 出现，
      再从 background-image 里取出 base64 落盘。这样拿到的是
      有效 JPEG（实测 1300x640、137-239KB）。
    """
    if not items:
        return 0
    outdir = outdir or os.path.join(HERE, "data", "covers")
    os.makedirs(outdir, exist_ok=True)

    # 已有本地缓存就跳过（按 id 命名，重复搜索不用重下）
    missing = []
    for it in items:
        aid = str(it.get("id") or "")
        p = os.path.join(outdir, "cov_%s.jpg" % aid)
        if os.path.exists(p) and os.path.getsize(p) > 2000:
            it["coverLocal"] = p
        elif aid:
            missing.append(aid)
    if not missing:
        return len(items)

    # 用 Playwright 批量取（一次开页拿全部，比逐张快得多）
    base = _album_base()
    kw = str(items[0].get("_kw") or "")
    if not kw:
        return len(items) - len(missing)
    url = "%s/search/%s/" % (base, quote_safe(kw))
    try:
        r = _run_node(COVER_JS, [url, outdir, json.dumps(missing)], timeout=180)
    except Exception:
        return len(items) - len(missing)

    by_id = {}
    for c in (r.get("covers") or []):
        by_id[str(c.get("id"))] = c.get("path")
    for it in items:
        p = by_id.get(str(it.get("id") or ""))
        if p and os.path.exists(p):
            it["coverLocal"] = p
    return sum(1 for it in items if it.get("coverLocal"))


def _parse_album_list(html, base):
    """解析一页搜索结果，返回 [{url,title,id,cover}]。"""
    if not html:
        return []

    # 找出所有文章入口的位置（域名不固定，所以用相对路径匹配）
    entries = [(m.start(), m.group(1)) for m in
               re.finditer(r'<a[^>]+href="(?:https?://[^"]+)?/archives/(\d+)/"', html)]
    if not entries:
        return []

    items, seen = [], set()
    for idx, (pos, aid) in enumerate(entries):
        if aid in seen:
            continue
        # 这一段 = 本文卡片（到下一个入口前）
        nxt = entries[idx + 1][0] if idx + 1 < len(entries) else len(html)
        chunk = html[pos:nxt]

        # 封面：loadBannerDirect('URL' 或 <img src/data-src>
        cover = ""
        cm = re.search(r"loadBannerDirect\(['\"]([^'\"]+)", chunk)
        if cm:
            cover = cm.group(1)
        if not cover:
            # 缩略图也可能在 <img> 或 CSS background
            bm = re.search(r"url\(['\"]?(https?://[^'\")]+\.(?:jpe?g|png|webp|gif))", chunk, re.I)
            if bm:
                cover = bm.group(1)
        if not cover:
            im = re.search(r'<img[^>]+(?:data-src|src)=["\']([^"\']+)["\']', chunk)
            if im:
                cover = im.group(1)

        # 标题：优先 h1/h2/h3 里的文本，其次 title= 属性
        title = ""
        tm = re.search(r"<(h[1-4])[^>]*>([\s\S]{0,200}?)</\1>", chunk, re.I)
        if tm:
            title = re.sub(r"<[^>]+>", "", tm.group(2)).strip()
        if not title:
            tt = re.search(r'title=["\']([^"\']{4,120})["\']', chunk)
            if tt:
                title = tt.group(1).strip()
        if not title:
            # 兜底：整个 chunk 去掉 script/style 后的最长中文片段
            plain = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", chunk, flags=re.I)
            plain = re.sub(r"<[^>]+>", " ", plain)
            plain = re.sub(r"\s+", " ", plain).strip()
            cand = [x for x in plain.split(" ") if len(x) >= 6]
            title = max(cand, key=len) if cand else ""

        if not title:
            continue
        seen.add(aid)
        items.append({
            "url": base + "/archives/%s/" % aid,
            "title": title[:120],
            "id": aid,
            "cover": cover,
        })
    return items


def album_parse_videos(article_url):
    """解析文章页里的所有 DPlayer 块。返回 (page_title, videos)。

    ⚠️ 不能用 <div ...>` 去截属性串：data-config 里的 JSON 含 `>` 字符
       （比如 URL 里的 ->），`[^>]*` 会在那里断掉，导致抓不到 config。
       改成直接搜 data-config='...' 本身（内容里不会出现单引号，
       因为 JSON 的双引号被写成 &quot;）。
    """
    html = _http_get(article_url)
    if not html:
        return "", []
    t = re.search(r"<title[^>]*>([\s\S]{0,200}?)</title>", html, re.I)
    page_title = re.sub(r"<[^>]+>", "", t.group(1)).strip() if t else ""

    videos = []
    for m in re.finditer(r"data-config='([^']+)'", html):
        raw = m.group(1)
        raw = (raw.replace("&quot;", _Q).replace("&#39;", _AQ)
                  .replace("&amp;", "&").replace("\\/", "/"))
        try:
            cfg = json.loads(raw)
        except Exception:
            continue
        url = ((cfg.get("video") or {}).get("url") or "").strip()
        if not url:
            continue

        # 同一段属性串里的其他信息：从 data-config 往前截一小段找
        ctx = html[max(0, m.start() - 900):m.start()]
        def grab(name):
            r = re.search(name + r'="([^"]*)"', ctx)
            return r.group(1).strip() if r else ""
        videos.append({
            "url": url,
            "videoTitle": grab("data-video_title")[:160],
            "tags": grab("data-video_tag_name"),
            "typeName": grab("data-video_type_name"),
        })
    return page_title, videos


def album_download(m3u8_url, referer, outdir):
    """用 ffmpeg 直接下 HLS（含 AES-128 解密）。返回 (路径, 字节数)。"""
    os.makedirs(outdir, exist_ok=True)
    dest = os.path.join(outdir, "alb_%d.mp4" % int(time.time()))
    cmd = [FFMPEG, "-y", "-headers",
           "Referer: %s\r\nUser-Agent: Mozilla/5.0\r\n" % referer,
           "-i", m3u8_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", dest]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception:
        return None, 0
    if os.path.exists(dest) and os.path.getsize(dest) > 10000:
        return dest, os.path.getsize(dest)
    try:
        if os.path.exists(dest):
            os.unlink(dest)
    except Exception:
        pass
    return None, 0


# ── 视频复用缓存 ────────────────────────────────────────────────────────────
#
# 为什么需要：一个视频下载+压缩要 10-20 分钟。同一个视频如果 A 群要看、
# B 群也要看，重复下载纯属浪费。缓存后第二次是**秒级**（实测群聊发 136MB
# 已缓存视频只要 6 秒，未缓存要 3 分钟上传）。
#
# 缓存键 = 视频的唯一标识：
#   · 每日大赛 / 游戏巴士：m3u8 的路径部分（去掉 auth_key 等会变的查询串）
#   · cosplaytele：同样用 m3u8 路径
# 注意 auth_key 有时效，但**文件内容**不变，所以缓存键必须去掉 query。
#
# 缓存策略：
#   · 索引文件 data/video_cache.json：{key: {path, bytes, at, title}}
#   · 超过 TTL（默认 14 天）或文件丢失 -> 视为失效
#   · 总量超过上限（默认 8GB）-> 按最久未用淘汰

VIDEO_CACHE_INDEX = os.path.join(HERE, "data", "video_cache.json")
VIDEO_CACHE_TTL = int(os.environ.get("XMF_CACHE_TTL_DAYS", "14")) * 86400
VIDEO_CACHE_MAX_BYTES = int(os.environ.get("XMF_CACHE_MAX_GB", "8")) * 1024 * 1024 * 1024


def video_cache_key(url: str) -> str:
    """从 m3u8/video URL 提取稳定的缓存键（去掉会过期的 query）。"""
    try:
        from urllib.parse import urlparse
        u = urlparse(str(url or ""))
        # path 里通常含视频 id（如 /videos5/<hash>/<hash>.m3u8）
        return (u.netloc + u.path).strip()
    except Exception:
        return str(url or "")[:200]


def _load_video_cache():
    try:
        with io.open(VIDEO_CACHE_INDEX, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_video_cache(d):
    try:
        os.makedirs(os.path.dirname(VIDEO_CACHE_INDEX), exist_ok=True)
        with io.open(VIDEO_CACHE_INDEX, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def cache_lookup(url: str):
    """查缓存。命中返回 (path, bytes, title)，否则 None。"""
    key = video_cache_key(url)
    d = _load_video_cache()
    e = d.get(key)
    if not e:
        return None
    path = e.get("path") or ""
    if not path or not os.path.exists(path):
        d.pop(key, None)
        _save_video_cache(d)
        return None
    if time.time() - float(e.get("at") or 0) > VIDEO_CACHE_TTL:
        try:
            os.unlink(path)
        except Exception:
            pass
        d.pop(key, None)
        _save_video_cache(d)
        return None
    # 更新访问时间（LRU 用）
    e["at"] = time.time()
    d[key] = e
    _save_video_cache(d)
    return path, int(e.get("bytes") or os.path.getsize(path)), e.get("title") or ""


def cache_store(url: str, path: str, title: str = ""):
    """写入缓存，并按总大小淘汰。"""
    key = video_cache_key(url)
    try:
        nbytes = os.path.getsize(path)
    except Exception:
        return
    d = _load_video_cache()
    d[key] = {"path": path, "bytes": nbytes, "at": time.time(), "title": str(title)[:160]}
    # 超总量：按 at 最旧淘汰
    items = sorted(d.items(), key=lambda kv: float(kv[1].get("at") or 0))
    total = sum(int(v.get("bytes") or 0) for _, v in items)
    while items and total > VIDEO_CACHE_MAX_BYTES:
        k, v = items.pop(0)
        try:
            if os.path.exists(v.get("path") or ""):
                os.unlink(v["path"])
        except Exception:
            pass
        total -= int(v.get("bytes") or 0)
        d.pop(k, None)
    _save_video_cache(d)


# ── 视频压缩 ────────────────────────────────────────────────────────────────
#
# 实测 QQ 上传上限（SnowLuma 走本地文件上传）：
#   30MB  -> ok  43s
#   175MB -> ok  183s（约 1MB/s）
#   989MB -> fetch failed（HTTP 层就被拒）
# 所以必须把视频压到可上传范围。默认目标 150MB（实测 175MB 能过，留点余量）。

MAX_SEND_BYTES = int(os.environ.get("XMF_MAX_SEND_MB", "150")) * 1024 * 1024


def _probe_duration(path):
    """用 ffprobe 读时长（秒）。失败返回 0。"""
    try:
        p = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=60)
        return float((p.stdout or "0").strip() or 0)
    except Exception:
        return 0.0




# ── 推文媒体 ────────────────────────────────────────────────────────────
# 图片魔数：用来判断下下来的到底是图还是一段错误页
_IMG_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"\x00\x00\x00")


def _looks_like_image(path):
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return any(head.startswith(m) for m in _IMG_MAGIC)
    except Exception:
        return False


def _download_file(url, dst, referer=None, timeout=120, min_bytes=2000, retries=2):
    """下载文件（走代理）。返回 True/False。

    2026-10-25 加固（封面经常"抓不到"的根因是网络抖动，不是逻辑错）：
      · 实测同一个 URL 经代理会偶发 rc=28（超时）/ rc=35（SSL），重试即可
      · 先下到 .part 再改名 —— 以前直接 -o 目标文件，失败会留 0 字节的
        残file，下次 "文件已存在" 判断就被它骗过去了
      · 用图片魔数校验，避免把 404 错误页当成封面存下来
      · 阈值 5000 → 2000（小封面也能过），但必须像图
      · curl → curl（Linux 上也有 curl，两个平台通吃）
    """
    tmp = dst + ".part"
    for attempt in range(max(1, retries + 1)):
        for f in (tmp,):
            try:
                if os.path.exists(f):
                    os.unlink(f)
            except Exception:
                pass
        cmd = ["curl", "-s", "--proxy", PROXY, "-A", UA,
               "--max-time", str(timeout), "--retry", "1", "-L", "-o", tmp]
        if referer:
            cmd += ["-e", referer]
        cmd.append(url)
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        except Exception:
            continue
        try:
            if not os.path.exists(tmp):
                continue
            sz = os.path.getsize(tmp)
            if sz < min_bytes or not _looks_like_image(tmp):
                os.unlink(tmp)
                continue
            os.replace(tmp, dst)
            return True
        except Exception:
            continue
    try:
        if os.path.exists(tmp):
            os.unlink(tmp)
    except Exception:
        pass
    return False


def fetch_tweet_media(tweet_id):
    """用 twscrape 取推文里的视频直链 / 图片链接。

    返回 {ok, videoUrls:[{url,quality,bitrate}], imageUrls:[], text, duration}
    为什么走 twscrape 而不是爬网页：推文的视频地址在 HTML 里是转义的，
    而且需要登录态；twscrape 直接给 variants 结构，稳定得多。
    """
    script = os.path.join(HERE, "_twmedia.py")
    body = r'''
import asyncio, os, sys, json, io
os.environ.setdefault("TWS_PROXY", xmf_paths.proxy_url())
from twscrape import API

DB = os.environ.get("XMF_DB")
tid = sys.argv[1]
out = {"ok": False}

async def main():
    api = API(DB)
    tw = await api.tweet_details(int(tid))
    if not tw:
        out["error"] = "推文不存在或已删除"
        return
    out["ok"] = True
    out["text"] = (tw.rawContent or "")[:200].replace("\n", " ")
    out["date"] = str(tw.date)[:16]
    m = tw.media
    vids, pics = [], []
    if m:
        for v in (getattr(m, "videos", None) or []) + (getattr(m, "animated", None) or []):
            out["duration"] = getattr(v, "duration", 0)
            for va in (getattr(v, "variants", None) or []):
                u = getattr(va, "url", None)
                if u and getattr(va, "contentType", "") == "video/mp4":
                    vids.append({"url": u, "bitrate": getattr(va, "bitrate", 0) or 0,
                                 "quality": _q(u)})
            if getattr(v, "thumbnailUrl", None):
                pics.append(v.thumbnailUrl)
        for ph in (getattr(m, "photos", None) or []):
            u = getattr(ph, "url", None)
            if u:
                pics.append(u)
    vids.sort(key=lambda x: x["bitrate"])
    out["videoUrls"] = vids
    out["imageUrls"] = pics
    out["isVideo"] = bool(vids)

def _q(u):
    import re
    mm = re.search(r"/(\d+x\d+)/", u)
    return mm.group(1) if mm else "?"

try:
    asyncio.run(main())
except Exception as e:
    out = {"ok": False, "error": str(e)[:200]}

io.open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
'''
    try:
        with io.open(script, "w", encoding="utf-8") as f:
            f.write(body)
        res_path = os.path.join(HERE, "data", "_twmedia_%s.json" % tweet_id)
        env = dict(os.environ)
        env["XMF_DB"] = env.get("XMF_DB") or os.path.join(
            HERE, "..", "..", "..", "data", "x-media-finder", "accounts.db")
        env["XMF_PROXY"] = PROXY
        env["PYTHONIOENCODING"] = "utf-8"
        subprocess.run([sys.executable, script, tweet_id, res_path],
                       capture_output=True, timeout=120, env=env)
        if os.path.exists(res_path):
            data = json.loads(io.open(res_path, encoding="utf-8").read())
            try:
                os.unlink(res_path)
            except Exception:
                pass
            return data
    except Exception as exc:
        return {"ok": False, "error": "取推文媒体失败：%s" % exc}
    return {"ok": False, "error": "取推文媒体超时或失败"}


def compress_if_needed(path):
    """超过 MAX_SEND_BYTES 就重编码到该上限以内。返回 (最终路径, 是否压缩过)。

    为什么重编码而不是截断：截断会让视频看起来"坏掉了"，
    重编码至少能保住完整内容（画质降一点而已）。
    策略：按目标码率反算，注意留 10% 余量给音频和封装开销。
    """
    try:
        size = os.path.getsize(path)
    except Exception:
        return path, False
    if size <= MAX_SEND_BYTES:
        return path, False

    dur = _probe_duration(path)
    if dur <= 0:
        return path, False

    # 目标视频码率：留 12% 给音频/容器
    target_kbps = int((MAX_SEND_BYTES * 0.88 * 8) / dur / 1000)
    target_kbps = max(300, min(target_kbps, 6000))   # 别低到看不清，也别超过源
    out = re.sub(r"\.mp4$", "", path) + "_c.mp4"
    cmd = [FFMPEG, "-y", "-i", path,
           "-c:v", "libx264", "-preset", "veryfast", "-b:v", "%dk" % target_kbps,
           "-maxrate", "%dk" % target_kbps, "-bufsize", "%dk" % (target_kbps * 2),
           "-vf", "scale='min(1280,iw)':-2",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception:
        return path, False
    if os.path.exists(out) and os.path.getsize(out) > 10000:
        try:
            os.unlink(path)      # 删掉原始大文件，省磁盘
        except Exception:
            pass
        return out, True
    return path, False


def grab_site(task):
    """主入口：search / video / album_search / album_video / gamebus_*。"""
    mode = str(task.get("mode") or "search")

    # 配置覆盖（来自技能设置，不写死）
    global MAX_SEND_BYTES, VIDEO_CACHE_TTL, VIDEO_CACHE_MAX_BYTES
    if task.get("maxSendMB"):
        try:
            MAX_SEND_BYTES = int(float(task["maxSendMB"])) * 1024 * 1024
        except Exception:
            pass
    if task.get("cacheDays"):
        try:
            VIDEO_CACHE_TTL = int(float(task["cacheDays"])) * 86400
        except Exception:
            pass
    if task.get("cacheMaxGB"):
        try:
            VIDEO_CACHE_MAX_BYTES = int(float(task["cacheMaxGB"])) * 1024 * 1024 * 1024
        except Exception:
            pass

    # ── 游戏巴士：搜索 ──────────────────────────────────────────────────
    if mode == "gamebus_search":
        kw = str(task.get("keyword") or "").strip()
        if not kw:
            return fail("BAD_ARGS", "缺少关键词")
        emit({"t": "progress", "stage": "search", "done": 0, "total": 1,
              "note": "在游戏巴士搜索：%s" % kw})
        items = gamebus_search(kw, int(task.get("limit") or 12))
        if not items:
            return fail("NO_RESULT", "游戏巴士没搜到")
        return emit({"t": "result", "ok": True, "items": items, "count": len(items)})

    # ── 游戏巴士：下载 ──────────────────────────────────────────────────
    if mode == "av_search":
        import av_sites
        kw = str(task.get("keyword") or "").strip()
        if not kw:
            return fail("BAD_ARGS", "缺少关键词")
        sites = task.get("sites") or ["fc2", "missav", "pornhub", "magnet"]
        limit = int(task.get("limit") or 6)
        site_hint, code = av_sites.normalize_code(kw)
        query = code or kw          # 有番号就用番号搜，命中率高得多

        items = []
        total = len(sites)
        for i, sn in enumerate(sites):
            emit({"t": "progress", "stage": "search", "done": i, "total": total,
                  "note": "搜索 %s（%s）…" % (sn, query)})
            got = []
            try:
                if sn == "fc2":
                    got = av_sites.search_fc2(query, limit)
                elif sn == "missav":
                    got = av_sites.search_missav(query, limit)
                elif sn == "pornhub":
                    got = av_sites.search_pornhub(query, limit)
                elif sn == "magnet":
                    got = av_sites.search_magnet(query, limit)
            except Exception as exc:
                log_warn("搜索 %s 失败：%s" % (sn, exc))
            items.extend(got)

        if not items:
            return fail("NO_RESULT", "四个站都没搜到")
        # 下封面到本地（QQ 只能发本地文件；远程 URL 协议端拉不到）
        if task.get("withCover", True):
            emit({"t": "progress", "stage": "cover", "done": 0, "total": 1,
                  "note": "取封面图…"})
            try:
                n = av_sites.download_covers(items, limit=int(task.get("coverLimit") or 6))
                emit({"t": "progress", "stage": "cover", "done": 1, "total": 1,
                      "note": "封面 %d/%d 张" % (n, len(items))})
            except Exception as exc:
                log_warn("取封面失败：%s" % exc)
        return emit({"t": "result", "ok": True, "items": items,
                     "count": len(items), "code": code, "query": query})
    # ── 只解析播放地址，不下载（给云端 API 用）──────────────────────────
    #
    # 为什么单独一个 mode：API 架构是「服务器只搜索 + 分发链接，客户端自己下」。
    # 如果复用 av_video 会把几 GB 视频下到服务器上，完全违背设计。
    if mode == "av_resolve":
        import av_sites
        page_url = str(task.get("url") or "").strip()
        if not page_url.startswith("http"):
            return fail("BAD_ARGS", "缺少页面链接")
        emit({"t": "progress", "stage": "resolve", "done": 0, "total": 2,
              "note": "打开视频页取播放地址…"})
        r = av_sites.get_video_url(page_url, timeout=int(task.get("timeout") or 200))
        if not r.get("ok"):
            return fail("NO_VIDEO", r.get("error") or "取不到播放地址")
        m3u8s = r.get("m3u8") or []
        mp4s = r.get("mp4") or []
        # 只留真正的视频地址（mp4 列表里会混进缩略图/gif，按文件名特征筛）
        real_mp4 = []
        for u in mp4s:
            lu = u.lower()
            if any(k in lu for k in ("thumb", "/gif", "poster", ".jpg", ".webp")):
                continue
            real_mp4.append(u)
        from urllib.parse import urlsplit
        sp = urlsplit(page_url)
        referer = "%s://%s/" % (sp.scheme, sp.netloc)
        emit({"t": "progress", "stage": "resolve", "done": 2, "total": 2,
              "note": "取到 %d 个 hls / %d 个 mp4" % (len(m3u8s), len(real_mp4))})
        return emit({"t": "result", "ok": True,
                     "title": r.get("title") or "",
                     "kind": "hls" if m3u8s else ("mp4" if real_mp4 else "none"),
                     "m3u8": m3u8s[:3], "mp4": real_mp4[:5],
                     "referer": referer, "pageUrl": page_url})

    if mode == "av_video":
        import av_sites
        page_url = str(task.get("url") or "").strip()
        if not page_url.startswith("http"):
            return fail("BAD_ARGS", "缺少页面链接")
        emit({"t": "progress", "stage": "video", "done": 0, "total": 4,
              "note": "打开视频页取播放地址…"})
        r = av_sites.get_video_url(page_url, timeout=200)
        if not r.get("ok"):
            return fail("NO_VIDEO", r.get("error") or "取不到播放地址")
        title = r.get("title") or ""
        urls = r.get("m3u8") or []
        mp4s = r.get("mp4") or []
        outdir = os.path.join(HERE, "data", "sitevideos")
        os.makedirs(outdir, exist_ok=True)
        key = "av:" + page_url.rstrip("/").split("/")[-1]
        hit = cache_lookup(key)
        if hit:
            hp, hb, ht = hit
            emit({"t": "progress", "stage": "video", "done": 4, "total": 4,
                  "note": "命中缓存（%.0fMB）" % (hb / 1048576.0)})
            return emit({"t": "result", "ok": True, "localPath": hp, "bytes": hb,
                         "compressed": False, "cached": True, "title": ht or title})
        dst = os.path.join(outdir, "av_%s.mp4" % re.sub(r"[^A-Za-z0-9]", "", key)[-24:])
        got = None
        # m3u8 有两种情况：
        #  · Pornhub 的 hv-h.phncdn.com → ffmpeg 直接能拉
        #  · MissAV 的 surrit.com → 分片后缀是 .jpeg（伪装），ffmpeg 报
        #    "Invalid data found"，必须 curl 逐片下再 concat
        # 所以先试 ffmpeg，失败再走 hls_curl。
        for u in urls[:3]:
            emit({"t": "progress", "stage": "video", "done": 1, "total": 4,
                  "note": "下载 m3u8 流…"})
            p2, sz = album_download(u, page_url, outdir)
            if p2:
                got = p2
                break
            # 兜底：curl 逐片下载 + concat（对 surrit 这类伪装后缀有效）
            emit({"t": "progress", "stage": "video", "done": 1, "total": 4,
                  "note": "改用分片下载（这条流 ffmpeg 不认）…"})
            try:
                import hls_curl
                p3, sz3 = hls_curl.download_hls(
                    u, page_url, outdir,
                    on_progress=lambda ev: emit(ev))
                if p3:
                    got = p3
                    break
            except Exception as exc:
                log_warn("分片下载失败：%s" % exc)
        if not got:
            # mp4 直链按清晰度从高到低排（URL 里带 1080P/720P/240P）
            def _q(u):
                m = re.search(r"(\d{3,4})P", u, re.I)
                return int(m.group(1)) if m else 0
            ordered = sorted(mp4s, key=_q, reverse=True)
            for u in ordered[:4]:
                emit({"t": "progress", "stage": "video", "done": 1, "total": 4,
                      "note": "下载 mp4 直链（%dP）…" % _q(u)})
                cmd = [FFMPEG, "-y", "-referer", page_url,
                       "-user_agent", UA, "-i", u, "-c", "copy",
                       "-bsf:a", "aac_adtstoasc", dst]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=1800)
                except Exception:
                    continue
                if os.path.exists(dst) and os.path.getsize(dst) > 100000:
                    got = dst
                    break
        if not got:
            return fail("DOWNLOAD_FAILED", "播放地址拿到了但下载失败")
        sz = os.path.getsize(got)
        cache_store(key, got, title)
        emit({"t": "progress", "stage": "video", "done": 3, "total": 4,
              "note": "下好 %.0fMB，检查体积…" % (sz / 1048576.0)})
        finalp, comp = compress_if_needed(got)
        if comp:
            cache_store(key, finalp, title)
        return emit({"t": "result", "ok": True, "localPath": finalp,
                     "bytes": os.path.getsize(finalp), "compressed": comp,
                     "title": title})
    if mode == "magnet_info":
        import av_sites
        mag = str(task.get("magnet") or "").strip()
        if not mag.startswith("magnet:"):
            return fail("BAD_ARGS", "这不是磁力链接")
        emit({"t": "progress", "stage": "magnet", "done": 0, "total": 2,
              "note": "正在取种子元数据（连做种节点）…"})
        info = av_sites.magnet_info(mag, timeout_sec=int(task.get("timeoutSec") or 90))
        if not info.get("ok"):
            return fail("MAGNET_FAILED", info.get("error") or "解析失败")
        emit({"t": "progress", "stage": "magnet", "done": 2, "total": 2,
              "note": "拿到文件列表（%d 个文件）" % len(info.get("files") or [])})
        return emit({"t": "result", "ok": True, **info})
    if mode == "magnet_download":
        import av_sites
        mag = str(task.get("magnet") or "").strip()
        if not mag.startswith("magnet:"):
            return fail("BAD_ARGS", "缺少磁力链接")
        outdir = os.path.join(HERE, "data", "sitevideos", "magnet")
        emit({"t": "progress", "stage": "magnet", "done": 0, "total": 3,
              "note": "连接做种节点…"})
        maxmin = int(task.get("magnetMaxMinutes") or 25)
        path, sz = av_sites.download_magnet(mag, outdir,
                                            on_progress=lambda ev: emit(ev),
                                            max_minutes=maxmin,
                                            select_file=task.get("selectFile"))
        if not path:
            return fail("MAGNET_FAILED",
                        "磁力没下下来（可能没做种，或这个网络连不上 BT 节点）")
        emit({"t": "progress", "stage": "magnet", "done": 2, "total": 3,
              "note": "下好 %.0fMB，检查体积…" % (sz / 1048576.0)})
        finalp, comp = compress_if_needed(path)
        return emit({"t": "result", "ok": True, "localPath": finalp,
                     "bytes": os.path.getsize(finalp), "compressed": comp,
                     "title": "磁力下载"})
    if mode == "twitter_media":
        tid = str(task.get("tweetId") or "").strip()
        if not tid.isdigit():
            return fail("BAD_ARGS", "缺少 tweetId")
        emit({"t": "progress", "stage": "video", "done": 0, "total": 3,
              "note": "取推文媒体地址…"})
        info = fetch_tweet_media(tid)
        if not info.get("ok"):
            return fail("NO_MEDIA", info.get("error") or "取不到推文媒体")
        urls = info.get("videoUrls") or []
        if not urls:
            # 纯图片推文：把图片下下来当"视频"发不了，返回图片路径让上层处理
            pics = info.get("imageUrls") or []
            if pics:
                outdir = os.path.join(HERE, "data", "sitevideos")
                os.makedirs(outdir, exist_ok=True)
                got = []
                for i, iu in enumerate(pics[:4]):
                    dst = os.path.join(outdir, "twimg_%s_%d.jpg" % (tid, i))
                    if _download_file(iu, dst, referer="https://x.com/"):
                        got.append({"localPath": dst, "bytes": os.path.getsize(dst),
                                    "title": info.get("text") or tid, "isImage": True})
                if not got:
                    return fail("DOWNLOAD_FAILED", "图片下载失败")
                return emit({"t": "result", "ok": True, "images": got,
                             "count": len(got), "isImage": True})
            return fail("NO_MEDIA", "这条推文没有视频也没有图片")

        # 选清晰度：取码率最高的（通常 1080p）
        best = urls[-1]
        emit({"t": "progress", "stage": "video", "done": 1, "total": 3,
              "note": "开始下载（%s）…" % best.get("quality", "?")})
        outdir = os.path.join(HERE, "data", "sitevideos")
        os.makedirs(outdir, exist_ok=True)
        dst = os.path.join(outdir, "tw_%s.mp4" % tid)

        hit = cache_lookup("tw:" + tid)
        if hit:
            hp, hb, ht = hit
            emit({"t": "progress", "stage": "video", "done": 3, "total": 3,
                  "note": "命中缓存（%.0fMB）" % (hb / 1048576.0)})
            return emit({"t": "result", "ok": True, "localPath": hp, "bytes": hb,
                         "compressed": False, "cached": True,
                         "title": ht or info.get("text") or tid,
                         "duration": info.get("duration") or 0})

        if not _download_file(best["url"], dst, referer="https://x.com/"):
            return fail("DOWNLOAD_FAILED", "视频下载失败")
        sz = os.path.getsize(dst)
        cache_store("tw:" + tid, dst, info.get("text") or tid)
        emit({"t": "progress", "stage": "video", "done": 2, "total": 3,
              "note": "下好 %.0fMB，检查体积…" % (sz / 1048576.0)})
        finalp, comp = compress_if_needed(dst)
        if comp:
            cache_store("tw:" + tid, finalp, info.get("text") or tid)
        return emit({"t": "result", "ok": True, "localPath": finalp,
                     "bytes": os.path.getsize(finalp), "compressed": comp,
                     "title": info.get("text") or tid,
                     "duration": info.get("duration") or 0})
    if mode == "image_source":
        # 以图搜源交给独立脚本（依赖 Playwright + twscrape，逻辑较多）
        img = str(task.get("image") or "").strip()
        if not img or not os.path.exists(img):
            return fail("BAD_ARGS", "图片不存在：%s" % img)
        script = os.path.join(HERE, "image_source_worker.py")
        if not os.path.exists(script):
            return fail("NO_SCRIPT", "找不到 image_source_worker.py")
        # 直接把结果行透传（子脚本自己 emit progress/result）
        try:
            proc = subprocess.Popen(
                [sys.executable, script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                cwd=HERE, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            out, err = proc.communicate(json.dumps(task), timeout=int(task.get("timeoutSec") or 300))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            return fail("TIMEOUT", "识图超时")
        except Exception as exc:
            return fail("SPAWN_FAILED", "启动识图脚本失败：%s" % exc)
        got_result = False
        for line in (out or "").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            emit(ev)
            if ev.get("t") == "result":
                got_result = True
        if not got_result:
            return fail("NO_RESULT", (err or out or "识图没有输出")[-300:])
        return 0
    if mode == "gamebus_video":
        vid = str(task.get("videoId") or "").strip()
        if not vid:
            return fail("BAD_ARGS", "缺少 videoId")
        emit({"t": "progress", "stage": "video", "done": 0, "total": 3,
              "note": "取播放地址…"})
        hls, title, dur = gamebus_hls(vid)
        if not hls:
            return fail("NO_VIDEO", "没取到播放地址（可能是会员视频）")
        emit({"t": "progress", "stage": "video", "done": 1, "total": 4,
              "note": "开始下载，视频时长约 %d 分钟…" % max(1, dur // 60)})
        outdir = os.path.join(HERE, "data", "sitevideos")
        hit = cache_lookup(hls)
        if hit:
            hp, hb, ht = hit
            emit({"t": "progress", "stage": "video", "done": 3, "total": 3,
                  "note": "命中缓存（%.0fMB），跳过下载" % (hb / 1048576.0)})
            return emit({"t": "result", "ok": True, "localPath": hp, "bytes": hb,
                         "compressed": False, "cached": True,
                         "title": ht or title, "duration": dur})
        pth, sz = album_download(hls, GAMEBUS_BASE + "/video/", outdir)
        if not pth:
            return fail("DOWNLOAD_FAILED", "下载失败")
        cache_store(hls, pth, title)
        emit({"t": "progress", "stage": "video", "done": 2, "total": 3,
              "note": "下好 %.0fMB，检查体积…" % (sz / 1048576.0)})
        finalp, comp = compress_if_needed(pth)
        if comp:
            cache_store(hls, finalp, title)
        return emit({"t": "result", "ok": True, "localPath": finalp,
                     "bytes": os.path.getsize(finalp), "compressed": comp,
                     "title": title, "duration": dur})

    # ── 自动路由：按说法决定搜哪个站 ────────────────────────────────────
    if mode == "route":
        kw = str(task.get("keyword") or "").strip()
        if not kw:
            return fail("BAD_ARGS", "缺少关键词")
        plan = detect_route(kw)

        # ── 「换一批 / 多找某站」支持 ────────────────────────────────────
        # focus  : 只搜指定站（cosplaytele / gamebus / album / twitter）
        # exclude: 已展示过的 URL 列表（换一批时不再重复）
        # perSite: 每站最多保留几条（默认 3）
        focus = str(task.get("focus") or "").strip().lower()
        if focus and focus not in ("all", ""):
            plan["sites"] = [focus]
        # perSite=0 表示"该站有多少给多少"（群友说「非常多」/「都要」时用）
        _ps = int(task.get("perSite") if task.get("perSite") is not None else 3)
        per_site = 0 if _ps <= 0 else min(20, _ps)
        # 内部限额用于"抓多少"和"筛后保留多少"，0 表示不限制
        def _cap(n):
            return n if per_site <= 0 else per_site
        exclude = set(str(u) for u in (task.get("exclude") or []) if u)

        tokens = build_match_tokens(plan)
        emit({"t": "progress", "stage": "route", "done": 0, "total": 1,
              "note": "路由：%s → %s（搜索词：%s%s）"
                      % (kw, "+".join(plan["sites"]), plan["keyword"],
                         "，已看过 %d 条" % len(exclude) if exclude else "")})
        if tokens:
            emit({"t": "progress", "stage": "route", "done": 0, "total": 1,
                  "note": "相关性过滤：%s" % " / ".join(tokens)})
        results = []
        # 多关键词：plan["keywords"] 是拆好的中文词列表
        # （「千夏和阿米娅」→ ['千夏','阿米娅']），各站逐个别搜再合并。
        cn_words = plan.get("keywords") or [plan.get("cnSearch") or plan["keyword"]]
        cn_words = [w for w in cn_words if w]
        if not cn_words:
            cn_words = [plan["keyword"]]

        # 按 plan.sites 顺序执行（cosplaytele 在 twitter 前 = 优先）
        for site in plan["sites"]:
            if site == "album":
                for w in cn_words:
                    try:
                        grab_n = 40 if per_site <= 0 else max(per_site * 3, int(task.get("limit") or 10))
                        items = album_search(w, grab_n, int(task.get("pages") or 2))
                        for it in items:
                            it["site"] = "每日大赛"
                        results.extend(items)
                        emit({"t": "progress", "stage": "search", "done": 0, "total": 1,
                              "note": "每日大赛「%s」→ %d 条" % (w, len(items))})
                    except Exception as e:
                        sys.stderr.write("[album] %s 失败：%s\n" % (w, str(e)[:120]))
            elif site == "cosplaytele":
                # 英文站：中文词先翻成英文；每个词都试，谁有结果就继续下一个词
                en_words = []
                for w in cn_words:
                    en_words.extend(cn_to_en(w) or [])
                if not en_words:
                    en_words = plan["converted"] or [plan["keyword"]]
                for en in en_words:
                    try:
                        grab_ct = 20 if per_site <= 0 else max(per_site * 4, 8)
                        r = _run_node(SEARCH_JS, [en, grab_ct], timeout=120)
                        got = r.get("items") or []
                        for it in got:
                            it["site"] = "cosplaytele"
                            results.append(it)
                        emit({"t": "progress", "stage": "search", "done": 0, "total": 1,
                              "note": "cosplaytele「%s」→ %d 条" % (en, len(got))})
                    except Exception as e:
                        sys.stderr.write("[cosplaytele] %s 失败：%s\n" % (en, str(e)[:120]))
            elif site == "gamebus":
                # 游戏巴士认中文，且是**连续短语**匹配
                for w in cn_words:
                    gkw = re.sub(r"\s+", "", _NOISE_RE.sub("", w)) or w
                    try:
                        grab_gb = 20 if per_site <= 0 else max(per_site * 4, 8)
                        gb = gamebus_search(gkw, grab_gb)
                        results.extend(gb)
                        emit({"t": "progress", "stage": "search", "done": 0, "total": 1,
                              "note": "游戏巴士「%s」→ %d 条" % (gkw, len(gb))})
                    except Exception as e:
                        sys.stderr.write("[gamebus] %s 失败：%s\n" % (gkw, str(e)[:120]))
            elif site == "twitter":
                # 推特由 index.js 那边另调 x_worker，这里只标记。
                # 如果调用方事先知道推特限流了（task.twitterDegraded），
                # 就透传到 result，让上层在卡片说明里标注"推特限频，已跳过"。
                pass
        if not results:
            return fail("NO_RESULT", "都没搜到")

        # ── 相关性过滤 + 每站限额 ────────────────────────────────────────
        # 必须在补封面**之前**做：封面要进详情页，无关条目白花几十秒。
        raw_count = len(results)
        results = filter_relevant(results, tokens, per_site_limit=per_site,
                                  exclude=exclude)
        emit({"t": "progress", "stage": "filter", "done": 1, "total": 1,
              "note": "筛掉 %d 条无关/重复，保留 %d 条"
                      % (raw_count - len(results), len(results))})
        if not results:
            return fail("NO_RELEVANT",
                        "搜到 %d 条但都不匹配「%s」%s"
                        % (raw_count, " / ".join(tokens) or kw,
                           "（可能是换一批换到底了，换个关键词或平台）" if exclude else ""))
        # 三站统一补封面（用户要求多站也带预览图）：
        #   每日大赛 -> 详情页 .post-content
        #   cosplaytele -> 详情页 .entry-content
        #   游戏巴士 -> 搜索结果的 coverUrl
        if task.get("withCover", True):
            emit({"t": "progress", "stage": "cover", "done": 0,
                  "total": len(results),
                  "note": "取 %d 条预览图…" % len(results)})
            got = fetch_all_covers(results)
            emit({"t": "progress", "stage": "cover", "done": len(results),
                  "total": len(results),
                  "note": "预览图 %d/%d 张" % (got, len(results))})
            if got == 0:
                emit({"t": "progress", "stage": "cover", "done": len(results),
                      "total": len(results),
                      "note": "⚠️ 一张预览图都没抓到（站点超时或防盗链），卡片里会只有文字"})
        return emit({"t": "result", "ok": True, "items": results,
                     "coverCount": sum(1 for it in results if it.get("coverLocal")),
                     "twitterDegraded": bool(task.get("twitterDegraded")),
                     "count": len(results), "plan": plan})

    # ── 每日大赛：搜索 ──────────────────────────────────────────────────
    if mode == "album_search":
        kw = str(task.get("keyword") or "").strip()
        if not kw:
            return fail("BAD_ARGS", "缺少关键词")
        emit({"t": "progress", "stage": "search", "done": 0, "total": 1,
              "note": "在每日大赛搜索：%s" % kw})
        # 注意：站点搜索**不做地区过滤**（用户要求）。
        # region 只作用于推特搜索 —— 那两个站本身就是中文/日文内容，
        # 再按语言筛反而会误杀（比如标题是中文但正文标签是英文）。
        items = album_search(kw, int(task.get("limit") or 50),
                             int(task.get("pages") or 3))
        if not items:
            return fail("NO_RESULT", "每日大赛没搜到")
        # 取详情页正文第一张图当预览（按 id 缓存，重复搜索不重取）
        if task.get("withCover", True):
            n = len(items)
            emit({"t": "progress", "stage": "cover", "done": 0, "total": n,
                  "note": "取 %d 条预览图…" % n})
            got = fetch_detail_covers(items)
            emit({"t": "progress", "stage": "cover", "done": n, "total": n,
                  "note": "预览图 %d/%d 张" % (got, n)})
        return emit({"t": "result", "ok": True, "items": items, "count": len(items)})

    # ── 每日大赛：下载视频 ──────────────────────────────────────────────
    if mode == "album_video":
        url = str(task.get("url") or "").strip()
        if not url.startswith("http"):
            return fail("BAD_ARGS", "缺少文章链接")
        emit({"t": "progress", "stage": "video", "done": 0, "total": 4,
              "note": "正在解析文章页…"})
        page_title, vids = album_parse_videos(url)
        if not vids:
            return fail("NO_VIDEO", "这篇没找到视频")
        emit({"t": "progress", "stage": "video", "done": 1, "total": 4,
              "note": "找到 %d 个视频，开始下载第 1 个…" % len(vids)})
        outdir = os.path.join(HERE, "data", "sitevideos")
        got = []
        for i, v in enumerate(vids):
            if i >= int(task.get("maxVideos") or 3):
                break
            emit({"t": "progress", "stage": "video", "done": 2, "total": 4,
                  "note": "下载第 %d/%d 个（共 %d 个）…" % (i + 1, len(vids), len(vids))})
            # 先查缓存：同一视频给不同群/不同人时直接复用，不再下载
            hit = cache_lookup(v["url"])
            if hit:
                hp, hb, ht = hit
                emit({"t": "progress", "stage": "video", "done": 2, "total": 3,
                      "note": "命中缓存（%.0fMB），跳过下载" % (hb / 1048576.0)})
                got.append({"localPath": hp, "bytes": hb, "compressed": False,
                            "title": ht or v.get("videoTitle") or page_title,
                            "tags": v.get("tags") or "", "pageTitle": page_title,
                            "cached": True})
                continue
            p, sz = album_download(v["url"], url, outdir)
            if not p:
                continue
            cache_store(v["url"], p, v.get("videoTitle") or page_title)
            emit({"t": "progress", "stage": "video", "done": 2, "total": 3,
                  "note": "第 %d 个下好（%.0fMB），检查体积…" % (i + 1, sz / 1048576.0)})
            finalp, comp = compress_if_needed(p)
            if comp:
                # 压缩后路径变了：缓存指向最终文件，原始大文件已被删
                cache_store(v["url"], finalp, v.get("videoTitle") or page_title)
            got.append({
                "cached": False,
                "localPath": finalp,
                "bytes": os.path.getsize(finalp),
                "compressed": comp,
                "title": v.get("videoTitle") or page_title,
                "tags": v.get("tags") or "",
                "pageTitle": page_title,
            })
        if not got:
            return fail("DOWNLOAD_FAILED", "下载失败")
        return emit({"t": "result", "ok": True, "videos": got, "count": len(got)})
    if mode == "search":
        kw = str(task.get("keyword") or "").strip()
        if not kw:
            return fail("BAD_ARGS", "缺少关键词")
        emit({"t": "progress", "stage": "search", "done": 0, "total": 1,
              "note": "在 cosplaytele 搜索：%s" % kw})
        try:
            r = _run_node(SEARCH_JS, [kw, task.get("limit") or 12], timeout=120)
        except Exception as exc:
            return fail("SEARCH_FAILED", "搜索失败：%s" % exc)
        if r.get("error"):
            return fail("SEARCH_FAILED", r["error"])
        items = r.get("items") or []
        if not items:
            return fail("NO_RESULT", "没搜到")
        return emit({"t": "result", "ok": True, "items": items, "count": len(items)})

    if mode == "video":
        url = str(task.get("url") or "").strip()
        if not url.startswith("http"):
            return fail("BAD_ARGS", "缺少文章链接")
        emit({"t": "progress", "stage": "video", "done": 0, "total": 3,
              "note": "解析播放器…"})
        emit({"t": "progress", "stage": "video", "done": 0, "total": 4,
              "note": "正在打开播放器取地址…"})
        try:
            r = _run_node(VIDEO_JS, [url], timeout=150)
        except Exception as exc:
            return fail("VIDEO_FAILED", "解析失败：%s" % exc)
        if r.get("error"):
            return fail("VIDEO_FAILED", r["error"])
        m3u8 = r.get("m3u8")
        if not m3u8:
            return fail("NO_VIDEO", "这篇没有视频")

        # 先查缓存
        hit = cache_lookup(m3u8)
        if hit:
            hp, hb, ht = hit
            emit({"t": "progress", "stage": "video", "done": 3, "total": 3,
                  "note": "命中缓存（%.0fMB），跳过下载" % (hb / 1048576.0)})
            return emit({"t": "result", "ok": True, "localPath": hp, "bytes": hb,
                         "compressed": False, "cached": True,
                         "title": r.get("title") or "",
                         "duration": r.get("duration") or 0})

        emit({"t": "progress", "stage": "video", "done": 1, "total": 3,
              "note": "拿到播放列表，开始下载分片…"})
        try:
            pl = _curl(m3u8, url)
            segs = [l.strip() for l in pl.split("\n") if l.strip() and not l.startswith("#")]
            if not segs:
                return fail("NO_VIDEO", "播放列表为空")
            outdir = os.path.join(HERE, "data", "sitevideos")
            os.makedirs(outdir, exist_ok=True)
            segdir = os.path.join(outdir, "_seg_%d" % int(time.time()))
            os.makedirs(segdir, exist_ok=True)
            total = 0
            for i, s in enumerate(segs):
                buf = _curl(s, url, binary=True, timeout=60)
                if not buf:
                    continue
                with open(os.path.join(segdir, "seg%04d.ts" % i), "wb") as f:
                    f.write(buf)
                total += len(buf)
                if i % 10 == 0:
                    emit({"t": "progress", "stage": "video", "done": 1, "total": 3,
                          "note": "已下载 %d/%d 分片（%.0fMB）" % (i + 1, len(segs), total / 1048576.0)})
            emit({"t": "progress", "stage": "video", "done": 2, "total": 3,
                  "note": "合并中（%.0fMB）…" % (total / 1048576.0)})
            listf = os.path.join(segdir, "list.txt")
            with open(listf, "w", encoding="utf-8") as f:
                for fn in sorted(os.listdir(segdir)):
                    if fn.endswith(".ts"):
                        f.write("file '%s'\n" % os.path.join(segdir, fn).replace("\\", "/"))
            name = "site_%d.mp4" % int(time.time())
            dest = os.path.join(outdir, name)
            p = subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
                                "-i", listf, "-c", "copy", dest],
                               capture_output=True, timeout=300)
            if not os.path.exists(dest) or os.path.getsize(dest) < 10000:
                return fail("MERGE_FAILED", "合并失败：%s" % (p.stderr or b"")[-200:].decode("utf-8", "replace"))
            # 清理分片
            for fn in os.listdir(segdir):
                try:
                    os.unlink(os.path.join(segdir, fn))
                except Exception:
                    pass
            try:
                os.rmdir(segdir)
            except Exception:
                pass
            cache_store(m3u8, dest, r.get("title") or "")

            # 压到可上传体积内（989MB 实测会被 QQ 拒绝）
            emit({"t": "progress", "stage": "video", "done": 3, "total": 3,
                  "note": "检查体积…"})
            finalp, compressed = compress_if_needed(dest)
            if compressed:
                cache_store(m3u8, finalp, r.get("title") or "")
                emit({"t": "progress", "stage": "video", "done": 3, "total": 3,
                      "note": "已压缩到 %.0fMB" % (os.path.getsize(finalp) / 1048576.0)})
            return emit({"t": "result", "ok": True, "localPath": finalp,
                         "bytes": os.path.getsize(finalp),
                         "compressed": compressed,
                         "title": r.get("title") or "",
                         "duration": r.get("duration") or 0})

        except Exception as exc:
            return fail("DOWNLOAD_FAILED", "下载失败：%s" % exc)

    return fail("BAD_ARGS", "未知 mode：%s" % mode)


def main():
    raw = sys.stdin.read()
    try:
        task = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        return fail("BAD_ARGS", "参数不是合法 JSON：%s" % exc)
    grab_site(task)


if __name__ == "__main__":
    main()
