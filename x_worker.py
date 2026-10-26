#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""x-media-finder 的 Python 工作进程：用 twscrape 搜 X，提取媒体直链。

为什么是独立进程而不是在 Node 里直接抓：
  1. twscrape 是 Python 库，带账号池/限频/登录态管理，重写一遍没有意义；
  2. X 的搜索必须带登录态（游客态搜不到敏感内容，也搜不到很多媒体）；
  3. 独立进程崩了不会拖垮机器人主进程 —— 与 jm-suomingor 的 jm_worker.py 同一套
     思路（那里用 Python 是因为没有图像库，这里是因为没有 twscrape）。

用法（stdin 收 JSON，stdout 每行一个 JSON）：
    python x_worker.py --check        # 自检：twscrape 版本 / 账号库可用性
    python x_worker.py                # 读 stdin 的 JSON 任务执行

任务 JSON：
{
  "keyword": "cosplay",
  "extraTags": ["nsfw", "18+", "自撮り"],
  "expandTags": true,
  "maxResults": 12,
  "minLikes": 0,
  "accountsDb": "<技能数据目录>/accounts.db",
  "proxy": "http://127.0.0.1:7890",
  "retries": 3,
  "backoffSeconds": 8
}

输出协议（每行一个 JSON）：
    {"t":"progress","stage":"login","done":0,"total":1,"note":"..."}
    {"t":"progress","stage":"search","done":1,"total":2,"note":"第1轮：扩展词"}
    {"t":"result","ok":true,"items":[...],"query":"...","raw":42,"kept":12}
    {"t":"result","ok":false,"error":"...","code":"RATE_LIMIT|NO_ACCOUNTS|..."}

items 里每项：
    {"id":"123","author":"user","likes":321,"text":"...","url":"https://x.com/...",
     "images":["https://pbs.twimg.com/media/X?format=jpg&name=orig"],"videos":["https://video.twimg.com/...mp4"]}
"""

import asyncio
import base64
import json
import os
import re
import sys
import time
import traceback
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

# ── 错误码 ──────────────────────────────────────────────────────────────────
#
# 为什么需要带码的异常：Node 侧要按 code 决定"回群友哪句话"。
# 早期实现里"账号库不存在/没账号"都是裸 RuntimeError → 被归成通用 ERROR →
# 前端把环境问题当成"搜不到"，回了句「没货，换个关键词试试」。
# 群友照着换了十个关键词也不会好，因为问题根本不在关键词上。

class WorkerError(RuntimeError):
    """带机器可读错误码的业务异常。"""

    def __init__(self, message: str, code: str = "ERROR"):
        super().__init__(message)
        self.code = code


# ── 输出 ────────────────────────────────────────────────────────────────────

def emit(obj):
    """输出一行 JSON 并立即 flush —— Node 侧要实时看进度，不能等缓冲。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ── 垃圾内容过滤 ────────────────────────────────────────────────────────────
#
# 目标：把"带关键词但根本没图"的推、以及明显的营销/引流号挡在外面。
# 宁可少给几条，也不要把一整屏广告塞进群友的卡片里。

# 营销号典型话术：命中即整条丢弃（不区分大小写）
PROMO_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bfollow\s+(me|back)\b",
        r"\bf4f\b",
        r"\bgain\s+followers?\b",
        r"\bdm\s+(me|for)\b",
        r"\bcheck\s+(out\s+)?my\s+(bio|link)\b",
        r"\bsubscribe\b",
        r"\bonlyfans\b",
        r"\bfansly\b",
        r"\btelegram\b",
        r"\bwhatsapp\b",
        # ── Discord / 社群引流：实测最猖獗的一类 ──────────────────────────
        # 真实样本："Join the best NSFW discord server👀 https://discord.gg/xxx"
        # 这类号会连发几十条同一句话，把整个结果页刷满。
        r"join\s+(the\s+)?(best\s+|my\s+|our\s+|this\s+)?.*\bdiscord\b",
        r"\bdiscord\s+(server|group|invite|link)\b",
        r"\bdiscord\.gg\b",
        r"\bjoin\s+(my|our)\s+(server|group|channel)\b",
        r"\bclick\s+(here|the\s+link)\b",
        r"\blink\s+in\s+(my\s+)?bio\b",
        r"\bfree\s+(nudes|porn|onlyfans)\b",
        r"\bpremium\s+(content|snap|set)\b",
        r"\bselling\s+(pics|vids|content|nudes)\b",
        r"私信",
        r"互关",
        r"加微",
        r"引流",
        r"接单",
        r"约炮",
        r"卖片",
        r"扫码",
        r"电报",
        r"进群",
        r"加群",
        r"福利群",
        r"神秘链接",
        r"限定公開",
        r"限定公开",
    )
]

# 转推里常见的噪声标记
NOISE_TOKENS = ("#ad", "#ads", "#promo", "#sponsored")

# 图片直链里这些名字后缀意味着"缩略图"，换成 orig 才有原图
THUMB_NAME_RE = re.compile(r"([?&])name=[^&]*", re.IGNORECASE)


def looks_like_promo(text: str) -> bool:
    """判断一条推文是不是营销/引流内容。"""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    for token in NOISE_TOKENS:
        if token in low:
            return True
    return any(p.search(t) for p in PROMO_PATTERNS)


def to_orig(url: str) -> str:
    """把 Twitter 图片地址换成 name=orig（原图）。

    X 的媒体直链形如：
        https://pbs.twimg.com/media/Gxxx?format=jpg&name=small
        https://pbs.twimg.com/media/Gxxx?format=jpg&name=900x900
    把 name 参数替换成 orig 就拿到原始分辨率版本。
    没有 name 参数时补一个（部分 CDN 分支默认给 small）。
    """
    if not isinstance(url, str) or not url:
        return ""
    if "pbs.twimg.com" not in url:
        return url
    if THUMB_NAME_RE.search(url):
        return THUMB_NAME_RE.sub(r"\1name=orig", url, count=1)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}name=orig"


# ── twscrape 结果 → 纯数据 ──────────────────────────────────────────────────
#
# twscrape 的模型在不同版本里字段名改过（_media / media、.photos / .images、
# variants 里 url 的键名也不完全一致），所以这里一律"防御式取值"：
# 拿不到就当没有，绝不让一条脏数据把整轮搜索打断。

def _get(obj, *names, default=None):
    """从 dict 或对象上按多个候选名取第一个非空值。"""
    for n in names:
        if obj is None:
            return default
        if isinstance(obj, dict):
            v = obj.get(n)
        else:
            v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def extract_photos(media) -> list:
    """取该推文的图片地址（已转 orig）。"""
    out = []
    photos = _as_list(_get(media, "photos", "images", "photo", default=[]))
    for p in photos:
        url = _get(p, "url", "media_url_https", "media_url", "src", default="")
        url = to_orig(str(url))
        if url and url not in out:
            out.append(url)
    return out


def extract_video_details(media) -> list:
    """取视频的详细信息：mp4 直链 + 预览图 + 时长。

    为什么要单独一个函数（而不是只返回 URL 列表）：
    只给一条光秃秃的 mp4 链接，群友根本不知道点开是什么 ——
    这是"视频模式体验差"的直接原因。带上缩略图和时长后，
    卡片里能先看到画面，再决定要不要点链接。

    返回 [{url, thumbnail, duration, kind}]，按码率择优。
    """
    out = []
    if media is None:
        return out

    # ── 1. animated_gif：本质是 mp4，也带缩略图 ────────────────────────────
    for a in _as_list(_get(media, "animated", default=[])):
        url = str(_get(a, "videoUrl", "video_url", "url", default="") or "")
        if not url.startswith("http"):
            continue
        if re.search(r"\.m3u8(\?|$)", url, re.IGNORECASE):
            continue
        thumb = to_orig(str(_get(a, "thumbnailUrl", "thumbnail_url", default="") or ""))
        out.append({"url": url, "thumbnail": thumb, "duration": 0, "kind": "gif"})

    # ── 2. videos：从 variants 里挑最高码率的 mp4 ──────────────────────────
    for v in _as_list(_get(media, "videos", "video", default=[])):
        variants = _as_list(_get(v, "variants", default=[]))
        mp4s = []
        for var in variants:
            url = str(_get(var, "url", "src", default="") or "")
            ctype = str(_get(var, "contentType", "content_type", "type", default="") or "").lower()
            br = _get(var, "bitrate", "bit_rate", default=0) or 0
            if ctype and "mpegurl" in ctype:
                continue
            if ctype and "mp4" not in ctype:
                continue
            if not ctype and not re.search(r"\.mp4(\?|$)", url, re.IGNORECASE):
                continue
            if not url.startswith("http"):
                continue
            try:
                br = int(br)
            except (TypeError, ValueError):
                br = 0
            mp4s.append((br, url))
        if not mp4s:
            direct = str(_get(v, "url", "src", default="") or "")
            if re.search(r"\.mp4(\?|$)", direct, re.IGNORECASE):
                mp4s.append((0, direct))
        if not mp4s:
            continue
        mp4s.sort(key=lambda x: x[0], reverse=True)
        best = mp4s[0][1]
        thumb = to_orig(str(_get(v, "thumbnailUrl", "thumbnail_url", default="") or ""))
        dur = _get(v, "duration", "duration_millis", default=0) or 0
        try:
            dur = int(dur)
        except (TypeError, ValueError):
            dur = 0
        # twscrape 的 duration 单位是毫秒；> 1000 时换算成秒
        if dur > 1000:
            dur = round(dur / 1000)
        out.append({"url": best, "thumbnail": thumb, "duration": dur, "kind": "video"})

    # 去重（同一条推文可能既在 animated 又在 videos 里）
    seen = set()
    uniq = []
    for d in out:
        if d["url"] in seen:
            continue
        seen.add(d["url"])
        uniq.append(d)
    return uniq


def extract_videos(media) -> list:
    """取该推文的视频 mp4 直链（只要 URL 列表，供过滤/去重判断用）。

    详细字段（缩略图/时长）走 extract_video_details，这里保持简单签名，
    因为大部分调用点只需要"有没有视频"。
    """
    return [d["url"] for d in extract_video_details(media)]


def tweet_to_item(tweet) -> dict:
    """把一条 twscrape tweet 转成本技能的 item；没有可用媒体时返回 None。"""
    media = _get(tweet, "media", "_media", default=None)
    images = extract_photos(media) if media is not None else []
    video_details = extract_video_details(media) if media is not None else []
    videos = [d["url"] for d in video_details]

    # ── 过滤 1：无媒体推直接丢 ────────────────────────────────────────────
    # 需求明确要求"过滤纯文字推"，而且卡片里放纯文字毫无意义。
    if not images and not videos:
        return None

    text = str(_get(tweet, "rawContent", "raw_content", "text", "content", default="") or "")
    # ── 过滤 2：营销号 ──────────────────────────────────────────────────
    if looks_like_promo(text):
        return None

    user = _get(tweet, "user", default=None)
    author = str(_get(user, "username", "screen_name", default="") or "").strip().lstrip("@")

    likes = _get(tweet, "likeCount", "like_count", "favorites", default=0) or 0
    try:
        likes = int(likes)
    except (TypeError, ValueError):
        likes = 0

    # 回复 / 转推标记：这两种多半不是内容本体（回复是评论区闲聊，
    # 转推是搬运），用于排序降权，不直接丢 —— 偶尔也有好内容。
    is_reply = _get(tweet, "inReplyToTweetId", "in_reply_to_status_id", default=None) is not None
    is_retweet = _get(tweet, "retweetedTweet", "retweeted_tweet", default=None) is not None
    sensitive = _get(tweet, "possibly_sensitive", default=False) is True

    tweet_id = str(_get(tweet, "id", "id_str", "tweet_id", default="") or "")
    url = str(_get(tweet, "url", default="") or "")
    if not url and author and tweet_id:
        url = f"https://x.com/{author}/status/{tweet_id}"

    return {
        "id": tweet_id,
        "author": author,
        "likes": likes,
        # 原文：卡片里要显示"这条视频推文写了什么"。
        # 保留到 300 字（原 200），中文/日文一句就占不少。
        "text": re.sub(r"\s+", " ", text).strip()[:300],
        "url": url,
        "images": images,
        "videos": videos,
        # 视频详情：预览图 + 时长。卡片里先给画面再给链接，
        # 否则群友只看到一条裸 URL 不知道是什么。
        "videoDetails": video_details,
        "isReply": is_reply,
        "isRetweet": is_retweet,
        "sensitive": sensitive,
        # 保留原始 tweet 供打分（adult_score 需要 hashtags / possibly_sensitive）
        "_tweet": tweet,
    }


# ── 相关性打分 ──────────────────────────────────────────────────────────────
#
# 为什么需要：实测搜 "cosplay" 时，前排混进大量**回复**（别人在评论区说
# "cosplay her on a road trip"）、零互动搬运号。它们都满足 filter:media，
# 但不是用户要的东西 —— 这就是"质量低"的来源。
#
# 注意这是**排序**不是过滤：宁可把更可能的排前面，也不凭空删结果
# （删了就成了"搜不到"，比"排得不好"更糟）。
# 唯一的硬过滤是下面 mode 里的"纯图片/纯视频"——那是用户显式要求的。

# 成人向标签分**两级**。为什么要分：
#   adultOnly 早期只要求"命中任意成人词"，结果 bikini / swimsuit / sexy /
#   水着 / 下着 这类**擦边**标签也算了 —— 用户开"仅成人"却仍收到软色情视频。
#   现在拆开：
#     STRONG = 明确裸露/性行为/成人向（真正的 18+）
#     WEAK   = 性感/泳装/内衣/写真（擦边，不等于 18+）
#   adultStrict 打开时只认 STRONG。

ADULT_STRONG = {
    # 英文
    "nsfw", "nsfwtwt", "nsfwsky", "r18", "r18+", "18+", "18禁",
    "porn", "xxx", "hentai", "explicit", "hardcore",
    "nude", "nudes", "naked", "nudeart", "nudemodel", "fullnude",
    # 日文
    "ヌード", "全裸", "半裸", "おっぱい", "痴女", "露出",
    "エロ", "エッチ", "えっち", "えろ", "裏垢", "裏垢女子", "過激",
    # 中文
    "裸", "全裸", "露点", "露點", "私处", "私處",
}

ADULT_WEAK = {
    # 英文：性感但不一定 18+
    "sexy", "lewd", "ecchi", "ero", "erotic", "adult",
    "bikini", "swimsuit", "gravure", "lingerie",
    "onlyfans", "fansly",
    # 日文
    "水着", "下着", "グラビア", "写真", "寫真", "自撮り",
    # 中文
    "色气", "色氣", "骚", "騷", "福利", "私房",
}

# 兼容旧代码：并集（排序/过滤仍用它判断"是否成人向"）
ADULT_TAGS = ADULT_STRONG | ADULT_WEAK


def adult_score(tweet) -> int:
    """算成人向信号强度（强信号权重更高），用于排序。

    STRONG 命中 +3，WEAK 命中 +1。
    这样排出来的顺序天然偏好硬核内容，而不是被 #sexy 之类擦边标签挤满。
    """
    score = 0
    if _get(tweet, "possibly_sensitive", default=False) is True:
        score += 3
    tags = {str(t).lower().lstrip("#") for t in (_get(tweet, "hashtags", default=[]) or [])}
    strong_hits = len(tags & ADULT_STRONG)
    weak_hits = len(tags & ADULT_WEAK)
    score += min(strong_hits, 3) * 3
    score += min(weak_hits, 3)
    # 正文命中（只看强信号词，避免 "sexy" 到处都有导致误判）
    text = str(_get(tweet, "rawContent", "raw_content", "text", default="") or "").lower()
    body_strong = sum(1 for w in ADULT_STRONG if w in text)
    score += min(body_strong, 2)
    return score


def has_strong_adult(tweet) -> bool:
    """是否命中**强**成人信号（裸露/性行为/明确成人向）。

    adultStrict 用它做硬门槛。注意 #sexy #bikini #水着 这类**不算** ——
    它们只是擦边，用户开"仅 18+"时不该出现。
    """
    if tweet is None:
        return False
    tags = {str(t).lower().lstrip("#") for t in (_get(tweet, "hashtags", default=[]) or [])}
    if tags & ADULT_STRONG:
        return True
    text = str(_get(tweet, "rawContent", "raw_content", "text", default="") or "").lower()
    return any(w in text for w in ADULT_STRONG)


def quality_score(item: dict) -> float:
    """综合排序分。

    组成：
      · 成人信号 × 10  —— 用户要的就是这个，权重最高
      · log(点赞) × 2  —— 用对数压缩，避免一条 10 万赞旧推压掉一整屏新内容
      · 回复 -15       —— 评论区闲聊不是内容本体
      · 转推 -5        —— 搬运号多半不是作者本人
    """
    import math

    tweet = item.get("_tweet")
    score = adult_score(tweet) * 10.0 if tweet is not None else 0.0
    likes = max(0, int(item.get("likes") or 0))
    score += math.log1p(likes) * 2.0
    if item.get("isReply"):
        score -= 15.0
    if item.get("isRetweet"):
        score -= 5.0
    return score


def want_enough(collected: list, max_results: int, mode: str) -> int:
    """判断"候选够不够了"的目标条数。

    不能只看总数：mix 模式要求图片和视频都尽量有，
    否则前 12 条全是图就提前收工，"一半视频"落空。
    """
    if mode == "mix":
        imgs = sum(1 for x in collected if x["images"])
        vids = sum(1 for x in collected if x["videos"])
        half = max(1, max_results // 2)
        if imgs >= half and vids >= half:
            return max_results
        # 还缺一边 → 继续收（但设个上限，别无限翻页）
        return min(max_results * 3, max(max_results, imgs + vids) + max_results)
    return max_results


def pick_images_first_balanced(images: list, videos: list, limit: int) -> list:
    """把图片和视频交错组合，让卡片里两种内容穿插出现。

    为什么不直接 images + videos 拼接：那样卡片前半屏全是图、后半屏全是视频，
    浏览起来很割裂。交错后体验更均匀。
    """
    out = []
    i = j = 0
    # 图片先手（图多、加载快，先给点确定的）
    take_image = True
    while len(out) < limit and (i < len(images) or j < len(videos)):
        if take_image and i < len(images):
            out.append(images[i]); i += 1
        elif j < len(videos):
            out.append(videos[j]); j += 1
        elif i < len(images):
            out.append(images[i]); i += 1
        else:
            break
        take_image = not take_image
    # 按综合分再排一次，保证交错后高分的仍在前面
    out.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return out


def limit_per_author(items: list, limit: int) -> list:
    """限制同一作者在一次结果里最多几条。

    为什么需要：实测搜 cosplay 视频时，前 4 条全是同一个号
    （连发几十条 "Join the best NSFW discord server"）。
    即使文案过滤漏掉个别变体，作者维度也能兜住 —— 一个号刷满整个卡片
    是最影响观感的情况。
    """
    if not limit or limit <= 0:
        return items
    counts = {}
    out = []
    for it in items:
        a = str(it.get("author") or "").lower() or "_unknown"
        if counts.get(a, 0) >= limit:
            continue
        counts[a] = counts.get(a, 0) + 1
        out.append(it)
    return out


# ── 相关性过滤 ──────────────────────────────────────────────────────────────
#
# 问题：搜「奥黛塔」时会混进完全无关的内容（实测有 911 新闻、AI 图集），
# 因为 X 的搜索本身就不精确，加上 filter:media 之后相关性更松。
#
# 做法分两层：
#   1. **别名扩展**（最强）：关键词是人名/角色名时，模型会通过 includeTerms
#      传进中文/日文/英文的各种写法（奥黛塔 / オデット / Odette）。
#      只有这样才能同时留下用了不同语言写法的内容。
#   2. **共现扩展**（兜底）：没有别名时，先找出正文里真的含关键词的"锚点"，
#      再取这些锚点里出现率高的 hashtag（如 #odette #GenshinImpact），
#      用它们把"换了写法但同主题"的内容也纳进来。
#
# 太通用的标签必须排除，否则 #cosplay 会把所有东西都放进来。

# 这些标签几乎每条都有，不能拿来当相关性依据
GENERIC_TAGS = {
    "cosplay", "cosplayer", "cosplaygirl", "nsfw", "nsfwtwt", "nsfwsky",
    "r18", "18+", "sexy", "lewd", "美女", "自撮り", "selfie",
    "aiart", "ai", "photography", "art", "写真",
}


def _norm_text(s: str) -> str:
    """归一化用于匹配的文本：小写、去空白、去 # 。"""
    return re.sub(r"[\s#]+", "", str(s or "").lower())


def item_matches_terms(item: dict, terms: list) -> bool:
    """item 的正文/标签/作者里是否出现任一 term。"""
    if not terms:
        return True
    blob = _norm_text(_item_blob(item))
    tags_norm = [_norm_text(t) for t in match_hashtags(item)]
    for t in terms:
        n = _norm_text(t)
        if not n:
            continue
        if n in blob:
            return True
        if any(n in tg or tg in n for tg in tags_norm if tg):
            return True
    return False


def build_cooccur_tags(items: list, keyword: str, min_ratio: float = 0.5) -> list:
    """从"含关键词的锚点"里提取高共现 hashtag，用作扩展匹配词。

    例：搜「奥黛塔」时锚点里有 #odette #GenshinImpact #cosplay
        → 排除 cosplay（太通用）→ 得到 [odette, genshinimpact]
    """
    key_norm = _norm_text(keyword)
    if not key_norm:
        return []
    anchors = []
    for it in items:
        # 正文或标签里含关键词（或关键词含标签）才算锚点
        blob = _norm_text(_item_blob(it))
        tags_norm = [_norm_text(t) for t in match_hashtags(it)]
        if key_norm in blob or any(key_norm in t for t in tags_norm if t):
            anchors.append(it)
    if len(anchors) < 2:
        return []
    counts = {}
    for it in anchors:
        for t in set(match_hashtags(it)):
            if not t or t in GENERIC_TAGS:
                continue
            counts[t] = counts.get(t, 0) + 1
    need = max(2, int(len(anchors) * min_ratio))
    picked = [t for t, c in counts.items() if c >= need]
    # 限制数量，避免扩得太宽
    picked.sort(key=lambda t: counts[t], reverse=True)
    return picked[:6]


# ── 地区 / 语言过滤 ─────────────────────────────────────────────────────────
#
# 「不要欧美，只要亚洲（日本/中国）」这个需求是可以**客观判定**的，
# 因为文字系统本身就带地区信息：
#   · 日文 → 平假名/片假名（U+3040-U+30FF）几乎只出现在日文里
#   · 中文 → 汉字 + 中文特有标点/常用字
#   · 欧美 → 拉丁字母主导，且没有假名/汉字
#
# 这比"按 hashtag 猜地区"可靠得多：#westerncosplay 这种标签很少人用，
# 但"这段文字是英文还是日文"永远能测出来。
#
# ⚠️ 注意：这是**语言/文字系统**判定，不是人种判定。
#    它判断的是"这条内容用什么语言写的"，与创作者长相、种族无关 ——
#    一个日本博主用英文发推，会被归到英文；这是内容分类该有的行为。

# 假名区间：平假名 + 片假名
_KANA = re.compile(r"[\u3040-\u30ff]")
# 汉字（CJK 统一表意文字）
_HAN = re.compile(r"[\u4e00-\u9fff]")
# 拉丁字母
_LATIN = re.compile(r"[A-Za-z]")


def detect_script(text: str) -> str:
    """判断一段文本主要用什么文字系统。

    返回 "ja"（日文，含假名）/ "zh"（中文，有汉字无假名）/
         "latin"（拉丁字母主导）/ "unknown"。

    判定顺序有讲究：先看假名 —— 只要有假名就是日文，
    哪怕汉字更多（日文本来就大量用汉字）。
    """
    t = str(text or "")
    if not t.strip():
        return "unknown"
    kana = len(_KANA.findall(t))
    han = len(_HAN.findall(t))
    latin = len(_LATIN.findall(t))
    total = kana + han + latin
    if total == 0:
        return "unknown"
    if kana >= 2 or (kana >= 1 and kana * 20 >= total):
        return "ja"
    if han >= 2 and han * 2 >= total:
        return "zh"
    if latin * 2 >= total:
        return "latin"
    return "unknown"


def item_script(item: dict) -> str:
    """item 的语言归属：正文 + 标签 + 作者名一起判。

    为什么把作者名也算进来：正文可能是纯 emoji 或只有链接，
    这时作者名（如 @jp_girl_xxx / 桜井）是很好的补充信号。
    """
    parts = [str(item.get("text") or ""), str(item.get("author") or "")]
    t = item.get("_tweet")
    if t is not None:
        for tag in (_get(t, "hashtags", default=[]) or []):
            parts.append(str(tag))
    return detect_script(" ".join(parts))


# 地区 → 可接受的语言集合
REGION_LANGS = {
    "asia": {"ja", "zh"},
    "japan": {"ja"},
    "china": {"zh"},
    "western": {"latin"},
    "any": {"ja", "zh", "latin", "unknown"},
}


# ── 地区关键词扩展 ──────────────────────────────────────────────────────────
#
# 关键洞察：**用英文关键词搜，X 就给你英文内容**。
# 实测 search"cosplay #nsfw" 返回 19/20 条 latin；只有 region 过滤会剩 0 条。
# 想要亚洲内容，关键词本身就得是日文/中文。
#
# 所以选了 region=asia 时，把常见题材词自动扩展成日文写法一起搜。
# 这不是翻译器，只是一个够用的常用词表 —— 覆盖 cosplay 圈的高频词。

# 英文 → 日文/中文别名（用于 region 扩展）
REGION_KEYWORD_MAP = {
    "cosplay":      ["コスプレ", "cos"],
    "cosplayer":    ["コスプレイヤー", "コスプレ"],
    "selfie":       ["自撮り", "自拍"],
    "lingerie":     ["下着", "ランジェリー", "内衣"],
    "swimsuit":     ["水着", "泳装"],
    "nude":         ["ヌード", "全裸", "裸"],
    "naked":        ["全裸", "裸"],
    "school":       ["制服", "学生服"],
    "uniform":      ["制服"],
    "maid":         ["メイド", "女仆"],
    "kimono":       ["着物", "和服"],
    "bikini":       ["ビキニ", "比基尼"],
    "thigh":        ["太もも", "大腿"],
    "stocking":     ["ニーハイ", "黒タイツ", "丝袜"],
    "pantyhose":    ["黒タイツ", "パンスト"],
}


def expand_keywords_by_region(keyword: str, region: str) -> list:
    """按地区把关键词扩展成对应语言的同义写法。

    返回要**额外**搜的关键词列表（原关键词不在里面）。
    region=asia / japan 时补日文写法；region=china 时补中文写法。

    只对表里有的词生效 —— 不在表里就返回空，不做机械转换
    （机械音译只会产生搜不到任何东西的乱码词）。
    """
    region = str(region or "any").strip().lower()
    if region not in ("asia", "japan", "china"):
        return []
    kw = str(keyword or "").strip().lower()
    if not kw:
        return []
    out = []
    # 整词匹配，也允许关键词里含这个词（如 "cyber cosplay"）
    for en, alts in REGION_KEYWORD_MAP.items():
        if en != kw and en not in kw.split():
            continue
        for a in alts:
            # japan/asia 要日文；china 只要中文写法（跳过含假名的）
            if region == "china" and _KANA.search(a):
                continue
            if a not in out:
                out.append(a)
    return out


def filter_region(items: list, region: str) -> tuple:
    """按语言把内容分成"符合地区"和"不符合"。

    region:
      · "any"     不过滤（默认）
      · "asia"    只留日文/中文
      · "japan"   只留日文
      · "china"   只留中文
      · "western" 只留拉丁字母（想要欧美向时用）

    返回 (kept, dropped)。

    安全阀：如果过滤后一条都不剩，**原样返回**并报告 dropped=0 ——
    宁可给几条不合适的，也不要让群友以为"没货"。
    这个判断交给上层：真正空结果时会走"没货"话术。
    """
    region = str(region or "any").strip().lower()
    if region == "any" or not items:
        return items, 0
    allow = REGION_LANGS.get(region)
    if not allow:
        return items, 0

    kept = []
    for it in items:
        sc = item_script(it)
        # unknown（纯 emoji/纯数字/纯链接）一律保留：不能因为判不出来就丢掉
        if sc in allow or sc == "unknown":
            kept.append(it)
    if not kept:
        return items, 0
    return kept, len(items) - len(kept)


def filter_relevance(items: list, keyword: str, include_terms: list,
                     enabled: bool = True) -> tuple:
    """相关性过滤。返回 (kept, dropped, terms_used)。

    宁可少留也不硬删：只有当"匹配得到的条数 >= 期望下限"时才真的过滤，
    否则（比如关键词太生僻、一条都匹配不上）放行全部 ——
    报"没货"比给一堆无关内容更糟，但比"明明有却不给"好。
    """
    if not enabled or not items:
        return items, 0, []

    terms = [t for t in (include_terms or []) if str(t).strip()]
    # 关键词本身也算一个 term
    if keyword:
        terms.append(keyword)
    terms = list(dict.fromkeys(terms))

    matched = [it for it in items if item_matches_terms(it, terms)]
    # 用共现扩展再捞一轮
    cooc = build_cooccur_tags(items, keyword)
    if cooc:
        extra = [it for it in items
                 if it not in matched and item_matches_terms(it, cooc)]
        if extra:
            matched.extend(extra)
            terms = terms + cooc

    # 安全阀：匹配太少说明判断不可靠，不过滤
    if len(matched) < max(2, len(items) // 8):
        return items, 0, []

    dropped = len(items) - len(matched)
    return matched, dropped, terms


def filter_adult(items: list, enabled: bool, strict: bool = False) -> tuple:
    """硬过滤：只留"明确成人向"的内容。

    为什么需要（对应"涩情程度不够"这个反馈）：
      quality_score 只是**排序**，一条 500 万赞的普通 cosplay 照样能压过
      一条没互动的荤图。用户要的是"只给我荤的"时，必须硬过滤。

    判定标准（任一命中即算成人向，与 adult_score 同源但更严格）：
      · X 自己标了 possibly_sensitive
      · hashtag 里出现成人词
      · 正文里出现**两个以上**成人词（单个词太容易误判，比如 "sexy" 到处都有）

    返回 (kept, dropped)。dropped>0 说明过滤真的生效了。
    """
    if not enabled:
        return items, 0
    kept = []
    for it in items:
        t = it.get("_tweet")
        if strict:
            # 严格模式：只认强信号。#sexy/#bikini/#水着 这类擦边**不放行**。
            if t is not None and has_strong_adult(t):
                kept.append(it)
            continue
        if t is not None and adult_score(t) > 0:
            kept.append(it)
            continue
        # _tweet 已被清掉时（例如补搜路径）用 item 自身字段兜底判断
        if it.get("sensitive") is True:
            kept.append(it)
            continue
    return kept, len(items) - len(kept)


# ── 自定义屏蔽 / 偏好 ───────────────────────────────────────────────────────
#
# 为什么做成"用户自定义标签表"而不是内置固定规则：
#   1. 每个人的口味不一样，硬编码一定会踩到别人
#   2. X 的推文元数据里**没有**人脸、种族、国家这类结构化字段，
#      唯一可靠的分类依据就是 hashtag / 正文关键词 / 作者
#   3. 用户自己在配置里加词，比等改代码灵活
#
# 匹配方式（三处都查）：
#   · hashtag（最准，X 已经把内容分类好了）
#   · 正文
#   · 作者名（有些号名字里就带标识，比如 ai_girl_xxx）
#
# ⚠️ 有一条线我不越过：不提供"按人种过滤"的预置（比如按 #blackcosplayer
#    这类种族标签屏蔽）。那是在按种族筛人，不是内容分类。
#    地区/风格类（欧美 cosplay vs 亚洲 cosplay）属于内容分类，可以做。

# AI 内容的典型标签。这些是真实存在的、被创作者自己打的分类标签。
DEFAULT_AI_TAGS = [
    "aiart", "ai-art", "aiartwork", "aiイラスト", "ai画像", "ai生成",
    "ai生成画像", "aigenerated", "ai_generated", "aigirl", "aiグラビア",
    "midjourney", "stablediffusion", "stable_diffusion", "novelai",
    "sdwebui", "comfyui", "aicg", "ai美女", "aiコスプレ",
]

# AI 内容在**正文**里常见的写法。
# 为什么需要单独一组：标签是 #stablediffusion（无空格），但正文里通常写
# "made with stable diffusion"（有空格）。只查标签会漏掉这类。
DEFAULT_AI_WORDS = [
    "stable diffusion", "stablediffusion", "midjourney", "novelai",
    "ai-generated", "ai generated", "ai生成", "ai 生成", "ai art",
    "aiイラスト", "生成ai", "comfyui", "sdxl", "controlnet",
]

# 常见"地区/风格"标签，仅供用户抄进配置，**不默认启用**
REGION_TAG_HINTS = {
    "western": ["欧美cosplay", "海外コスプレ", "westerncosplay", "eurocosplay"],
    "asian": ["アジア", "日本コスプレ", "韓国コスプレ", "asiancosplay", "和装"],
}


def _norm_token(s: str) -> str:
    """归一化一个匹配词：去 #、去空白、小写。"""
    return str(s or "").strip().lstrip("#").strip().lower()


def _item_blob(item: dict) -> str:
    """把一条 item 里所有可匹配的文本拼起来（小写）。

    包含：正文、作者名、URL。hashtag 单独在 match_hashtags 里查，
    因为 hashtag 需要精确匹配（避免 "ai" 命中 "airdrop"）。
    """
    parts = [
        str(item.get("text") or ""),
        str(item.get("author") or ""),
        " ".join(str(u) for u in (item.get("images") or [])),
        " ".join(str(u) for u in (item.get("videos") or [])),
        str(item.get("url") or ""),
    ]
    return " ".join(parts).lower()


def match_hashtags(item: dict) -> list:
    """取该 item 的 hashtag 列表（小写、去 #）。"""
    t = item.get("_tweet")
    if t is None:
        return []
    tags = _get(t, "hashtags", default=[]) or []
    return [_norm_token(x) for x in tags if x]


def is_blocked(item: dict, block_tags: list, block_words: list) -> tuple:
    """判断这条是否该被屏蔽。返回 (blocked, reason)。

    优先级：hashtag 精确命中 > 文本/作者包含。
    文本匹配用子串（"ai生成" 能命中正文），
    但对很短、容易误伤的词（<=2 字符，如 "ai"）要求词边界，
    否则 "airdrop"/"chain" 会被误杀。
    """
    tags = set(match_hashtags(item))
    for t in block_tags:
        if t and t in tags:
            return True, "tag:" + t

    blob = _item_blob(item)
    for w in block_words:
        if not w:
            continue
        if len(w) <= 3:
            # 短词要求独立出现，避免误伤 air / chain / wait 之类
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", blob):
                return True, "word:" + w
        else:
            if w in blob:
                return True, "word:" + w
    return False, ""


def prefer_score(item: dict, prefer_tags: list, prefer_words: list) -> float:
    """偏好加分（不是过滤）：命中偏好词的结果排更前面。"""
    score = 0.0
    tags = set(match_hashtags(item))
    for t in prefer_tags:
        if t and t in tags:
            score += 15.0
    blob = _item_blob(item)
    for w in prefer_words:
        if not w:
            continue
        if len(w) <= 3:
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", blob):
                score += 5.0
        elif w in blob:
            score += 5.0
    return score


def apply_custom_filters(items: list, block_tags: list, block_words: list,
                         prefer_tags: list, prefer_words: list) -> tuple:
    """先按屏蔽词硬过滤，再给剩下的加偏好分。

    返回 (kept, blocked_count, block_reasons)。
    block_reasons 只保留前 10 条，用于日志排障（不做成对外内容）。
    """
    kept = []
    reasons = []
    blocked = 0
    for it in items:
        hit, why = is_blocked(it, block_tags, block_words)
        if hit:
            blocked += 1
            if len(reasons) < 10:
                reasons.append(why)
            continue
        bonus = prefer_score(it, prefer_tags, prefer_words)
        if bonus:
            it["_score"] = float(it.get("_score", 0.0)) + bonus
        kept.append(it)
    return kept, blocked, reasons


# ── 自然语言触发 ────────────────────────────────────────────────────────────
#
# 用户说"不要ai"、"别给我欧美"、"要亚洲的"时，运行期临时改过滤条件，
# 不用去改配置。解析出来的是**同一个** block/prefer 结构，
# 所以配置里填的词和嘴上说的词走完全相同的代码路径 —— 行为可预测。

# 说法 → 归一化的意图关键字。用子串匹配，容忍"不要AI图""别要ai生成的"等变体。
_NEG_PREFIX = ("不要", "别给我", "别要", "别给", "不看", "屏蔽", "过滤掉",
              "去掉", "排除", "不想要", "讨厌")
_POS_PREFIX = ("都给我", "想要", "想看", "只要", "来点", "多发", "偏向", "要")

# 别名表：用户说的词 -> 实际参与匹配的标签/关键词
# 键统一小写；匹配时按**长度降序**尝试，避免 "ai" 抢在 "ai生成" 前面。
_ALIAS = {
    "ai生成": DEFAULT_AI_TAGS,
    "ai生成画像": DEFAULT_AI_TAGS,
    "aiイラスト": DEFAULT_AI_TAGS,
    "aigc": DEFAULT_AI_TAGS,
    "ai图": DEFAULT_AI_TAGS,
    "ai": DEFAULT_AI_TAGS,
    "欧美": ["欧美cosplay", "海外コスプレ", "westerncosplay", "eurocosplay", "european"],
    "亚洲": ["アジア", "日本コスプレ", "韓国コスプレ", "asiancosplay", "asian"],
    "日本": ["日本", "japanese", "和装"],
    "韩国": ["韓国", "korean", "kpop"],
}

# 地区意图 → region 取值。口头说"不要欧美"时直接落到 region 过滤上，
# 比只加几个没人用的 hashtag 靠谱。
_REGION_INTENT = {
    "欧美": "western",
    "亚洲": "asia",
    "日本": "japan",
    "中国": "china",
    "韩国": "asia",
}

# 别名按长度降序（长词优先匹配）
_ALIAS_LONGEST_FIRST = sorted(_ALIAS.items(), key=lambda kv: len(kv[0]), reverse=True)


def parse_custom_intent(keyword: str) -> dict:
    """从用户输入里解析出屏蔽/偏好意图。

    "不要ai的cosplay"  -> keyword="cosplay",   blockTags=AI标签, blockWords=["ai"]
    "要亚洲的cosplay"  -> keyword="cosplay",   preferTags=[亚洲...], preferWords=["亚洲"]
    "想看日本cosplay"  -> keyword="cosplay",   preferTags=[日本...], preferWords=["日本"]
    "cosplay"          -> keyword="cosplay"（无意图）

    返回 {"keyword", "blockTags", "blockWords", "preferTags", "preferWords"}
    """
    kw = str(keyword or "").strip()
    out = {"keyword": kw, "blockTags": [], "blockWords": [],
           "preferTags": [], "preferWords": [], "region": ""}
    if not kw:
        return out

    rest = kw
    changed = False
    # 前缀按长度降序，保证 "别给我" 优先于 "别给"
    for prefixes, bucket, tag_bucket in (
        (sorted(_NEG_PREFIX, key=len, reverse=True), "blockWords", "blockTags"),
        (sorted(_POS_PREFIX, key=len, reverse=True), "preferWords", "preferTags"),
    ):
        for p in prefixes:
            idx = rest.find(p)
            if idx < 0:
                continue
            tail = rest[idx + len(p):]
            # 先按**最长别名**尝试（"ai生成" 优先于 "ai"，"日本" 优先于 "日"）
            token = ""
            consumed = 0
            low = tail.lower()
            for alias, _tags in _ALIAS_LONGEST_FIRST:
                if low.startswith(alias):
                    token = alias
                    consumed = len(alias)
                    break
            if not token:
                # 没命中别名：取一段词，遇到常见虚词/标点就停
                m = re.match(r"\s*([^\s,，。、的了吗啊呀呢]{1,8})", tail)
                if not m:
                    continue
                token = m.group(1).strip()
                consumed = m.end() - (len(m.group(1)) - len(token)) if token else 0
                consumed = m.end()
            if not token:
                continue
            key = token.lower()
            aliases = _ALIAS.get(key)
            if aliases:
                for a in aliases:
                    n = _norm_token(a)
                    if n and n not in out[tag_bucket]:
                        out[tag_bucket].append(n)
            # 地区类词要落成 region，而不是只加几个没用的 hashtag
            rk = _REGION_INTENT.get(key)
            if rk:
                if bucket == "preferWords":
                    # "要亚洲的" → 只要亚洲
                    out["region"] = rk
                elif rk == "western":
                    # "不要欧美" → 排除欧美 = 只要亚洲
                    out["region"] = "asia"
            n = _norm_token(token)
            if n and n not in out[bucket]:
                out[bucket].append(n)
            tail_rest = tail[consumed:]
            rest = (rest[:idx] + tail_rest).strip()
            changed = True
            break

    if changed:
        rest = re.sub(r"^[的\s,，。、]+", "", rest).strip()
        # 清掉残留的连接词（"不要ai 的 cosplay" -> "cosplay"）
        rest = re.sub(r"[的\s]+$", "", rest).strip()
        if rest:
            out["keyword"] = rest
        # 若清完是空的，说明用户只说了意图没说内容，保留原词不报错
    return out


# ── 图片内联 ────────────────────────────────────────────────────────────────
#
# 为什么要把图下载下来转 base64：
#   传 URL 时是**协议端**去下载图片，不是本进程。而协议端（SnowLuma）在墙内
#   没有代理，拉不到 pbs.twimg.com —— 实测返回 `{"status":"failed",
#   "wording":"fetch failed"}`，用户看到的是"卡片发出来了但没图"。
#   改由本进程（已配代理）下载再内联，就与协议端网络无关了。
#
# 代价：体积变大（base64 膨胀约 4/3）。所以有硬上限：
#   · 单图默认最大 3MB
#   · 每条推文最多内联 3 张（再多意义不大，且会把卡片撑爆）
#   · 总量默认最大 12MB，超了就退回传 URL（让协议端尽力而为）

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGES_PER_TWEET = 3
MAX_TOTAL_INLINE_BYTES = 12 * 1024 * 1024


async def _download_as_data_uri(client, url: str, budget: dict) -> tuple[str, str]:
    """下载一张图并转成 data URI。

    返回 (data_uri_or_empty, note)。失败时返回空串，调用方退回用原 URL。
    budget 是可变 dict，累计已用字节数，跨图共享（用于总上限）。
    """
    try:
        rep = await client.get(url, timeout=20)
        if rep.status_code != 200:
            return "", f"HTTP {rep.status_code}"
        data = rep.content
        if len(data) > MAX_IMAGE_BYTES:
            return "", f"太大 {len(data)}B"
        if budget["used"] + len(data) > MAX_TOTAL_INLINE_BYTES:
            return "", "总量超限"
        # 用响应头判断类型；拿不到就按 jpg 猜（Twitter 基本是 jpg/png）
        ctype = (rep.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        budget["used"] += len(data)
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{ctype};base64,{b64}", ""
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {str(exc)[:60]}"


async def inline_images(items: list, proxy: str, progress) -> dict:
    """把 items 里的图片 URL 换成内联 data URI。

    同时处理**视频缩略图**（videoDetails[].thumbnail）—— 理由同上：
    缩略图也要由本进程下载，否则协议端拉不到 Twitter CDN 时卡片里就是裂图。

    原地修改 items（给每项加 `imageData` 字段，保留原 `images` 供退回；
    给 videoDetails 每项加 `thumbData`）。
    返回统计 {inlined, failed, bytes}。
    """
    if not items:
        return {"inlined": 0, "failed": 0, "bytes": 0}

    try:
        import httpx
    except ImportError:
        return {"inlined": 0, "failed": 0, "bytes": 0}

    budget = {"used": 0}
    stat = {"inlined": 0, "failed": 0, "bytes": 0}

    # trust_env=False + 显式 proxy：避免 NO_PROXY 里的畸形 [::1] 把代理搞掉
    kwargs = {"timeout": 25, "follow_redirects": True, "trust_env": False}
    if proxy:
        kwargs["proxy"] = proxy

    total = sum(min(len(it.get("images") or []), MAX_IMAGES_PER_TWEET) for it in items)
    total += sum(1 for it in items for vd in (it.get("videoDetails") or [])[:MAX_IMAGES_PER_TWEET])
    done = 0

    async with httpx.AsyncClient(**kwargs) as client:
        for it in items:
            # ── 图片 ────────────────────────────────────────────────────
            urls = (it.get("images") or [])[:MAX_IMAGES_PER_TWEET]
            inlined = []
            for url in urls:
                uri, note = await _download_as_data_uri(client, url, budget)
                if uri:
                    inlined.append(uri)
                    stat["inlined"] += 1
                else:
                    # 失败就保留原 URL：协议端能拉就还能显示，拉不到也不至于少一条
                    inlined.append(url)
                    stat["failed"] += 1
                    progress("inline", done, total, f"下载失败（{note}）：{url[:60]}")
                done += 1
                progress("inline", done, total, "")
            # 超过上限的也保留（用 URL，不内联）
            for url in (it.get("images") or [])[MAX_IMAGES_PER_TWEET:]:
                inlined.append(url)
            it["imageData"] = inlined

            # ── 视频缩略图 ──────────────────────────────────────────────
            for vd in (it.get("videoDetails") or [])[:MAX_IMAGES_PER_TWEET]:
                thumb = str(vd.get("thumbnail") or "")
                if not thumb:
                    continue
                uri, note = await _download_as_data_uri(client, thumb, budget)
                if uri:
                    vd["thumbData"] = uri
                    stat["inlined"] += 1
                else:
                    vd["thumbData"] = thumb   # 退回 URL
                    stat["failed"] += 1
                    progress("inline", done, total, f"缩略图失败（{note}）")
                done += 1

    stat["bytes"] = budget["used"]
    return stat

# ── tag 组合策略 ────────────────────────────────────────────────────────────
#
# 用户提到的问题："多个 tag 同时搜索结果会很少"。
# 这不是 bug，是 X 的布尔语义决定的：
#     A (B OR C) filter:media     → 必须含 A，且含 B 或 C
#     A B OR C filter:media       → 会被解析成 (A AND B) OR C，跟直觉不同
# 所以 tag 越多，AND 面越窄，结果越少。想要"多"就得拆成多轮：
#
#   any  （默认）: 每个 tag 单独搜一轮，结果合并去重
#                  → 结果最多，每个 tag 都能命中；代价是请求数 = tag 数
#   or          : 全部 tag 塞进一个 OR 组，一轮搜完
#                  → 请求最少，但结果量介于两者之间（实测容易互相稀释）
#   and        : 必须同时含所有 tag
#                  → 最精准也最少，通常只适合 2-3 个强相关 tag
#
# 这三档是"用多少结果换多少精度"的旋钮，交给用户按需选。

TAG_MODES = ("any", "or", "and")


def build_query_variants(keyword: str, tags: list, tag_mode: str, min_likes: int,
                         only_video: bool = False) -> list:
    """按 tag_mode 生成**一组**搜索式（可能多轮）。

    返回 [query, ...]，调用方按顺序跑并在结果上合并去重。

    @param tags 已经筛好的 tag 列表（不含前导 #，由 _norm_tag 统一处理）
    @param tag_mode any | or | and
    """
    kw = (keyword or "").strip()
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    mode = tag_mode if tag_mode in TAG_MODES else "any"

    suffix = ["filter:media"]
    if only_video:
        suffix.append("filter:native_video")
    if min_likes and min_likes > 0:
        suffix.append(f"min_faves:{int(min_likes)}")
    tail = " ".join(suffix)

    def norm(t: str) -> str:
        """裸词按 hashtag 处理更精准：#nsfw 比 nsfw 命中率高得多。
        已经是 #wata 这种形式就原样保留。"""
        t = t.strip()
        if not t:
            return ""
        if t.startswith("#"):
            return t
        # 含空格/中文日文的当普通关键词（写成 hashtag 反而搜不到）
        if re.search(r"[\s\u3040-\u30ff\u4e00-\u9fff]", t):
            return t
        return f"#{t}"

    normed = [x for x in (norm(t) for t in tags) if x]
    if not normed:
        return [f"{kw} {tail}".strip()]

    if mode == "and":
        # 全部 AND：一轮
        return [f"{kw} {' '.join(normed)} {tail}".strip()]
    if mode == "or":
        # 全部 OR：一轮
        return [f"{kw} ({' OR '.join(normed)}) {tail}".strip()]
    # any：每个 tag 一轮（结果最多）
    return [f"{kw} {t} {tail}".strip() for t in normed]


def build_query(keyword: str, extra_tags: list, expand: bool, min_likes: int,
                only_video: bool = False) -> str:
    """拼**单条** X 高级搜索式（兼容旧调用点 / 测试用）。

    形如：
        cosplay (nsfw OR 18+ OR r18) filter:media min_faves:50

    filter:media 是关键：让 X 只返回**带媒体**的推，
    省掉大量"有关键词但没图"的结果，也显著降低请求量。
    """
    kw = (keyword or "").strip()
    parts = [kw]
    tags = [str(t).strip() for t in (extra_tags or []) if str(t).strip()]
    if expand and tags:
        ors = " OR ".join(tags)
        parts.append(f"({ors})")
    parts.append("filter:media")
    if only_video:
        parts.append("filter:native_video")
    if min_likes and min_likes > 0:
        parts.append(f"min_faves:{int(min_likes)}")
    return " ".join(p for p in parts if p)


# ── 账号池登录 ──────────────────────────────────────────────────────────────

# ── 视频下载 ────────────────────────────────────────────────────────────────
#
# 为什么要下载：只给 mp4 链接时，群友得点出去才看得到，体验差；
# 而且协议端拉不到 video.twimg.com（和图片同一个问题）。
# 下载到本地后以 video 段发送，SnowLuma 走"上传本地文件"路径，
# 不依赖协议端网络，也省掉一次外部跳转。
#
# 约束（SnowLuma 侧 MAX_VIDEO_SIZE = 1536MB，但 QQ 实际能传的远小于此）：
#   · 单文件上限 40MB（超过就退回给链接，避免传半天失败）
#   · 单次最多下载 4 个（再多会很慢）
#   · 存在 data/x-media-finder/videos/，按小时清理旧文件

MAX_VIDEO_BYTES = 40 * 1024 * 1024
MAX_VIDEO_DOWNLOADS = 4
VIDEO_KEEP_HOURS = 12


def _video_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "videos")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _cleanup_videos(keep_hours: int = VIDEO_KEEP_HOURS) -> None:
    """删掉过期的本地视频，避免磁盘无限增长。"""
    d = _video_dir()
    cutoff = time.time() - keep_hours * 3600
    try:
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.unlink(fp)
            except Exception:
                pass
    except Exception:
        pass


async def download_videos(items: list, proxy: str, progress) -> dict:
    """把视频下到本地，给 videoDetails 加 localPath。

    失败/超限的不写 localPath，发送时会自动退回文本链接。
    返回 {downloaded, skipped, bytes}。
    """
    stat = {"downloaded": 0, "skipped": 0, "bytes": 0}
    if not items:
        return stat
    try:
        import httpx
    except ImportError:
        return stat

    _cleanup_videos()

    budget = {"used": 0}
    kwargs = {"timeout": 60, "follow_redirects": True, "trust_env": False}
    if proxy:
        kwargs["proxy"] = proxy

    tried = 0
    async with httpx.AsyncClient(**kwargs) as client:
        for it in items:
            for vd in (it.get("videoDetails") or []):
                if tried >= MAX_VIDEO_DOWNLOADS:
                    break
                url = str(vd.get("url") or "")
                if not url.startswith("http"):
                    continue
                tried += 1
                name = "%s_%s.mp4" % (re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("id") or "x"))[:40],
                                      stat["downloaded"] + stat["skipped"])
                dest = os.path.join(_video_dir(), name)
                try:
                    size = 0
                    too_big = False
                    truncated = True
                    async with client.stream("GET", url) as rep:
                        if rep.status_code != 200:
                            stat["skipped"] += 1
                            progress("video", tried, max(tried, 1), "下载失败 HTTP %d" % rep.status_code)
                            continue
                        with open(dest, "wb") as f:
                            async for chunk in rep.aiter_bytes(65536):
                                size += len(chunk)
                                if size > MAX_VIDEO_BYTES:
                                    too_big = True
                                    break
                                f.write(chunk)
                    if too_big:
                        try:
                            os.unlink(dest)
                        except Exception:
                            pass
                        stat["skipped"] += 1
                        progress("video", tried, max(tried, 1), "视频超过 40MB，改给链接")
                        continue
                    if size < 1024:
                        try:
                            os.unlink(dest)
                        except Exception:
                            pass
                        stat["skipped"] += 1
                        continue
                    vd["localPath"] = dest
                    vd["bytes"] = size
                    stat["downloaded"] += 1
                    stat["bytes"] += size
                    budget["used"] += size
                    progress("video", tried, max(tried, 1),
                             "已下载 %.1fMB" % (size / 1048576.0))
                except Exception as exc:
                    stat["skipped"] += 1
                    progress("video", tried, max(tried, 1),
                             "下载异常 %s" % type(exc).__name__)
                    try:
                        if os.path.exists(dest):
                            os.unlink(dest)
                    except Exception:
                        pass
    progress("video", 1, 1,
             "视频：下载 %d 个、跳过 %d 个、共 %.1fMB"
             % (stat["downloaded"], stat["skipped"], stat["bytes"] / 1048576.0))
    return stat


# ── 按博主检索 ──────────────────────────────────────────────────────────────
#
# 对应"看看 xxx 的新推文"、"想看 xxx 的自慰视频"这类说法。
#
# 为什么要跟关键词搜索分开：
#   关键词搜索搜的是**推文文本**，只能搜到"提到 xxx"的推文（别人转发、
#   评论区讨论）；而用户要的是"xxx 这个人**发**的作品"。
#   正确做法是 user_by_login(用户名) -> uid -> user_media(uid) 拉他的媒体时间线。

ADULT_HINT_WORDS = {
    "自慰": {"masturbat", "自慰", "オナニー", "オナ"},
    "裸": {"nude", "naked", "裸", "ヌード"},
    "啪啪": {"sex", "fuck", "セックス", "エッチ"},
}


def parse_author_request(keyword: str):
    """从用户输入里拆出"博主名"和"内容诉求"。

    「看看 xxx 的新推文」   -> ("xxx", "的新推文")
    「想看 xxx 的自慰视频」 -> ("xxx", "的自慰视频")
    「看看 @xxx 的新作品」  -> ("xxx", "的新作品")
    返回 (author, want)。

    ⚠️ 中文句子里没有空格，所以不能靠"取第一个空格前的词"来定博主名 ——
    那样 "看看 alice 的新推文" 会解析出 "看看"。正确做法是**先剥掉开头的
    动词/语气词**，再在剩余部分里找结尾的诉求短语。
    """
    kw = (keyword or "").strip()
    if not kw:
        return "", ""

    # 1) 剥掉开头的看/找/要类动词（中英文都处理）
    body = re.sub(
        r"^(?:我想|想|要|帮我看|看看|看下|看一下|找找|找一下|找|搜|搜一下|l(?:o+)?k+|check|show)\s*",
        "", kw, flags=re.IGNORECASE).strip()
    # 英文 "look at alice" / "check alice"
    body = re.sub(r"^(?:at|for)\s+", "", body, flags=re.IGNORECASE).strip()
    if not body:
        body = kw

    # 2) 剥掉结尾的"内容诉求"短语，剩下的是博主名
    tails = [
        "的自慰视频", "的裸照", "的裸视频", "的新推文", "的新作品", "的新图",
        "的新视频", "最近的推文", "最近发的", "最新的", "的作品", "的推文",
        "的视频", "的照片", "的照片", "的自拍", "的图", "新推文",
    ]
    author = body
    want = ""
    for t in tails:
        i = author.rfind(t)
        if i > 0:                       # 必须前面还有内容，否则整句都是诉求
            want = author[i:]
            author = author[:i].strip()
            break

    # 3) 清理：去 @、引号、首尾标点
    author = author.strip().strip('@"\'')
    author = re.sub(r"^@+", "", author).strip()
    author = author.strip("，,。.、!！?？ ")
    # X 用户名不含空格；若仍有空格，取最后一段（"看看 alice" -> "alice"）
    if " " in author:
        author = author.split()[-1]

    # 4) 剩下的如果还是个动词短语，说明没识别出博主
    if author in ("看看", "想看", "想", "要", "看", "找", "搜"):
        return "", ""

    return author, want


def pick_author_media(items: list, want: str) -> list:
    """按内容诉求排序（不是过滤）：命中诉求词的博主作品排前面。"""
    want = (want or "").strip()
    if not want:
        return items
    words = set()
    for key, syns in ADULT_HINT_WORDS.items():
        if key in want:
            words |= syns
    for w in re.split(r"[\s,、]+", want):
        w = w.strip()
        if len(w) >= 2 and w not in ("视频", "新推文", "作品", "新作品", "最新", "的新", "照片"):
            words.add(w)
    if not words:
        return items

    def hit(it):
        blob = (str(it.get("text", "")) + " " + str(it.get("url", ""))).lower()
        return sum(1 for w in words if w.lower() in blob)

    return sorted(items, key=lambda it: (hit(it), it.get("_score", 0.0)), reverse=True)


async def run_author_search(task: dict, api, count: int, progress) -> dict:
    """按博主拉取其媒体作品，返回与 run_search 相同形状的结果字典。"""
    from twscrape import gather

    keyword = str(task.get("keyword") or "").strip()
    author, want = parse_author_request(keyword)
    if not author:
        raise WorkerError("没能从「%s」里认出博主名" % keyword, code="BAD_AUTHOR")

    mode = str(task.get("mediaType") or "mix").strip().lower()
    max_results = max(1, int(task.get("maxResults") or 12))
    progress("author", 0, 3, "查找博主 @%s" % author)

    user = await api.user_by_login(author)
    if user is None:
        raise WorkerError(
            "找不到博主 @%s。确认用户名拼写（不带 @），或该账号已注销/改名。" % author,
            code="AUTHOR_NOT_FOUND"
        )
    uid = int(getattr(user, "id", 0) or 0)
    screen = str(getattr(user, "username", author) or author)
    progress("author", 1, 3, "找到 @%s（id=%s），拉取媒体时间线…" % (screen, uid))

    want_n = max(max_results * 4, 40)
    last_err = None
    tweets = []
    retries = max(1, int(task.get("retries") or 3))
    backoff = max(1, int(task.get("backoffSeconds") or 8))
    for attempt in range(retries):
        try:
            tweets = await gather(api.user_media(uid, limit=want_n))
            last_err = None
            break
        except Exception as err:
            last_err = err
            if attempt + 1 >= retries:
                break
            delay = backoff * (2 ** attempt)
            progress("author", 1, 3, "拉取失败，%ss 后重试（%s）" % (delay, type(err).__name__))
            await asyncio.sleep(delay)
    if last_err is not None and not tweets:
        code = "RATE_LIMIT" if is_rate_limit(last_err) else "AUTHOR_FETCH_FAILED"
        raise WorkerError(
            "拉取 @%s 的作品失败：%s: %s" % (screen, type(last_err).__name__, last_err),
            code=code
        )

    progress("author", 2, 3, "拿到 %d 条带媒体推文，筛选中…" % len(tweets))

    collected = []
    seen = set()
    for tw in tweets:
        try:
            it = tweet_to_item(tw)
        except Exception:
            continue
        if not it:
            continue
        if mode == "image" and not it["images"]:
            continue
        if mode == "video" and not it["videos"]:
            continue
        key = it["id"] or (it["images"][0] if it["images"] else "")
        if key in seen:
            continue
        seen.add(key)
        collected.append(it)

    boost = task.get("qualityBoost", True)
    for it in collected:
        it["_score"] = quality_score(it) if boost else 0.0

    collected = pick_author_media(collected, want)

    imgs = [x for x in collected if x["images"]]
    vids = [x for x in collected if x["videos"]]
    if mode == "image":
        kept = imgs[:max_results]
    elif mode == "video":
        kept = vids[:max_results]
    else:
        half = max(1, max_results // 2)
        pv = vids[:half]
        pi = imgs[:max(1, max_results - len(pv))]
        kept = pick_images_first_balanced(pi, pv, max_results)

    for it in kept:
        it.pop("_tweet", None)
        it.pop("_score", None)

    inline_stat = {"inlined": 0, "failed": 0, "bytes": 0}
    if task.get("inlineImages", True) and kept:
        inline_stat = await inline_images(kept, str(task.get("proxy") or "").strip(), progress)

    progress("author", 3, 3, "@%s：%d 条作品" % (screen, len(kept)))
    return {
        "ok": True,
        "items": kept,
        "query": "user:%s" % screen,
        "kept": len(kept),
        "accounts": count,
        "mode": mode,
        "author": screen,
        "want": want,
        "pool": {"images": len(imgs), "videos": len(vids)},
        "inline": inline_stat,
    }


AUTHOR_VERBS = (
    "看看", "想看", "看一下", "看下", "找找", "找一下", "来点", "整点",
    "look", "check", "show",
)


def looks_like_author_request(keyword: str) -> bool:
    """判断用户是不是在问"某个博主的作品"。

    实测教训：只靠结尾关键词（"的新推文"、"的视频"）不够 ——
    「想看 mikufu0809」这种没有尾巴的说法会被漏掉，结果跑到关键词搜索，
    搜一个用户名当然搜不到东西（0 结果）。

    所以改成看两件事：
      1. 有没有"看某人"的动词开头（看看/想看/look...）
      2. 剥掉动词和诉求后，剩下的很像一个 X 用户名（无空格、长度合理）

    仍然保守：纯中文关键词（"猫"、"自拍"）不会被判成博主，
    因为它们的字符集不像用户名（X 用户名只有字母数字下划线）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return False

    has_verb = any(kw.lower().startswith(v) for v in AUTHOR_VERBS)
    # 结尾诉求也算强信号
    tail_hints = ("的新推文", "的新作品", "的新图", "的新视频", "的视频",
                  "的作品", "的推文", "的照片", "的自拍", "最近发的", "最新的")
    has_tail = any(t in kw for t in tail_hints)

    author, _ = parse_author_request(kw)
    if not author:
        return False
    # X 用户名：字母数字下划线，1-15 位
    looks_like_username = bool(re.fullmatch(r"[A-Za-z0-9_]{1,15}", author))

    # 判定：有动词或诉求 + 剩下的像用户名
    if (has_verb or has_tail) and looks_like_username:
        return True
    return False


def build_search_plan(keyword: str, tags: list, tag_mode: str, min_likes: int,
                      only_video: bool, extra_keywords: list = None) -> list:
    """生成搜索轮次：**tag 轮 -> 纯关键词轮**。

    两者都保留而不是二选一：
      · tag 轮 precision 高（实测 nsfw 的敏感占比 88%）
      · 纯关键词轮 recall 高（不受 tag 限制，能捞到"没打 tag 但就是你要的"）
    只靠 tag 会明显偏少，这正是"搜索结果少"的原因之一。
    """
    plan = []
    # 地区扩展：同一题材的日文/中文写法各走一遍 tag 轮 + 纯关键词轮。
    # 外层是词、内层是 tag —— 换语言后 tag 依然有用，
    # 且同一个词的轮次连续发出，X 端不容易判成异常频率。
    words = [keyword] + [w for w in (extra_keywords or []) if w and w != keyword]
    for w in words:
        if tags:
            plan.extend(build_query_variants(w, tags, tag_mode, min_likes, only_video))
        plan.append(build_query(w, [], False, min_likes, only_video))
    return plan


async def open_pool(accounts_db: str, progress, proxy: str = ""):
    """打开 twscrape 账号池。

    账号由 **twscrape 自己的 CLI** 登录并写入 sqlite（见 README 的登录步骤），
    本进程只读不写：不替用户存密码、不碰 cookie。

    返回 (api, account_count)。账号数为 0 时抛 RuntimeError —— 这是最常见的
    首次部署失败原因（库是空的），必须给出可操作的错误而不是空结果。

    ⚠️ 账号数必须用 `await api.pool.get_all()` 取。
        twscrape 0.20.x 的 AccountsPool **没有** `.accounts` 属性，
        早期实现按老版本猜属性名，结果在 0.20 上永远数出 0 个账号，
        于是一个配好的环境被误报成"账号库为空"。
    """
    from twscrape import API  # 延迟导入：--check 模式下能给出更清楚的报错

    db = os.path.abspath(accounts_db)
    if not os.path.exists(db):
        raise WorkerError(
            f"账号库不存在：{db}。请先按 README 用 twscrape CLI 登录一个 X 账号"
            f"（twscrape add_accounts ... && twscrape login_accounts）",
            code="NO_ACCOUNTS_DB"
        )

    # proxy 直接传给 API（0.20.x 原生支持），比设环境变量可靠：
    # 环境变量会被子进程里的其它库读到，也不便于多账号不同代理。
    #
    # wait_timeout：账号全被锁/限频时，twscrape 会**阻塞等待**这么久才放弃。
    # 设太大（原本 60s）会让"账号冷却"这种可预期情况把用户拖到整体超时，
    # 报出来的还是一句"搜索超时"，完全看不出真因。
    # 20s 足够覆盖"上一个请求刚用完、马上释放"的正常竞争，又不会磨掉耐心。
    api = API(db, wait_timeout=20, proxy=(proxy or None))
    progress("login", 0, 1, f"账号库：{db}")
    # 把实际生效的代理报出来：这一条是排查"搜索 0 结果/超时"的第一现场。
    # 早期没有这行，结果"worker 单跑能搜到、在机器人里搜不到"时无从下手。
    progress("login", 0, 1, f"代理：{proxy or '直连（未配置）'}")

    accounts = []
    try:
        accounts = await api.pool.get_all()
    except Exception as exc:  # noqa: BLE001
        progress("login", 1, 1, f"读取账号列表失败：{type(exc).__name__}: {exc}")

    count = len(accounts or [])
    # 顺便报一下可用（active）账号数——被限频/被封的账号也算在 total 里，
    # 只报 total 会让人以为"有号就能搜"。
    active = 0
    for a in accounts or []:
        state = _get(a, "active", default=True)
        # twscrape 的 Account.active 是 bool；SQLite 里也可能存成 0/1 或字符串
        if isinstance(state, str):
            is_active = state.strip().lower() not in ("false", "0", "no", "")
        else:
            is_active = bool(state)
        if is_active:
            active += 1
    progress("login", 1, 1, f"账号数：{count}（其中可用约 {active}）")
    if count == 0:
        raise WorkerError(
            "账号库里没有账号。请先用 twscrape CLI 登录（README「登 X 账号」一节），"
            "并确认账号已开启「显示敏感内容」。",
            code="NO_ACCOUNTS"
        )
    return api, count


# ── 主搜索 ──────────────────────────────────────────────────────────────────

RATE_LIMIT_HINTS = (
    "rate limit", "ratelimit", "too many requests", "429",
    "rate_limit_exceeded", "slow down",
    # ⚠️ 2026-10-25 实测：twscrape 限流时抛的是这句，**不含上面任何一个词**：
    #   No account available for queue "SearchTimeline". Next available at 19:58:54
    # 不补这几个特征，限流会被当成普通失败，上层就说"没货"，模型还会自己编
    # "卡片发不出去"。实测单账号搜了 1535 次后就是这个状态。
    "no account available", "next available at",
    "queue", "unable to search", "temporarily blocked",
    "account is locked", "login required", "unauthorized", "401", "403",
)


def is_rate_limit(err: Exception) -> bool:
    """判断异常是不是"被限频"（需要退避，而不是重试同一个请求）。"""
    msg = f"{type(err).__name__} {err}".lower()
    return any(h in msg for h in RATE_LIMIT_HINTS)


async def run_search(task: dict) -> dict:
    """执行一轮完整搜索：扩展词优先，结果太少则回退到纯关键词。

    返回 {ok, items, query, raw, kept} 或抛异常（由 main 转成 result 行）。
    """
    from twscrape import gather

    keyword = str(task.get("keyword") or "").strip()
    if not keyword:
        raise ValueError("keyword 为空")

    # ── 解析"不要ai / 要亚洲的"这类口头意图 ──────────────────────────────
    # 解析器只此一份（JS 侧不重复实现），解析出的屏蔽/偏好词与配置里的
    # 走同一条 apply_custom_filters 代码路径。
    intent = parse_custom_intent(keyword)
    if (intent["blockTags"] or intent["blockWords"]
            or intent["preferTags"] or intent["preferWords"]):
        progress_log = (
            "口头意图：屏蔽标签%d个/词%s，偏好标签%d个/词%s；实际搜索词「%s」"
            % (len(intent["blockTags"]), intent["blockWords"],
               len(intent["preferTags"]), intent["preferWords"], intent["keyword"]))
        # progress 还没定义，先攒着，定义后立刻打
    else:
        progress_log = ""
    if intent["keyword"]:
        keyword = intent["keyword"]

    extra_tags = task.get("extraTags") or []
    expand = bool(task.get("expandTags", True))
    # tag 组合策略：any（每个 tag 单独搜，结果最多）/ or（一轮 OR）/ and（必须都有）
    tag_mode = str(task.get("tagMode") or "any").strip().lower()
    if tag_mode not in TAG_MODES:
        tag_mode = "any"
    max_results = max(1, int(task.get("maxResults") or 12))
    min_likes = int(task.get("minLikes") or 0)
    retries = max(1, int(task.get("retries") or 3))
    backoff = max(1, int(task.get("backoffSeconds") or 8))

    # 媒体类型：image 只要图 / video 只要视频 / mix 各一半
    mode = str(task.get("mediaType") or "mix").strip().lower()
    if mode not in ("image", "video", "mix"):
        mode = "mix"
    # video 模式可以额外加 filter:native_video 让 X 端先筛一遍；
    # 不加也行（本地还会过滤），加了能显著减少无用请求。
    video_only_filter = bool(task.get("videoOnlyQuery", True)) and mode == "video"

    def progress(stage, done, total, note=""):
        emit({"t": "progress", "stage": stage, "done": done, "total": total, "note": note})

    if progress_log:
        progress("intent", 0, 1, progress_log)

    api, count = await open_pool(
        str(task.get("accountsDb") or "accounts.db"),
        progress,
        str(task.get("proxy") or "")
    )

    # ── 分流：按博主 / 按关键词 ───────────────────────────────────────────
    # searchMode:
    #   auto   = 自动判断（默认）。看起来像"看某人的作品"就走博主路径
    #   author = 强制按博主
    #   search = 强制按关键词搜索
    want_mode = str(task.get("searchMode") or "auto").strip().lower()
    if want_mode == "auto":
        looks_author = looks_like_author_request(keyword)
    else:
        looks_author = (want_mode == "author")

    if looks_author:
        progress("dispatch", 0, 1, "识别为「按博主检索」")
        try:
            return await run_author_search(task, api, count, progress)
        except WorkerError as exc:
            # 找不到这个博主时**回退到关键词搜索**，而不是直接报错。
            # 原因：用户名和普通英文词会撞车（"看看 cosplay" 里 cosplay
            # 恰好像合法用户名）。硬报"找不到博主 cosplay"很蠢，
            # 用户明明是在问内容。
            if exc.code in ("AUTHOR_NOT_FOUND", "BAD_AUTHOR"):
                progress("dispatch", 0, 1, "没找到这个博主，改按关键词搜索")
            else:
                raise

    progress("dispatch", 0, 1, "按关键词 + tag 检索")

    # 两轮尝试：先带扩展 tag，不够再退回纯关键词。
    # 为什么需要回退：加了 tag 的 OR 组会让 X 的匹配面变窄（尤其是中文词配日文 tag），
    # 有时反而一条都搜不到，而纯关键词是有结果的。
    # 轮次顺序：tag 轮 -> 纯关键词轮。
    # 纯关键词轮是**独立**的一轮（不是兜底）：只靠 tag 会漏掉"没打 tag 但就是你要的"内容，
    # 这正是搜索结果偏少的原因之一。
    # 地区意图（口头优先于配置）。必须在搜索之前定义 ——
    # 它既用于扩展搜索词，也用于后面过滤结果。
    _region_hint = str(intent.get("region") or task.get("region") or "any").strip().lower()
    _extra_kw = expand_keywords_by_region(keyword, _region_hint)
    if _extra_kw:
        progress("intent", 0, 1,
                 "地区扩展：额外用 %s 搜索（原词 %s）" % ("、".join(_extra_kw), keyword))

    attempts = build_search_plan(
        keyword,
        list(extra_tags) if (expand and extra_tags) else [],
        tag_mode, min_likes, video_only_filter,
        # 地区扩展：用英文词只能搜到英文内容，必须补日文/中文写法
        expand_keywords_by_region(keyword, _region_hint))

    collected = []
    seen_ids = set()
    used_query = attempts[0]

    for idx, query in enumerate(attempts, start=1):
        progress("search", idx - 1, len(attempts), f"第{idx}轮：{query}")
        # 多取一些原始结果，用于过滤/排序后仍能凑够 max_results（垃圾率常在半数以上）
        # any 模式下每个 tag 只搜一轮，单轮要的少些（总量靠多轮堆），
        # 否则 N 个 tag × 大 limit 会把额度烧光。
        per_round = max(max_results, 15) if tag_mode == "any" else max(max_results * 4, 20)
        want = per_round
        last_err = None
        for attempt in range(retries):
            try:
                tweets = await gather(api.search(query, limit=want))
                last_err = None
                break
            except Exception as err:  # noqa: BLE001 —— 限频/网络/登录态都在这里
                last_err = err
                if attempt + 1 >= retries:
                    tweets = []
                    break
                delay = backoff * (2 ** attempt)
                note = "限频退避" if is_rate_limit(err) else "失败重试"
                progress("search", idx - 1, len(attempts),
                         f"{note} {delay}s（{type(err).__name__}: {str(err)[:120]}）")
                await asyncio.sleep(delay)

        if last_err is not None and not tweets:
            # 所有重试都失败：如果还有下一轮 query 就继续，否则把错误抛出去
            if idx >= len(attempts):
                if is_rate_limit(last_err):
                    raise WorkerError(
                        f"X 限频（已退避重试 {retries} 次仍失败）。"
                        f"账号池规模：{count} —— 多登录几个账号，或过 15 分钟再试。"
                        f"（这不是卡片问题，是推特那边暂时不给搜了）",
                        code="RATE_LIMIT"
                    )
                raise WorkerError(
                    f"搜索失败：{type(last_err).__name__}: {last_err}",
                    code="SEARCH_FAILED"
                )

        for tw in tweets:
            try:
                item = tweet_to_item(tw)
            except Exception:  # noqa: BLE001 —— 单条解析失败不影响整轮
                continue
            if not item:
                continue
            # 媒体类型硬过滤（用户显式要求的，不算"凭空删结果"）
            if mode == "image" and not item["images"]:
                continue
            if mode == "video" and not item["videos"]:
                continue
            key = item["id"] or (item["author"] + "|" + (item["images"][0] if item["images"] else ""))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            collected.append(item)

        used_query = query
        # 判断"够不够"时按类型算：mix 模式要求图片和视频都尽量有。
        # 早期只数总条数，导致明明要"一半视频"，但前 12 条全是图就提前收工。
        if len(collected) >= want_enough(collected, max_results, mode):
            break
        progress("search", idx, len(attempts), f"本轮累计 {len(collected)} 条")

    # ── 排序 + 按类型平衡 ─────────────────────────────────────────────────
    # 先把全部候选按相关性排序，再按 mode 挑出最终 N 条。
    # 排序在**截断之前**做：早期是边收边截，等于按 X 的返回顺序取前 N 条，
    # 质量高低全看运气 —— 这正是"拉取到的内容质量低"的直接原因。
    #
    # qualityBoost=False 时退化为"按 X 返回顺序"（保留开关是为了
    # 万一排序把用户想要的东西压下去时能快速关掉，不用改代码）。
    if task.get("qualityBoost", True):
        for it in collected:
            it["_score"] = quality_score(it)
    else:
        for it in collected:
            it["_score"] = 0.0

    # 自定义屏蔽 / 偏好（放在分池之前，两个池共享同一次过滤）
    def _merge(*lists):
        out = []
        for lst in lists:
            for x in (lst or []):
                n = _norm_token(x)
                if n and n not in out:
                    out.append(n)
        return out

    block_tags = _merge(task.get("blockTags"), intent["blockTags"])
    block_words = _merge(task.get("blockWords"), intent["blockWords"])
    prefer_tags = _merge(task.get("preferTags"), intent["preferTags"])
    prefer_words = _merge(task.get("preferWords"), intent["preferWords"])
    # blockAi：把整组 AI 标签一次性加入屏蔽（常用开关，省得手抄一长串）
    if task.get("blockAi", False):
        # 标签和词形都要灌：标签靠 hashtag 精确命中，词形兜住正文写法
        for t in DEFAULT_AI_TAGS:
            n = _norm_token(t)
            if n and n not in block_tags:
                block_tags.append(n)
        for wd in DEFAULT_AI_WORDS:
            n = _norm_token(wd)
            if n and n not in block_words:
                block_words.append(n)

    custom_blocked = 0
    if block_tags or block_words or prefer_tags or prefer_words:
        collected, custom_blocked, _reasons = apply_custom_filters(
            collected, block_tags, block_words, prefer_tags, prefer_words)
        progress("filter", 0, 1,
                 "自定义过滤：屏蔽 %d 条，剩 %d 条" % (custom_blocked, len(collected)))

    # ── 地区关键词扩展 ────────────────────────────────────────────────
    # 用英文词搜只能得到英文内容（实测 19/20 条 latin），所以要先扩展关键词。
    # 注意这会**改变搜索词**，不只是过滤结果 —— 这是必要的：
    # 过滤只能删掉不合适的，扩展才能真正找到亚洲内容。
    # ── 地区/语言过滤（"不要欧美，只要亚洲"）──────────────────────────
    # 放在相关性过滤之前：先把明显语言不对的去掉，再做相关性匹配，
    # 免得英文垃圾内容干扰共现标签的提取。
    region_dropped = 0
    # 口头意图优先于配置（群友当场说"只要日本的"就该当场生效）
    region_used = _region_hint
    if region_used != "any" and collected:
        collected, region_dropped = filter_region(collected, region_used)
        progress("filter", 0, 1,
                 "地区过滤(%s)：去掉 %d 条，剩 %d 条"
                 % (region_used, region_dropped, len(collected)))

    # ── 相关性过滤（解决"搜 A 却出现小猫新闻"）────────────────────────
    # 放在分池之前，两个池共享同一次过滤。
    relevance_dropped = 0
    relevance_terms = []
    if task.get("relevanceFilter", True):
        # includeTerms 是模型通过工具参数传进来的别名
        # （比如"奥黛塔" -> 奥黛塔/オデット/Odette），没有就只靠关键词+共现
        collected, relevance_dropped, relevance_terms = filter_relevance(
            collected,
            keyword,
            task.get("includeTerms") or [],
            True)
        if relevance_dropped:
            progress("filter", 0, 1,
                     "相关性：滤掉 %d 条不相干内容，保留 %d 条（匹配词：%s）"
                     % (relevance_dropped, len(collected),
                        "/".join(relevance_terms[:6]) or keyword))

    # 硬过滤（adultOnly）：放在分池之前，两个池共享同一次过滤
    adult_only = bool(task.get("adultOnly", False))
    adult_strict = bool(task.get("adultStrict", False))
    adult_dropped = 0
    if adult_only or adult_strict:
        # adultStrict 隐含 adultOnly（开了严格就必须过滤）
        collected, adult_dropped = filter_adult(collected, True, adult_strict)
        progress("filter", 0, 1,
                 "只保留成人向内容：%d 条达标、%d 条被滤掉" % (len(collected), adult_dropped))

    pool_images = [x for x in collected if x["images"]]
    pool_videos = [x for x in collected if x["videos"]]
    pool_images.sort(key=lambda x: x["_score"], reverse=True)
    pool_videos.sort(key=lambda x: x["_score"], reverse=True)

    # 同作者限制：先排序再限流，保证留下的是该作者最好的几条。
    # 实测视频模式下单个引流号能占满前 4 条，图片模式下也会一个号刷 7 条。
    # 注意必须在**合并成卡片之前**做，且对两个池分别做 —— 之后 mix 分支
    # 从两个池各取一部分，不会绕过这个限制。
    per_author = int(task.get("maxPerAuthor") or 0)
    if per_author > 0:
        pool_images = limit_per_author(pool_images, per_author)
        pool_videos = limit_per_author(pool_videos, per_author)

    if mode == "image":
        kept = pool_images[:max_results]
    elif mode == "video":
        kept = pool_videos[:max_results]
    else:
        # mix：一半一半。某一边不够时，用另一边的余量补满，
        # 这样"视频只有 1 条"时不会只给 1 条图片，而是图片多给几条。
        half = max(1, max_results // 2)
        pick_v = pool_videos[:half]
        pick_i = pool_images[:max(1, max_results - len(pick_v))]
        if len(pick_i) < max_results - len(pick_v):
            # 图片不够，回头用视频补
            pick_v = pool_videos[:max_results - len(pick_i)]
        kept = pick_images_first_balanced(pick_i, pick_v, max_results)

    # 清掉内部字段，别把原始 tweet 对象塞进 JSON 输出
    for it in kept:
        it.pop("_tweet", None)
        it.pop("_score", None)

    if not kept:
        # 类型过滤后可能一条不剩（比如搜的词根本没有视频）
        return {
            "ok": True,
            "items": [],
            "query": used_query,
            "kept": 0,
            "accounts": count,
            "mode": mode,
            "pool": {"images": len(pool_images), "videos": len(pool_videos)},
            "inline": {"inlined": 0, "failed": 0, "bytes": 0},
        }

    # ── 图片内联（关键）──────────────────────────────────────────────────
    # 在返回前把图片下载成 data URI。这样协议端不需要自己访问 Twitter CDN，
    # 规避"协议端在墙内没代理 → 卡片发出来但没图"的问题。
    inline_stat = {"inlined": 0, "failed": 0, "bytes": 0}
    if task.get("inlineImages", True) and kept:
        progress("inline", 0, 0, "开始下载图片…")
        inline_stat = await inline_images(kept, str(task.get("proxy") or "").strip(), progress)
        progress("inline", 1, 1,
                 f"内联 {inline_stat['inlined']} 张、失败 {inline_stat['failed']} 张、"
                 f"{inline_stat['bytes'] // 1024}KB")

    # 视频下载（比只给链接体验好：不用点出去，也不依赖协议端网络）
    video_stat = {"downloaded": 0, "skipped": 0, "bytes": 0}
    if task.get("downloadVideos", True) and kept:
        has_video = any(it.get("videoDetails") for it in kept)
        if has_video:
            progress("video", 0, 0, "开始下载视频…")
            video_stat = await download_videos(
                kept, str(task.get("proxy") or "").strip(), progress)

    return {
        "ok": True,
        "items": kept,
        "query": used_query,
        "kept": len(kept),
        "accounts": count,
        "mode": mode,
        "video": video_stat,
        "pool": {"images": len(pool_images), "videos": len(pool_videos)},
        "adultOnly": adult_only,
        "adultStrict": adult_strict,
        "customBlocked": custom_blocked,
        "region": region_used,
        "regionDropped": region_dropped,
        "relevanceDropped": relevance_dropped,
        "relevanceTerms": relevance_terms[:8],
        "adultDropped": adult_dropped,
        "inline": inline_stat,
    }


# ── 自检 ────────────────────────────────────────────────────────────────────

def run_check() -> int:
    """能力自检：只报告环境，不联网。用于部署后第一件事。"""
    report = {"twscrape": None, "accountsDb": None, "accounts": 0, "origSample": ""}
    try:
        import twscrape  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        emit({"t": "result", "ok": False,
              "error": f"twscrape 不可用：{exc}。请 pip install -r requirements.txt"})
        return 1

    # 版本号：twscrape 模块级的 __version__ 在 0.20.x 上是 None，
    # 必须走 importlib.metadata 才算得准（早期实现报 "unknown"，看不出装没装对）。
    version = "unknown"
    try:
        import importlib.metadata as md

        version = md.version("twscrape")
    except Exception:  # noqa: BLE001
        version = getattr(twscrape, "__version__", None) or "unknown"
    report["twscrape"] = version

    # 顺便确认关键 API 存在 —— 版本对但接口变了同样跑不起来
    missing = []
    try:
        from twscrape import API, gather  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        missing.append(f"API/gather 导入失败：{exc}")
    try:
        from twscrape.accounts_pool import AccountsPool

        if not hasattr(AccountsPool, "get_all"):
            missing.append("AccountsPool 缺少 get_all()（版本过旧？需要 >=0.12）")
    except Exception as exc:  # noqa: BLE001
        missing.append(f"AccountsPool 导入失败：{exc}")
    if missing:
        report["missing"] = missing

    db = sys.argv[2] if len(sys.argv) > 2 else ""
    if db:
        path = os.path.abspath(db)
        report["accountsDb"] = path
        if not os.path.exists(path):
            emit({"t": "result", "ok": False,
                  "error": f"账号库不存在：{path}（先跑 twscrape 登录）", "report": report})
            return 1

        async def _count():
            api, n = await open_pool(path, lambda *a: None)
            return n

        try:
            report["accounts"] = asyncio.run(_count())
        except Exception as exc:  # noqa: BLE001
            emit({"t": "result", "ok": False, "error": str(exc), "report": report})
            return 1

    # 用一个纯字符串解析测试，确认 to_orig 生效（不联网）
    sample = "https://pbs.twimg.com/media/Gabc?format=jpg&name=small"
    fixed = to_orig(sample)
    report["origSample"] = fixed
    if "name=orig" not in fixed:
        emit({"t": "result", "ok": False,
              "error": "to_orig 转换异常（应输出 name=orig）", "report": report})
        return 1

    # animated_gif 提取自检（构造假对象，不联网）：确认 GIF 不会被当成无媒体
    class _Anim:
        videoUrl = "https://video.twimg.com/tweet_video/abc.mp4"

    class _Media:
        photos = []
        videos = []
        animated = [_Anim()]

    if not extract_videos(_Media()):
        emit({"t": "result", "ok": False,
              "error": "animated_gif 提取异常：GIF 动图会被当成无媒体丢掉", "report": report})
        return 1

    if missing:
        emit({"t": "result", "ok": False,
              "error": "twscrape 接口不完整：" + "；".join(missing), "report": report})
        return 1

    emit({"t": "result", "ok": True, "report": report})
    return 0


# ── 入口 ────────────────────────────────────────────────────────────────────

def main() -> int:
    if "--check" in sys.argv:
        return run_check()

    # Windows 上 asyncio 默认策略即可；显式设一遍是为了兼容 3.10 的差异
    if os.name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:  # noqa: BLE001
            pass

    raw = sys.stdin.read()
    try:
        task = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        emit({"t": "result", "ok": False, "error": f"任务 JSON 解析失败：{exc}"})
        return 1

    # 代理：twscrape 0.20.x 的 API(proxy=...) 原生支持，直接传给 open_pool。
    #
    # ⚠️ 这里踩过两个坑，都实测过：
    #   1. twscrape **不读** HTTPS_PROXY / HTTP_PROXY。它的解析顺序是
    #      `[构造参数, os.getenv("TWS_PROXY"), account.proxy]`（见 Account.resolve_proxy），
    #      所以老代码设的那几个环境变量对 twscrape 完全无效 —— 表现为日志里
    #      `proxy=False`、然后 ConnectError 连不上 x.com。
    #   2. 有些机器的 NO_PROXY 里带 `[::1]` 这种 IPv6 方括号写法，httpx 0.28 会把它
    #      当 URLPattern 解析并抛 `InvalidURL: Invalid port: ':1]'`，导致 client
    #      构造失败、**代理被静默丢弃**。实测显式传 proxy 时不受影响，
    #      但为了保险这里顺手清掉畸形的 NO_PROXY。
    proxy = str(task.get("proxy") or "").strip()
    if proxy:
        # TWS_PROXY 是 twscrape 自己认的变量，双保险（构造参数已经传了）
        os.environ["TWS_PROXY"] = proxy
        # 只在 NO_PROXY 含畸形 IPv6 写法时清理，避免无差别破坏用户的正常配置
        for key in ("NO_PROXY", "no_proxy"):
            raw = os.environ.get(key, "")
            if raw and re.search(r"\[\s*::1?\s*\]", raw):
                cleaned = ",".join(
                    part for part in raw.split(",")
                    if not re.search(r"\[\s*::1?\s*\]", part)
                )
                os.environ[key] = cleaned

    try:
        result = asyncio.run(run_search(task))
        emit({"t": "result", **result})
        return 0
    except WorkerError as exc:
        # 业务异常：带明确错误码，Node 侧据此决定回群友哪句话
        emit({
            "t": "result",
            "ok": False,
            "code": exc.code,
            "error": str(exc),
        })
        return 1
    except Exception as exc:  # noqa: BLE001
        code = "RATE_LIMIT" if is_rate_limit(exc) else "ERROR"
        emit({
            "t": "result",
            "ok": False,
            "code": code,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1200:],
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
