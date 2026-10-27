#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""av_sites.py — FC2 / MissAV / Pornhub 搜索 + 磁力解析

实测结论（2026-10-23）：
  · FC2      adult.contents.fc2.com/search?keyword=  → 商品页 /article/<id>/
            纯 HTML，curl 可取，无需浏览器。
  · MissAV   missav.com 已废弃（跳 ThisAV），missav.site 返回 404，
            **missav123.com 可用**。搜索页 /cn/search/<kw>，
            视频页 /cn/<code-lowercase>。真实 m3u8 由 JS 生成
            （surrit.com/<uuid>/1080p/video.m3u8），必须拦网络请求。
  · Pornhub  cn.pornhub.com 有年龄门，点「我年满 18 岁」后放行。
            视频页 HTML 里 flashvars 有 mediaDefinitions 数组，
            含各清晰度 hls m3u8（1080/480/240），直接正则取。
  · 磁力     sukebei.nyaa.si/?q=<番号> 返回 HTML 表格，
            magnet:?xt=urn:btih:<hash> 直接可取，命中率很高。

每个函数只做一件事，失败返回空列表/None，由上层决定怎么报。
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# ⚠️ import 必须在任何 xmf_paths.xxx 调用之前
import xmf_paths

PROXY = xmf_paths.proxy_url()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

NODE = xmf_paths.NODE
PW_MODULE = xmf_paths._pw_module_dir(HERE)
ARIA2 = xmf_paths.ARIA2

MISSAV_BASE = "https://missav123.com"
PH_BASE = "https://cn.pornhub.com"
FC2_BASE = "https://adult.contents.fc2.com"
SUKEBEI = "https://sukebei.nyaa.si"


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _download_file(url, dst, referer=None, timeout=120):
    """下载文件到本地（走代理）。返回 True/False。

    注意：这个函数原先只写在 grab_worker.py 里，av_sites.py 没有，
    导致 download_covers 调它时 NameError 被静默吞掉 → 封面一张没下来。
    现在在模块内部自带一份，不依赖外部。
    """
    if not url:
        return False
    cmd = ["curl", "-sL", "--proxy", PROXY, "-A", UA,
           "--max-time", str(timeout), "-o", dst]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        return os.path.exists(dst) and os.path.getsize(dst) > 3000
    except Exception:
        return False


def _curl(url, timeout=30, referer=None, extra=None):
    """GET 文本（走代理）。"""
    cmd = ["curl", "-sL", "--proxy", PROXY, "-A", UA,
           "--max-time", str(timeout), "-H", "Accept-Language: zh-CN,zh;q=0.9"]
    if referer:
        cmd += ["-e", referer]
    if extra:
        cmd += extra
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        return (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""



PW_HTML_JS = r"""
import { chromium } from "playwright";
import fs from "node:fs";

const url = process.argv[2];
const outPath = process.argv[3];
const needAge = process.argv[4] === "1";
const waitMs = Number(process.argv[5] || 5000);

const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1440, height: 1200 }, locale: "zh-CN"
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(2500);
  if (needAge) {
    await page.evaluate(() => {
      const all = Array.from(document.querySelectorAll("a, button, span"));
      for (const el of all) {
        const t = (el.innerText || "").trim();
        if (/\u5e74\u6ee1\s*18|I am 18/i.test(t) && t.length < 30) { el.click(); return; }
      }
    });
    await page.waitForTimeout(4000);
  }
  await page.waitForTimeout(waitMs);
  const html = await page.content();
  fs.writeFileSync(outPath, html, "utf8");
} catch (e) {
  fs.writeFileSync(outPath, "ERR:" + String(e.message).slice(0, 200), "utf8");
}
await browser.close();
console.log("@@DONE@@");
"""


def fetch_html(url, need_age=False, wait_ms=5000, timeout=180):
    """用 Playwright 取渲染后的 HTML。

    为什么不用 curl：实测 FC2 和 MissAV 对裸 curl 返回 443 字节的空壳
    （Cloudflare / JS 校验），只有真浏览器能拿到内容。
    """
    out_path = os.path.join(HERE, "data", "_av_html.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _run_pw(PW_HTML_JS, [url, out_path, "1" if need_age else "0", str(wait_ms)],
            timeout=timeout)
    if not os.path.exists(out_path):
        return ""
    try:
        html = io.open(out_path, encoding="utf-8", errors="replace").read()
        os.unlink(out_path)
    except Exception:
        return ""
    if html.startswith("ERR:"):
        return ""
    return html


# ── 番号识别 ────────────────────────────────────────────────────────────
def normalize_code(text):
    """从群友的话里抽出番号。

    支持：
      FC2-PPV-4605376 / FC2PPV4605376 / fc2-4605376
      SSIS-001 / SSIS001 / ssis 001
      ABC-12345
    返回 (site_hint, code)；抽不到返回 (None, None)。
    """
    t = str(text or "").strip()
    if not t:
        return None, None
    up = t.upper()

    m = re.search(r"FC2[\s\-_]*PPV[\s\-_]*(\d{5,9})", up)
    if m:
        return "fc2", "FC2-PPV-%s" % m.group(1)
    m = re.search(r"FC2[\s\-_]*(\d{5,9})", up)
    if m:
        return "fc2", "FC2-PPV-%s" % m.group(1)

    # 标准番号：2-6 个字母 + 可选分隔 + 2-5 位数字
    m = re.search(r"\b([A-Z]{2,6})[\s\-_]?(\d{2,5})\b", up)
    if m:
        return "av", "%s-%s" % (m.group(1), m.group(2))
    return None, None


# ── FC2 搜索 ────────────────────────────────────────────────────────────
def search_fc2(keyword, limit=8):
    """FC2 成人市场搜索。返回 [{site,title,url,id,cover}]。"""
    from urllib.parse import quote
    url = "%s/search?keyword=%s" % (FC2_BASE, quote(str(keyword)))
    html = fetch_html(url, wait_ms=4000)
    if not html:
        return []
    # ⚠️ FC2 现在会跳登录页（fc2.com/cn/login.php），搜索要登录态才给结果。
    # 检测到就直接返回空，并往 stderr 打可诊断提示（不然上层只会说"没搜到"）。
    if "login.php" in html[:4000] or ("/article/" not in html and "登录" in html[:3000]):
        sys.stderr.write("[fc2] 搜索被重定向到登录页，需要登录态才能搜\n")
        return []
    items = []
    seen = set()
    # 实测结构（相对链接 + title 属性）：
    #   <a href="/article/4996552/" title="【某标题】..." class="c-cntCard-110-f_thumb_link">
    #     <img src="//contents-thumbnail2.fc2.com/w360/...png">
    for m in re.finditer(
            r'<a href="/article/(\d+)/"\s+title="([^"]{4,300})"',
            html):
        aid, title = m.group(1), m.group(2)
        if aid in seen:
            continue
        seen.add(aid)
        title = re.sub(r"\s+", " ", title).strip()
        # 缩略图：链接之后 500 字符内的 img src
        seg = html[m.start():m.start() + 600]
        cov = ""
        cm = re.search(r'<img[^>]+src="([^"]+)"', seg)
        if cm:
            cov = cm.group(1)
            if cov.startswith("//"):
                cov = "https:" + cov
        items.append({"site": "FC2", "id": aid,
                      "url": "https://adult.contents.fc2.com/article/%s/" % aid,
                      "title": title[:160], "cover": cov})
        if len(items) >= limit:
            break
    return items


# ── MissAV 搜索 ─────────────────────────────────────────────────────────
def search_missav(keyword, limit=8):
    """MissAV 搜索。走 missav123.com（missav.com/.site 都失效了）。"""
    from urllib.parse import quote
    url = "%s/cn/search/%s" % (MISSAV_BASE, quote(str(keyword)))
    html = fetch_html(url, wait_ms=4000)
    if not html or len(html) < 500:
        return []
    items = []
    seen = set()
    # 视频页形如 /cn/fc2-ppv-4605376
    for m in re.finditer(r'href="(https://missav123\.com/cn/([a-z0-9][a-z0-9_-]{3,}))"', html):
        u, slug = m.group(1), m.group(2)
        if slug in ("actresses", "genres", "makers", "release", "contact",
                    "terms", "upload", "saved", "playlists", "history",
                    "klive", "clive", "uncensored-leak", "chinese-subtitle",
                    "vip", "login", "register", "articles", "tags", "search",
                    "about", "dmca", "privacy", "faq", "new", "ranking"):
            continue
        if slug in seen:
            continue
        seen.add(slug)
        # 找该链接附近的标题。
        # MissAV 卡片里同一个视频有好几个 <a>：缩略图链接（文本是时长 "1:17:44"）、
        # 标题链接（文本是完整标题）。必须**取最长的那个**，不能取第一个。
        seg = html[max(0, m.start() - 400):m.start() + 1500]
        cands = []
        for am in re.finditer(
                r'<a[^>]*href="[^"]*' + re.escape(slug) + r'[^"]*"[^>]*>(.*?)</a>',
                seg, re.S):
            txt = re.sub(r"<[^>]+>", " ", am.group(1))
            txt = re.sub(r"\s+", " ", txt).strip()
            if len(txt) > 8:
                cands.append(txt)
        for im in re.finditer(r'<img[^>]*alt="([^"]{8,200})"', seg):
            cands.append(re.sub(r"\s+", " ", im.group(1)).strip())

        def _bad(x):
            if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", x):
                return True
            if x.lower().replace("-", "") == slug.lower().replace("-", ""):
                return True
            return False
        cands = [c for c in cands if not _bad(c)]
        title = max(cands, key=len) if cands else slug
        items.append({"site": "MissAV", "id": slug, "url": u,
                      "title": str(title)[:160],
                      "cover": "https://fourhoi.com/%s/cover-t.jpg" % slug})
        if len(items) >= limit:
            break
    return items


# ── Pornhub 搜索（需浏览器过年龄门）──────────────────────────────────────
PW_SEARCH_JS = r"""
import { chromium } from "playwright";
import fs from "node:fs";

const url = process.argv[2];
const outPath = process.argv[3];
const out = { items: [], error: null, title: "" };

const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1440, height: 1000 }, locale: "zh-CN"
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(3000);
  // 过年龄门
  await page.evaluate(() => {
    const all = Array.from(document.querySelectorAll("a, button, span"));
    for (const el of all) {
      const t = (el.innerText || "").trim();
      if (/年满\s*18|I am 18|Enter/i.test(t) && t.length < 30) { el.click(); return; }
    }
  });
  await page.waitForTimeout(5000);
  out.title = await page.title();
  out.items = await page.evaluate(() => {
    const as = Array.from(document.querySelectorAll("a[href]"));
    const arr = []; const seen = {};
    for (const a of as) {
      const h = a.href;
      const m = h.match(/view_video\.php\?viewkey=([a-z0-9]+)/i);
      if (!m) continue;
      if (seen[m[1]]) continue;
      seen[m[1]] = 1;
      // ⚠️ 视频链接的 innerText 是时长徽章（"23:59"），不是标题。
      // 标题要从 title 属性 / img alt / 附近的文本里找。
      const isDur = (x) => /^\d{1,2}:\d{2}(:\d{2})?$/.test(String(x || "").trim());
      let t = (a.getAttribute("title") || "").trim();
      const box = a.closest("li, .pcVideoListItem, .videoBox, .wrap, div") || a.parentElement;
      let cov = "";
      if (box) {
        const img = box.querySelector("img");
        if (img) {
          cov = img.getAttribute("data-src") || img.src || img.getAttribute("data-image") || "";
          if (!t || isDur(t)) t = (img.getAttribute("alt") || "").trim();
        }
      }
      if (!t || isDur(t)) {
        const cands = box ? Array.from(box.querySelectorAll("a, span, div"))
          .map((e) => (e.innerText || "").trim())
          .filter((x) => x.length > 8 && !isDur(x) && !/^view_video|^\//.test(x)) : [];
        t = cands[0] || "";
      }
      if (!t || isDur(t)) t = "viewkey:" + m[1];     // 兜底，至少不是时长
      arr.push({ key: m[1], url: "https://cn.pornhub.com/view_video.php?viewkey=" + m[1],
                 title: t.slice(0, 160), cover: cov });
    }
    return arr;
  });
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
fs.writeFileSync(outPath, JSON.stringify(out), "utf8");
console.log("@@DONE@@");
"""


def _run_pw(js_code, args, timeout=180):
    """跑 Playwright 脚本。临时 .mjs 必须放在有 node_modules 的目录。"""
    js_dir = PW_MODULE if os.path.isdir(os.path.join(PW_MODULE, "node_modules")) else HERE
    js_path = os.path.join(js_dir, "_av_pw.mjs")
    with io.open(js_path, "w", encoding="utf-8") as f:
        f.write(js_code)
    env = dict(os.environ)
    env["XMF_PROXY"] = PROXY
    env["XMF_UA"] = UA
    env["PLAYWRIGHT_BROWSERS_PATH"] = env.get("PLAYWRIGHT_BROWSERS_PATH") or xmf_paths.BROWSERS
    # ⚠️ C 盘只剩几个 GB，浏览器临时 profile 必须写到 D 盘
    tmp_d = os.path.join(HERE, "data", "_tmp")
    os.makedirs(tmp_d, exist_ok=True)
    env["TEMP"] = tmp_d
    env["TMP"] = tmp_d
    env["TMPDIR"] = tmp_d
    node = NODE if os.path.exists(NODE) else "node"
    try:
        p = subprocess.run([node, js_path] + args, capture_output=True,
                           timeout=timeout, env=env, cwd=js_dir)
        return (p.stdout or b"").decode("utf-8", "replace")
    except Exception as exc:
        return "ERR:" + str(exc)[:200]


def search_pornhub(keyword, limit=8):
    """Pornhub 搜索（需浏览器过年龄门）。"""
    from urllib.parse import quote
    url = "%s/video/search?search=%s" % (PH_BASE, quote(str(keyword)))
    out_path = os.path.join(HERE, "data", "_ph_search.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    raw = _run_pw(PW_SEARCH_JS, [url, out_path], timeout=180)
    if not os.path.exists(out_path):
        return []
    try:
        data = json.loads(io.open(out_path, encoding="utf-8").read())
        os.unlink(out_path)
    except Exception:
        return []
    items = []
    for it in (data.get("items") or [])[:limit]:
        cov = it.get("cover") or ""
        if cov.startswith("//"):
            cov = "https:" + cov
        items.append({"site": "Pornhub", "id": it.get("key"),
                      "url": it.get("url"), "title": it.get("title") or it.get("key"),
                      "cover": cov})
    return items


# ── sukebei 磁力搜索 ────────────────────────────────────────────────────
def search_magnet(keyword, limit=6):
    """在 sukebei.nyaa.si 按番号/关键词找磁力。

    实测 FC2-PPV-4605376 能搜到 2 条，磁力带 tracker，可直接喂 aria2。
    """
    from urllib.parse import quote
    url = "%s/?q=%s&f=0&c=0_0" % (SUKEBEI, quote(str(keyword)))
    html = _curl(url, timeout=35, referer=SUKEBEI + "/")
    if not html:
        return []
    items = []
    # 每一行 <tr> 里有 view 链接 + magnet
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        mag = re.search(r'href="(magnet:\?xt=urn:btih:[^"]+)"', tr)
        if not mag:
            continue
        magnet = mag.group(1).replace("&amp;", "&")
        vid = re.search(r'href="/view/(\d+)"[^>]*title="([^"]*)"', tr)
        title = ""
        if vid:
            title = vid.group(2)
        else:
            t2 = re.search(r'title="([^"]{5,200})"', tr)
            title = t2.group(1) if t2 else ""
        # 体积
        size = ""
        sz = re.search(r'class="text-center">([\d.]+\s*[KMGT]iB)</td>', tr)
        if sz:
            size = sz.group(1)
        # seeders
        seeds = ""
        sd = re.findall(r'class="text-center">(\d+)</td>', tr)
        if len(sd) >= 2:
            seeds = sd[-2]
        hashv = magnet.split("btih:")[1][:40]
        items.append({
            "site": "Magnet", "id": hashv, "url": magnet,
            "title": re.sub(r"\s+", " ", title)[:160],
            "magnet": magnet, "size": size, "seeders": seeds,
            "cover": "",
        })
        if len(items) >= limit:
            break
    return items


# ── 取视频真实地址 ──────────────────────────────────────────────────────
PW_VIDEO_JS = r"""
import { chromium } from "playwright";
import fs from "node:fs";

const url = process.argv[2];
const outPath = process.argv[3];
const out = { m3u8: [], mp4: [], title: "", error: null };

const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1440, height: 1000 }, locale: "zh-CN"
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });
  const page = await ctx.newPage();
  const seen = [];
  page.on("response", (r) => {
    const u = r.url();
    if (/\.m3u8(\?|$)/.test(u)) seen.push(u);
  });

  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(3000);
  await page.evaluate(() => {
    const all = Array.from(document.querySelectorAll("a, button, span"));
    for (const el of all) {
      const t = (el.innerText || "").trim();
      if (/年满\s*18|I am 18/i.test(t) && t.length < 30) { el.click(); return; }
    }
  });
  // 等播放器初始化
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(2500);
    if (seen.length) break;
  }
  out.title = await page.title();
  const html = await page.content();

  // Pornhub: flashvars 里的 mediaDefinitions
  const md = html.match(/mediaDefinitions"?\s*:\s*(\[[\s\S]{0,20000}?\])\s*,\s*"/);
  if (md) {
    try {
      const arr = JSON.parse(md[1].replace(/\\\//g, "/"));
      const hs = arr.filter((x) => x.format === "hls" && x.videoUrl)
                    .sort((a, b) => (b.height || 0) - (a.height || 0));
      for (const h of hs) out.m3u8.push(h.videoUrl);
    } catch (e) {}
  }

  // 网络请求里的 m3u8（MissAV 的 surrit 就在这里）
  for (const u of seen) if (out.m3u8.indexOf(u) < 0) out.m3u8.push(u);

  // HTML 里的 mp4
  const mp = html.match(/https?:\/\/[^\s"'\\<>]+\.mp4[^\s"'\\<>]*/g) || [];
  for (const u of mp) {
    // &amp; 必须还原成 &，否则签名参数坏了会 403
    const clean = u.replace(/\\\//g, "/").replace(/&amp;/g, "&");
    if (/preview|gif|thumb/i.test(clean)) continue;
    if (out.mp4.indexOf(clean) < 0) out.mp4.push(clean);
  }
} catch (e) { out.error = String(e.message).slice(0, 200); }
await browser.close();
fs.writeFileSync(outPath, JSON.stringify(out), "utf8");
console.log("@@DONE@@");
"""


def get_video_url(page_url, timeout=180):
    """打开视频页拿真实播放地址。返回 {ok, m3u8:[], mp4:[], title}。"""
    out_path = os.path.join(HERE, "data", "_av_video.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _run_pw(PW_VIDEO_JS, [page_url, out_path], timeout=timeout)
    if not os.path.exists(out_path):
        return {"ok": False, "error": "取播放地址失败（浏览器没返回）"}
    try:
        data = json.loads(io.open(out_path, encoding="utf-8").read())
        os.unlink(out_path)
    except Exception:
        return {"ok": False, "error": "解析播放地址失败"}
    if data.get("error"):
        return {"ok": False, "error": data["error"]}
    m3u8 = [u for u in (data.get("m3u8") or []) if u.startswith("http")]
    mp4 = [u for u in (data.get("mp4") or []) if u.startswith("http")]
    if not m3u8 and not mp4:
        return {"ok": False, "error": "页面里没找到播放地址（可能要登录或该片已下架）"}
    return {"ok": True, "m3u8": m3u8, "mp4": mp4,
            "title": data.get("title") or ""}


def download_covers(items, limit=6, outdir=None):
    """把候选的封面图下到本地（QQ 只能发本地文件，远程 URL 协议端拉不到）。

    FC2 / MissAV / Pornhub 的封面都在各自 CDN 上，必须走代理下载。
    失败就跳过，不影响主流程。
    """
    if not items:
        return 0
    outdir = outdir or os.path.join(HERE, "data", "avcovers")
    os.makedirs(outdir, exist_ok=True)
    got = 0
    for i, it in enumerate(items):
        if got >= limit:
            break
        cov = str(it.get("cover") or "")
        if not cov.startswith("http"):
            continue
        tag = re.sub(r"[^A-Za-z0-9]", "", str(it.get("id") or ""))[:24] or "x%d" % i
        dest = os.path.join(outdir, "av_%s.jpg" % tag)
        if os.path.exists(dest) and os.path.getsize(dest) > 3000:
            it["coverLocal"] = dest
            got += 1
            continue
        try:
            ok = _download_file(cov, dest, referer="https://missav123.com/", timeout=30)
        except Exception as exc:
            emit({"t": "progress", "stage": "warn",
                  "note": "封面下载异常 %s：%s" % (cov[:40], exc)})
            ok = False
        if ok:
            it["coverLocal"] = dest
            got += 1
    return got


# ── 磁力解析（不下载，只列文件）──────────────────────────────────────────
def magnet_info(magnet, timeout_sec=90):
    """解析磁力：取元数据 → 列文件清单。

    返回 {ok, name, totalSize, infoHash, files:[{idx,path,size}], error}

    为什么值得单独做：群友丢一条磁力过来，最常问的是"这里面是什么"。
    aria2 的 --bt-metadata-only 只拉种子信息（几秒到一分钟），
    再用 --show-files 就能把完整文件列表打出来，不用真的下载。
    """
    if not os.path.exists(ARIA2):
        return {"ok": False, "error": "aria2 没装（找不到 %s）" % ARIA2}
    if not str(magnet).startswith("magnet:"):
        return {"ok": False, "error": "不是磁力链接"}

    cache_d = os.path.join(HERE, "data", "_aria2")
    os.makedirs(cache_d, exist_ok=True)
    dht = os.path.join(cache_d, "dht.dat")
    meta_d = os.path.join(HERE, "data", "_metainfo")
    os.makedirs(meta_d, exist_ok=True)

    # 1) 只取元数据
    cmd = [ARIA2, "--all-proxy=" + PROXY,
           "--dht-file-path=" + dht, "--dht-file-path6=" + dht + "6",
           "--bt-metadata-only=true", "--bt-save-metadata=true",
           "--dir=" + meta_d, "--follow-torrent=mem",
           "--bt-stop-timeout=%d" % max(20, timeout_sec - 10),
           "--summary-interval=0", "--console-log-level=warn",
           magnet]
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout_sec + 20)
    except Exception:
        pass

    # 2) 找刚生成的 .torrent（按修改时间最新的）
    tfiles = []
    for root, _d, fs in os.walk(meta_d):
        for f in fs:
            if f.endswith(".torrent"):
                p2 = os.path.join(root, f)
                tfiles.append((os.path.getmtime(p2), p2))
    if not tfiles:
        return {"ok": False, "error":
                "取不到种子元数据（大概率没有做种，或这个网络连不上 BT 节点）"}
    tfiles.sort(reverse=True)
    tpath = tfiles[0][1]

    # 3) 列文件
    try:
        pr = subprocess.run([ARIA2, "--show-files", tpath],
                            capture_output=True, timeout=60)
        text = (pr.stdout or b"").decode("utf-8", "replace")
    except Exception as exc:
        return {"ok": False, "error": "列文件失败：%s" % exc}

    res = {"ok": True, "name": "", "totalSize": "", "infoHash": "",
           "files": []}
    for line in text.split("\n"):
        ln = line.strip()
        if ln.startswith("Name:"):
            res["name"] = ln[5:].strip()
        elif ln.startswith("Total Length:"):
            res["totalSize"] = ln[13:].strip()
        elif ln.startswith("Info Hash:"):
            res["infoHash"] = ln[10:].strip()
        elif ln.startswith("Magnet URI:"):
            res["magnet"] = ln[11:].strip()

    # 文件行：  1|./xxx.mp4  然后下一行 "|1.9MiB (2,001,226)"
    cur = None
    for line in text.split("\n"):
        m = re.match(r"\s*(\d+)\|(.+)$", line)
        if m and not line.strip().startswith("---"):
            if cur:
                res["files"].append(cur)
            cur = {"idx": int(m.group(1)), "path": m.group(2).strip(), "size": ""}
            continue
        m2 = re.match(r"\s*\|([\d.]+\s*[KMGT]iB)\s*\(([\d,]+)\)", line)
        if m2 and cur:
            cur["size"] = m2.group(1)
            cur["bytes"] = int(m2.group(2).replace(",", ""))
    if cur:
        res["files"].append(cur)

    # 清理旧种子（只留最近 20 个，避免占盘）
    if len(tfiles) > 20:
        for _mt, p3 in tfiles[20:]:
            try:
                os.unlink(p3)
            except Exception:
                pass
    return res


# ── 磁力下载（aria2）────────────────────────────────────────────────────
def download_magnet(magnet, outdir, on_progress=None, max_minutes=30,
                    select_file=None):
    """用 aria2 下载磁力。返回 (路径, 大小) 或 (None, 0)。

    只下视频文件：--select-file 需要先拿到文件列表，这里用
    --bt-metadata-only 先取种子信息再挑最大的视频文件。
    """
    if not os.path.exists(ARIA2):
        return None, 0
    os.makedirs(outdir, exist_ok=True)

    # 第一步：只取元数据，看文件列表
    meta_dir = os.path.join(outdir, "_meta")
    os.makedirs(meta_dir, exist_ok=True)
    cache_d = os.path.join(HERE, "data", "_aria2")
    os.makedirs(cache_d, exist_ok=True)
    dht = os.path.join(cache_d, "dht.dat")
    cmd = [ARIA2, "--all-proxy=" + PROXY,
           "--dht-file-path=" + dht,
           "--dht-file-path6=" + dht + "6",
           "--bt-metadata-only=true",
           "--bt-save-metadata=true", "--seed-time=0",
           "--dir=" + meta_dir, "--follow-torrent=mem",
           "--bt-stop-timeout=120", "--summary-interval=0",
           "--console-log-level=warn", magnet]
    try:
        subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception:
        pass

    # 第二步：直接下（aria2 会自己挑，我们用 --max-download-limit 控制）
    cmd2 = [ARIA2, "--all-proxy=" + PROXY,
            "--dht-file-path=" + dht,
            "--dht-file-path6=" + dht + "6",
            "--dir=" + outdir, "--seed-time=0",
            "--file-allocation=none", "--summary-interval=10",
            "--console-log-level=notice",
            "--bt-max-peers=100", "--max-connection-per-server=16",
            "--split=16", "--continue=true",
            "--bt-stop-timeout=%d" % (max_minutes * 60),
            "--follow-torrent=mem"]
    # 只下指定文件（群友说「下第 2 个」时），避免拖整个种子
    if select_file:
        cmd2.append("--select-file=%s" % select_file)
    cmd2.append(magnet)
    proc = None
    last_pct = -1
    last_t = 0.0
    try:
        proc = subprocess.Popen(cmd2, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        deadline = __import__("time").time() + max_minutes * 60
        for line in proc.stdout:
            now = __import__("time").time()
            # aria2 的进度行长这样：
            #   [#1b2c3d 120MiB/1.2GiB(9%) CN:12 DL:3.4MiB ETA:5m20s]
            m = re.search(r"\((\d+)%\)", line)
            if m:
                pct = int(m.group(1))
                # 每涨 5% 或每 30 秒报一次，别刷屏也别闷着
                if pct >= last_pct + 5 or (now - last_t > 30 and pct != last_pct):
                    last_pct = pct
                    last_t = now
                    speed = ""
                    sm = re.search(r"DL:([\d.]+[KMG]iB)", line)
                    if sm:
                        speed = " " + sm.group(1) + "/s"
                    eta = ""
                    em = re.search(r"ETA:([\dhms]+)", line)
                    if em:
                        eta = " 剩" + em.group(1)
                    done = ""
                    dm = re.search(r"([\d.]+[KMG]iB)/([\d.]+[KMG]iB)", line)
                    if dm:
                        done = " %s/%s" % (dm.group(1), dm.group(2))
                    if on_progress:
                        on_progress({"t": "progress", "stage": "magnet",
                                     "note": "磁力 %d%%%s%s%s" % (pct, done, speed, eta)})
            # 连上了但还没进度时，给个"在连"的反馈
            elif "SEED" in line or "peer" in line.lower():
                if now - last_t > 20 and on_progress:
                    last_t = now
                    on_progress({"t": "progress", "stage": "magnet",
                                 "note": "正在连做种节点…"})
            if now > deadline:
                proc.kill()
                break
    except Exception:
        pass
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    # 找最大的视频文件
    best = None
    best_sz = 0
    for root, _dirs, files in os.walk(outdir):
        if "_meta" in root:
            continue
        for f in files:
            if not re.search(r"\.(mp4|mkv|wmv|avi|mov|ts|m2ts|rmvb|flv|webm)$", f, re.I):
                continue
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except Exception:
                continue
            if sz > best_sz:
                best_sz, best = sz, p
    if best and best_sz > 1024 * 1024:
        return best, best_sz
    return None, 0


if __name__ == "__main__":
    # 自测
    print(json.dumps({
        "code": normalize_code("FC2-PPV-4605376"),
        "code2": normalize_code("ssis-001"),
        "fc2": search_fc2("素人", 3),
        "missav": search_missav("FC2-PPV-4605376", 3),
        "magnet": search_magnet("FC2-PPV-4605376", 2),
    }, ensure_ascii=False, indent=1)[:2000])
