#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hls_curl.py — 用 curl 下 HLS 分片再拼接（绕开 ffmpeg 的 HLS 限制）

为什么需要这个：
  MissAV 的真实流在 surrit.com，分片后缀是 .jpeg（伪装成图片），
  实际内容是 MPEG-TS(h264 1080p)。
  · ffmpeg 直接读 m3u8 → "Invalid data found"（它不认 .jpeg 后缀）
  · yt-dlp / Playwright request → 403（Cloudflare）
  · curl + Referer → 200 ✅

所以流程改成：curl 取 m3u8 → curl 逐个下分片（存成 .ts）→ ffmpeg concat。
"""
import io
import os
import re
import subprocess
import sys

PROXY = xmf_paths.proxy_url()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
import xmf_paths

FFMPEG = xmf_paths.FFMPEG


def _curl_text(url, referer, timeout=60):
    cmd = ["curl", "-sL", "--proxy", PROXY, "-A", UA,
           "--max-time", str(timeout)]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout + 20)
    return (p.stdout or b"").decode("utf-8", "replace")


def _curl_file(url, dst, referer, timeout=180):
    cmd = ["curl", "-sL", "--proxy", PROXY, "-A", UA,
           "--max-time", str(timeout), "-o", dst]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout + 20)
        return os.path.exists(dst) and os.path.getsize(dst) > 2000
    except Exception:
        return False


def download_hls(m3u8_url, referer, outdir, on_progress=None, max_segments=None):
    """下载 HLS 并拼成 mp4。返回 (路径, 大小) 或 (None, 0)。"""
    os.makedirs(outdir, exist_ok=True)
    seg_dir = os.path.join(outdir, "_segs")
    os.makedirs(seg_dir, exist_ok=True)

    text = _curl_text(m3u8_url, referer, timeout=45)
    if not text or len(text) < 50:
        return None, 0

    # master playlist → 取第一个 variant
    base = m3u8_url
    if "#EXT-X-STREAM-INF" in text:
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                nxt = lines[i + 1]
                base = nxt if nxt.startswith("http") else \
                    m3u8_url.rsplit("/", 1)[0] + "/" + nxt
                text = _curl_text(base, referer, timeout=45)
                break

    segs = [x.strip() for x in text.split("\n")
            if x.strip() and not x.strip().startswith("#")]
    if not segs:
        return None, 0
    if max_segments:
        segs = segs[:max_segments]

    base_dir = base.rsplit("/", 1)[0]
    total = len(segs)
    got = []
    for i, s in enumerate(segs):
        url = s if s.startswith("http") else base_dir + "/" + s
        # 统一存成 .ts（内容是 MPEG-TS，跟后缀无关）
        dst = os.path.join(seg_dir, "seg_%05d.ts" % i)
        if os.path.exists(dst) and os.path.getsize(dst) > 2000:
            got.append(dst)
        elif _curl_file(url, dst, referer):
            got.append(dst)
        if on_progress and (i % 10 == 0 or i == total - 1):
            on_progress({"t": "progress", "stage": "video",
                         "note": "下载分片 %d/%d" % (i + 1, total)})
    if not got:
        return None, 0

    # 拼接
    listfile = os.path.join(seg_dir, "list.txt")
    with io.open(listfile, "w", encoding="utf-8") as f:
        for g in got:
            f.write("file '%s'\n" % g.replace("\\", "/"))
    out = os.path.join(outdir, "hls_%d.mp4" % int(__import__("time").time()))
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
           "-c", "copy", "-bsf:a", "aac_adtstoasc", out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception:
        return None, 0

    if os.path.exists(out) and os.path.getsize(out) > 100000:
        # 清掉分片省盘（D 盘也经不起堆）
        for g in got:
            try:
                os.unlink(g)
            except Exception:
                pass
        try:
            os.unlink(listfile)
        except Exception:
            pass
        try:
            os.rmdir(seg_dir)
        except Exception:
            pass
        return out, os.path.getsize(out)
    return None, 0


if __name__ == "__main__":
    m3u8 = sys.argv[1]
    ref = sys.argv[2] if len(sys.argv) > 2 else ""
    od = sys.argv[3] if len(sys.argv) > 3 else "."
    mx = int(sys.argv[4]) if len(sys.argv) > 4 else None
    p, sz = download_hls(m3u8, ref, od, max_segments=mx)
    print("RESULT", p, sz)
