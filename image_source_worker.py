#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image_source_worker.py — 以图搜源

流程：
  1. Google Lens 上传图片（Playwright，走代理）
  2. 从结果页 HTML 的结构化数据里提取：
       · x.com/<user>/status/<id>   → 账号 + 原帖链接
       · instagram.com/p/<code>     → IG 帖子
       · tiktok / pinterest / weibo 等
       · pbs.twimg.com/media/<ID>   → 媒体 ID（可进一步反查）
       · AI 概述 → 角色名 / 服装描述
  3. 用 twscrape 查每个推特账号的资料 + 主页图（头像 / banner）
  4. 下载主页图到本地（QQ 只能发本地文件或 base64）

输出 JSON（每行一个事件）：
  {"t":"progress", ...}
  {"t":"result", "ok":true, "accounts":[...], "posts":[...], "summary":"..."}
"""
import io
import json
import os
import re
import subprocess
import sys
import asyncio

HERE = os.path.dirname(os.path.abspath(__file__))

# ⚠️ import 必须在任何 xmf_paths.xxx 调用之前。
# 之前把 import 放在了 PROXY 赋值后面，导致启动即 NameError。
import xmf_paths

PROXY = xmf_paths.proxy_url()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

NODE = xmf_paths.NODE
PW_MODULE = xmf_paths._pw_module_dir(HERE)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(code, msg):
    emit({"t": "result", "ok": False, "code": code, "error": msg})
    return 1


# ── Lens 抓取脚本（在 Playwright 里跑）────────────────────────────────────
LENS_JS = r"""
import { chromium } from "playwright";
import fs from "node:fs";

const IMG = process.argv[2];
const out = { ok: false, html: "", url: "", error: null };

const browser = await chromium.launch({
  headless: true, channel: "chromium",
  ...(process.env.XMF_PROXY ? { proxy: { server: process.env.XMF_PROXY } } : {}),
  args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
});
try {
  const ctx = await browser.newContext({
    userAgent: process.env.XMF_UA,
    viewport: { width: 1440, height: 1200 },
    locale: "en-US"
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = await ctx.newPage();

  let lensUrl = "";
  page.on("response", (resp) => {
    if (resp.url().indexOf("lens.google.com/v3/upload") >= 0) {
      const loc = resp.headers()["location"] || "";
      if (loc) lensUrl = loc;
    }
  });

  // 用本地表单提交到 Lens v3 上传接口（比点页面元素可靠）
  const html = '<html><body><form id="f" action="https://lens.google.com/v3/upload?stcs=' +
    Date.now() + '" method="POST" enctype="multipart/form-data">' +
    '<input type="file" name="encoded_image" id="i"></form></body></html>';
  const tmp = process.argv[3];
  fs.writeFileSync(tmp, html, "utf8");
  await page.goto("file:///" + tmp.replace(/\\/g, "/"), { waitUntil: "domcontentloaded" });
  await page.setInputFiles("#i", IMG);
  await Promise.all([
    page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => {}),
    page.evaluate(() => document.getElementById("f").submit())
  ]);
  await page.waitForTimeout(4000);

  const target = lensUrl || page.url();
  out.url = target;
  if (target.indexOf("google.com") < 0) {
    out.error = "上传后没拿到结果页";
  } else {
    await page.goto(target, { waitUntil: "domcontentloaded", timeout: 60000 });
    // 等动态内容渲染出来（文本太短说明还在加载）
    for (let i = 0; i < 8; i++) {
      await page.waitForTimeout(2500);
      const len = await page.evaluate(() => document.body.innerText.length);
      if (len > 1200) break;
    }
    out.html = await page.content();
    out.text = await page.evaluate(() => document.body.innerText);
    out.ok = true;
  }
} catch (e) {
  out.error = String(e.message).slice(0, 300);
}
await browser.close();
console.log("@@JSON@@" + JSON.stringify({ ok: out.ok, url: out.url, error: out.error,
  text: out.text || "", htmlLen: (out.html || "").length }));
fs.writeFileSync(process.argv[4], out.html || "", "utf8");
"""


def run_lens(img_path, workdir):
    """调 Playwright 抓 Lens 结果，返回 (ok, html, text, error)。"""
    # ⚠️ 临时 .mjs 必须放在装了 playwright 的目录里：
    # ESM 的 import 是"向上逐级找 node_modules"，放在 data/imgsearch 下
    # 会报 ERR_MODULE_NOT_FOUND（实测踩过）。
    js_dir = PW_MODULE if os.path.isdir(os.path.join(PW_MODULE, "node_modules")) else workdir
    js_path = os.path.join(js_dir, "_lens_run.mjs")
    html_path = os.path.join(workdir, "_lens_out.html")
    tmp_form = os.path.join(workdir, "_lens_form.html")
    with io.open(js_path, "w", encoding="utf-8") as f:
        f.write(LENS_JS)

    node = NODE if os.path.exists(NODE) else "node"
    env = dict(os.environ)
    env["XMF_PROXY"] = PROXY
    env["XMF_UA"] = UA
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or xmf_paths.BROWSERS
    # C 盘空间紧张：浏览器临时 profile 一律写 D 盘
    tmp_d = os.path.join(HERE, "data", "_tmp")
    os.makedirs(tmp_d, exist_ok=True)
    env["TEMP"] = tmp_d
    env["TMP"] = tmp_d
    env["TMPDIR"] = tmp_d

    try:
        p = subprocess.run([node, js_path, img_path, tmp_form, html_path],
                           capture_output=True, timeout=240, env=env,
                           cwd=js_dir)
    except subprocess.TimeoutExpired:
        return False, "", "", "Lens 抓取超时（240 秒）"
    except Exception as exc:
        return False, "", "", "启动 Playwright 失败：%s" % exc

    raw = (p.stdout or b"").decode("utf-8", "replace")
    info = {}
    for line in raw.split("\n"):
        if line.startswith("@@JSON@@"):
            try:
                info = json.loads(line[len("@@JSON@@"):])
            except Exception:
                pass
    html = ""
    if os.path.exists(html_path):
        try:
            html = io.open(html_path, encoding="utf-8", errors="replace").read()
        except Exception:
            pass
    if not info.get("ok"):
        cerr = (p.stderr or b"").decode("utf-8", "replace")[-400:]
        return False, html, info.get("text", ""), info.get("error") or cerr or "未知错误"
    return True, html, info.get("text", ""), ""


# ── 从 HTML 里提取结构化数据 ──────────────────────────────────────────────
def extract(html, text):
    """解析 Lens 结果页，抽出账号/帖子/媒体 ID。"""
    res = {"accounts": [], "posts": [], "media_ids": [], "summary": ""}

    # 1) 推特账号 + 原帖
    tw = {}
    for m in re.finditer(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})/status/(\d+)", html):
        user, sid = m.group(1), m.group(2)
        if user.lower() in ("i", "intent", "share", "home"):
            continue
        tw[(user, sid)] = True
    for (user, sid) in tw:
        res["posts"].append({
            "platform": "x",
            "url": "https://x.com/%s/status/%s" % (user, sid),
            "user": user,
            "id": sid,
        })

    # 2) Instagram
    for code in sorted(set(re.findall(r"instagram\.com/(?:p|reel)/([A-Za-z0-9_-]{5,20})", html))):
        res["posts"].append({
            "platform": "instagram",
            "url": "https://www.instagram.com/p/%s/" % code,
            "id": code,
        })

    # 3) TikTok
    for m in re.finditer(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", html):
        u = m.group(1)
        if u in ("discover", "api"):
            continue
        res["posts"].append({
            "platform": "tiktok",
            "url": "https://www.tiktok.com/@%s" % u,
            "user": u,
        })

    # 4) 媒体 ID（pbs.twimg.com/media/XXX）
    for mid in sorted(set(re.findall(
            r"pbs\.twimg\.com/(?:media|ext_tw_video_thumb|amplify_video_thumb)/([A-Za-z0-9_-]{10,})", html))):
        res["media_ids"].append(mid)

    # 5) AI 概述（页面开头的描述段）
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    # AI 概述一般在"AI による概要"/"AI Overview"之后
    for i, l in enumerate(lines):
        if "概要" in l or "Overview" in l:
            seg = [x for x in lines[i + 1:i + 8] if len(x) > 20]
            if seg:
                res["summary"] = " ".join(seg)[:600]
            break
    if not res["summary"]:
        # 退而求其次：找最长的描述行。
        # ⚠️ 必须排掉 URL 行 —— 实测会把 "URL: https://www.google.com/search?vsrid=..."
        # 当成描述返回，客户端拿到一堆乱码参数。
        def _is_url_line(x):
            t = x.strip()
            if t.startswith("http") or t.startswith("URL:") or t.startswith("url:"):
                return True
            # 字母数字占比低、含大量 ?=& 的也当 URL
            if len(t) > 40 and sum(c in "?=&%/" for c in t) > 8:
                return True
            return False
        cand = [l for l in lines if len(l) > 30 and not _is_url_line(l)]
        if cand:
            res["summary"] = max(cand, key=len)[:600]

    return res


# ── 用 twscrape 查账号资料 + 主页图 ──────────────────────────────────────
def enrich_accounts(users, outdir, max_users=6, tweetids=None):
    """查推特账号的资料（含头像/banner）。返回 [{user, name, followers, desc, avatar, banner}]。"""
    if not users:
        return []
    users = list(dict.fromkeys(users))[:max_users]
    script = os.path.join(HERE, "_enrich.py")
    body = r'''
import asyncio, os, sys, json, io
os.environ.setdefault("TWS_PROXY", xmf_paths.proxy_url())
from twscrape import API

DB = os.environ.get("XMF_DB")
payload = json.loads(sys.argv[1])
users = payload.get("users") or []
tweetids = payload.get("tweets") or {}      # user -> tweetId
out = []

def media_of(tw):
    """取出推文里的图片/视频缩略图。

    twscrape 的 tw.media 是个 Media 对象（不是列表）：
      .photos   [{url}]
      .videos   [{thumbnailUrl, variants, duration}]
      .animated [{thumbnailUrl, variants}]
    实测直接 len() 会 TypeError。
    """
    res = {"images": [], "videoThumb": None, "isVideo": False}
    m = getattr(tw, "media", None)
    if not m:
        return res
    try:
        for ph in (getattr(m, "photos", None) or []):
            u = getattr(ph, "url", None)
            if u:
                res["images"].append(u)
    except Exception:
        pass
    try:
        vids = (getattr(m, "videos", None) or []) + (getattr(m, "animated", None) or [])
        if vids:
            res["isVideo"] = True
            th = getattr(vids[0], "thumbnailUrl", None)
            if th:
                res["videoThumb"] = th
    except Exception:
        pass
    return res

async def main():
    api = API(DB)
    for login in users:
        try:
            u = await api.user_by_login(login)
            if not u:
                out.append({"user": login, "ok": False, "error": "not found"})
                continue
            out.append({
                "user": login, "ok": True,
                "id": str(u.id),
                "name": u.displayname,
                "followers": u.followersCount,
                "following": u.friendsCount,
                "tweets": u.statusesCount,
                "desc": (u.rawDescription or "").replace("\n", " ")[:200],
                "avatar": u.profileImageUrl,
                "banner": getattr(u, "profileBannerUrl", None),
                "created": str(getattr(u, "created", ""))[:10],
            })
            # 顺带取原帖的媒体（截图 / 视频缩略图）
            tid = tweetids.get(login)
            if tid:
                try:
                    tw = await api.tweet_details(int(tid))
                    if tw:
                        mm = media_of(tw)
                        out[-1]["tweetText"] = (tw.rawContent or "")[:200].replace("\n", " ")
                        out[-1]["tweetDate"] = str(tw.date)[:16]
                        out[-1]["tweetImages"] = mm["images"][:4]
                        out[-1]["tweetIsVideo"] = mm["isVideo"]
                        if mm["videoThumb"]:
                            out[-1]["tweetVideoThumb"] = mm["videoThumb"]
                except Exception as te:
                    out[-1]["tweetError"] = str(te)[:120]
        except Exception as e:
            out.append({"user": login, "ok": False, "error": str(e)[:150]})
    io.open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))

try:
    asyncio.run(main())
except Exception as e:
    io.open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(
        [{"ok": False, "error": "RUN: " + str(e)[:200]}], ensure_ascii=False))
'''
    with io.open(script, "w", encoding="utf-8") as f:
        f.write(body)

    res_path = os.path.join(outdir, "_enrich.json")
    env = dict(os.environ)
    env["XMF_DB"] = env.get("XMF_DB") or os.path.join(
        HERE, "..", "..", "..", "data", "x-media-finder", "accounts.db")
    env["XMF_PROXY"] = PROXY
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        payload = {"users": users, "tweets": tweetids or {}}
        subprocess.run([sys.executable, script, json.dumps(payload), res_path],
                       capture_output=True, timeout=240, env=env)
        if os.path.exists(res_path):
            data = json.loads(io.open(res_path, encoding="utf-8").read())
            return [d for d in data if d.get("ok")]
    except Exception:
        pass
    return []


def download_img(url, dest, referer=None):
    """下载图片（走代理）。返回本地路径或 None。"""
    if not url:
        return None
    cmd = ["curl", "-s", "--proxy", PROXY, "-A", UA,
           "--max-time", "30", "-L", "-o", dest]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    try:
        subprocess.run(cmd, capture_output=True, timeout=40)
        if os.path.exists(dest) and os.path.getsize(dest) > 2000:
            return dest
    except Exception:
        pass
    return None


def main():
    raw = sys.stdin.read()
    try:
        task = json.loads(raw)
    except Exception:
        return fail("BAD_ARGS", "任务不是合法 JSON")

    img = str(task.get("image") or "").strip()
    if not img or not os.path.exists(img):
        return fail("BAD_ARGS", "图片不存在：%s" % img)

    workdir = os.path.join(HERE, "data", "imgsearch")
    os.makedirs(workdir, exist_ok=True)
    outdir = os.path.join(HERE, "data", "profiles")
    os.makedirs(outdir, exist_ok=True)

    emit({"t": "progress", "stage": "lens", "done": 0, "total": 4,
          "note": "正在用 Google Lens 识图…"})

    ok, html, text, err = run_lens(img, workdir)
    if not ok:
        return fail("LENS_FAILED", "识图失败：%s" % err)

    emit({"t": "progress", "stage": "parse", "done": 1, "total": 4,
          "note": "解析识别结果…"})

    data = extract(html, text)
    posts = data["posts"]
    tw_users = list(dict.fromkeys([p["user"] for p in posts
                                   if p["platform"] == "x" and p.get("user")]))

    emit({"t": "progress", "stage": "accounts", "done": 2, "total": 4,
          "note": "找到 %d 个推特账号，查询主页资料…" % len(tw_users)})

    # user -> tweetId（用于取原帖媒体）
    tweetids = {}
    for p in posts:
        if p.get("platform") == "x" and p.get("user") and p.get("id"):
            tweetids.setdefault(p["user"], p["id"])

    accounts = enrich_accounts(tw_users, outdir, tweetids=tweetids) if tw_users else []

    # 下载主页图
    emit({"t": "progress", "stage": "avatar", "done": 3, "total": 4,
          "note": "取主页图…"})
    for a in accounts:
        u = a.get("user") or "x"
        av = a.get("avatar") or ""
        # _normal 是小图，换 _400x400 更清楚
        big = av.replace("_normal.", "_400x400.") if "_normal." in av else av
        p = download_img(big, os.path.join(outdir, "av_%s.jpg" % u),
                         referer="https://x.com/%s" % u)
        if p:
            a["avatarLocal"] = p
        bn = a.get("banner")
        if bn:
            p2 = download_img(bn, os.path.join(outdir, "bn_%s.jpg" % u),
                              referer="https://x.com/%s" % u)
            if p2:
                a["bannerLocal"] = p2

        # 原帖截图：优先用推文里的图片，视频则用缩略图
        shots = []
        for i, iu in enumerate((a.get("tweetImages") or [])[:2]):
            big = iu
            if "?" not in big:
                big = big + "?format=jpg&name=medium"   # 缩略图，省流量
            sp = download_img(big, os.path.join(outdir, "tw_%s_%d.jpg" % (u, i)),
                              referer="https://x.com/%s" % u)
            if sp:
                shots.append(sp)
        vt = a.get("tweetVideoThumb")
        if vt:
            sp = download_img(vt, os.path.join(outdir, "twv_%s.jpg" % u),
                              referer="https://x.com/%s" % u)
            if sp:
                shots.append(sp)
        if shots:
            a["tweetShotsLocal"] = shots

    emit({"t": "result", "ok": True,
          "accounts": accounts,
          "posts": posts[:30],
          "mediaIds": data["media_ids"][:12],
          "summary": data["summary"],
          "lensUrl": ""})
    return 0


if __name__ == "__main__":
    sys.exit(main())
