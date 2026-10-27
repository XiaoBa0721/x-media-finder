#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preflight.py — 部署前自检

为什么需要这个脚本
    2026-10-27 出过一次事故：做跨平台路径改造时，把 `import xmf_paths`
    放在了使用它的代码后面，三个 worker（image_source_worker / av_sites /
    hls_curl）启动即 NameError。当时只跑了 `ast.parse` 做语法检查——
    语法完全合法，所以没发现，直到用户真的去识图才炸。

    语法检查只能发现「写错了」，发现不了「跑起来会崩」。这个脚本补上后者。

检查项
    1. 所有 worker 能否被真正 import（不是 ast.parse，是 __import__）
    2. 每个 worker 能否独立启动（子进程跑一次 --check）
    3. 外部依赖：ffmpeg / ffprobe / node / aria2c / playwright / 浏览器
    4. Python 依赖：twscrape / httpx / PIL
    5. 账号库：存在性 + 账号数 + active 数
    6. 出站代理：配置了没、能不能通
    7. 数据目录：可写、空间够不够
    8. 关键文件：skill.json / index.js / 各 worker 是否齐全

用法
    python preflight.py                # 全部检查
    python preflight.py --quick        # 跳过网络探测（更快）
    python preflight.py --json         # 机器可读输出
    python preflight.py --fix-hints    # 额外打印修复建议

退出码
    0 = 全部通过（或有可忽略的警告）
    1 = 有致命问题（必须修才能用）
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 结果收集
RESULTS = []   # [(level, category, name, message)]
# level: "ok" | "warn" | "fail" | "skip"


def add(level, cat, name, msg):
    RESULTS.append({"level": level, "category": cat, "name": name, "message": msg})


# ══════════════════════════════════════════════════════════════════════
# 1. 文件完整性
# ══════════════════════════════════════════════════════════════════════

REQUIRED_FILES = [
    "skill.json",
    "index.js",
    "sender.js",
    "dedupe.js",
    "x_worker.py",
    "grab_worker.py",
    "av_sites.py",
    "hls_curl.py",
    "image_source_worker.py",
    "xmf_paths.py",
]

PY_WORKERS = [
    "grab_worker.py",
    "av_sites.py",
    "hls_curl.py",
    "image_source_worker.py",
]


def check_files():
    for fn in REQUIRED_FILES:
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            add("fail", "文件", fn, "缺失")
        elif os.path.getsize(p) < 100:
            add("warn", "文件", fn, "只有 %d 字节，可能不完整" % os.path.getsize(p))
        else:
            add("ok", "文件", fn, "%.1f KB" % (os.path.getsize(p) / 1024))


# ══════════════════════════════════════════════════════════════════════
# 2. Python import（核心检查，这次事故就靠它）
# ══════════════════════════════════════════════════════════════════════

IMPORT_TEST = r'''
import sys, os, traceback
skill = sys.argv[1]
sys.path.insert(0, skill)
os.chdir(skill)
mods = sys.argv[2:]
fail = 0
for m in mods:
    try:
        __import__(m)
        print("OK|" + m + "|")
    except Exception as e:
        tb = traceback.format_exc().strip().split("\n")
        # 只取最后两行，够定位
        detail = " / ".join(tb[-2:])[:300]
        print("FAIL|" + m + "|" + detail)
        fail = 1
sys.exit(fail)
'''


def check_imports():
    """真正 import 每个 worker —— 能抓到 NameError 这类运行时错误。"""
    mods = [os.path.splitext(f)[0] for f in PY_WORKERS]
    try:
        p = subprocess.run(
            [sys.executable, "-c", IMPORT_TEST, HERE] + mods,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
    except subprocess.TimeoutExpired:
        add("fail", "import", "全部", "超时 120 秒（某个模块 import 时卡住）")
        return

    lines = [ln for ln in (p.stdout or "").split("\n") if ln.strip()]
    for ln in lines:
        parts = ln.split("|", 2)
        if len(parts) < 2:
            continue
        status, mod, detail = parts[0], parts[1], (parts[2] if len(parts) > 2 else "")
        if status == "OK":
            add("ok", "import", mod, "可正常 import")
        else:
            add("fail", "import", mod, detail or "import 失败")

    if p.stderr and p.stderr.strip():
        # import 阶段往 stderr 写东西通常是警告
        first = p.stderr.strip().split("\n")[0][:200]
        add("warn", "import", "stderr", first)


# ══════════════════════════════════════════════════════════════════════
# 3. 外部依赖
# ══════════════════════════════════════════════════════════════════════

def check_deps():
    try:
        sys.path.insert(0, HERE)
        import xmf_paths
    except Exception as e:
        add("fail", "依赖", "xmf_paths", "无法 import: %s" % e)
        return

    desc = xmf_paths.describe()

    # 可执行文件类
    for key, label, required in (
        ("ffmpeg", "ffmpeg", True),
        ("ffprobe", "ffprobe", False),
        ("node", "node", False),
        ("aria2c", "aria2c", False),
    ):
        v = desc.get(key, "")
        if v.startswith("OK"):
            add("ok", "依赖", label, v[4:])
        elif v.startswith("缺失"):
            add("fail" if required else "warn", "依赖", label, v[3:])
        else:
            add("fail" if required else "warn", "依赖", label, v)

    # Playwright 浏览器
    b = desc.get("browsers", "")
    if b.startswith("OK"):
        add("ok", "依赖", "playwright 浏览器", b[4:])
    elif "由 Playwright 自管" in b:
        add("skip", "依赖", "playwright 浏览器", b)
    else:
        add("warn", "依赖", "playwright 浏览器",
            b + "（识图 / 番号搜索需要先 npx playwright install chromium）")

    # Python 包
    for mod, required in (("twscrape", True), ("httpx", False), ("PIL", False)):
        try:
            __import__(mod)
            add("ok", "依赖", "py:" + mod, "已安装")
        except ImportError:
            add("fail" if required else "warn", "依赖", "py:" + mod,
                "未安装（pip install -r requirements.txt）")


# ══════════════════════════════════════════════════════════════════════
# 4. 账号库
# ══════════════════════════════════════════════════════════════════════

ACCOUNT_TEST = r'''
import sys, os, asyncio, json
db = sys.argv[1]
if not os.path.exists(db):
    print("MISSING")
    sys.exit(0)
try:
    from twscrape import API
    api = API(db)
    accts = asyncio.run(api.pool.get_all())
    active = [a for a in accts if getattr(a, "active", False)]
    print("OK|%d|%d" % (len(accts), len(active)))
except Exception as e:
    print("ERR|" + str(e)[:200])
'''


def check_accounts():
    # 默认位置：<技能>/../../data/<技能id>/accounts.db
    candidates = [
        os.environ.get("XMF_DB"),
        os.path.abspath(os.path.join(HERE, "..", "..", "..", "data", "x-media-finder", "accounts.db")),
        os.path.abspath(os.path.join(HERE, "..", "..", "data", "x-media-finder", "accounts.db")),
    ]
    db = next((c for c in candidates if c and os.path.exists(c)), None)

    if not db:
        add("warn", "账号库", "accounts.db",
            "没找到。X/Twitter 搜索需要它：twscrape --db <路径> add_cookie <名字> "
            "\"auth_token=...; ct0=...\"")
        return

    add("ok", "账号库", "路径", db)

    try:
        p = subprocess.run(
            [sys.executable, "-c", ACCOUNT_TEST, db],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90,
        )
    except subprocess.TimeoutExpired:
        add("warn", "账号库", "读取", "超时 90 秒")
        return

    out = (p.stdout or "").strip()
    if out == "MISSING":
        add("warn", "账号库", "文件", "路径存在但读不到")
    elif out.startswith("OK|"):
        _, total, active = out.split("|")
        total, active = int(total), int(active)
        if total == 0:
            add("fail", "账号库", "账号数", "0 个账号（必须加一个才能搜 X）")
        elif active == 0:
            add("fail", "账号库", "active", "%d 个账号但全部 inactive（cookie 过期）" % total)
        else:
            add("ok", "账号库", "账号", "%d 个，其中 %d 个 active" % (total, active))
    elif out.startswith("ERR|"):
        add("warn", "账号库", "读取", out[4:])


# ══════════════════════════════════════════════════════════════════════
# 5. 代理
# ══════════════════════════════════════════════════════════════════════

def check_proxy(quick=False):
    try:
        sys.path.insert(0, HERE)
        import xmf_paths
        proxy = xmf_paths.PROXY
    except Exception:
        proxy = os.environ.get("XMF_PROXY", "")

    if not proxy:
        add("warn", "代理", "配置",
            "未设置（XMF_PROXY 或 paths.json 的 proxy）。"
            "X / MissAV / Pornhub / Google Lens 在多数地区需要代理才能访问")
        return

    add("ok", "代理", "配置", proxy)

    if quick:
        add("skip", "代理", "连通性", "--quick 模式跳过")
        return

    # 用 curl 探测一个目标站（不依赖 python 库）
    targets = [
        ("x.com", "https://x.com"),
        ("google.com", "https://www.google.com"),
    ]
    for name, url in targets:
        try:
            p = subprocess.run(
                ["curl", "-sS", "-o", os.devnull, "-w", "%{http_code}",
                 "--proxy", proxy, "--max-time", "15", url],
                capture_output=True, text=True, timeout=25,
            )
            code = (p.stdout or "").strip()
            if code and code != "000":
                add("ok", "代理", name, "HTTP %s" % code)
            else:
                err = (p.stderr or "").strip()[:120]
                add("warn", "代理", name, "连不上 %s" % (err or "（无响应）"))
        except FileNotFoundError:
            add("skip", "代理", name, "没装 curl，跳过")
            break
        except subprocess.TimeoutExpired:
            add("warn", "代理", name, "探测超时")
        except Exception as e:
            add("warn", "代理", name, str(e)[:120])


# ══════════════════════════════════════════════════════════════════════
# 6. 数据目录
# ══════════════════════════════════════════════════════════════════════

def check_data_dir():
    d = os.path.join(HERE, "data")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".preflight_write_test")
        with io.open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.unlink(probe)
        add("ok", "数据目录", "可写", d)
    except Exception as e:
        add("fail", "数据目录", "可写", "无法写入：%s" % e)
        return

    # 磁盘空间
    try:
        total, used, free = shutil.disk_usage(d)
        free_gb = free / (1024 ** 3)
        if free_gb < 2:
            add("fail", "数据目录", "剩余空间", "%.1f GB（视频缓存会很快撑满，建议 > 5GB）" % free_gb)
        elif free_gb < 5:
            add("warn", "数据目录", "剩余空间", "%.1f GB（偏紧）" % free_gb)
        else:
            add("ok", "数据目录", "剩余空间", "%.1f GB" % free_gb)
    except Exception:
        pass

    # 现有缓存大小
    try:
        for sub, label in (("sitevideos", "视频缓存"), ("covers", "封面缓存")):
            p = os.path.join(d, sub)
            if not os.path.isdir(p):
                continue
            sz = sum(
                os.path.getsize(os.path.join(p, f))
                for f in os.listdir(p)
                if os.path.isfile(os.path.join(p, f))
            )
            mb = sz / (1024 ** 2)
            if mb > 5000:
                add("warn", "数据目录", label, "%.0f MB（建议清理旧文件）" % mb)
            else:
                add("ok", "数据目录", label, "%.0f MB" % mb)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 7. skill.json 基本校验
# ══════════════════════════════════════════════════════════════════════

def check_manifest():
    p = os.path.join(HERE, "skill.json")
    if not os.path.exists(p):
        return
    try:
        with io.open(p, encoding="utf-8") as f:
            m = json.load(f)
    except Exception as e:
        add("fail", "manifest", "skill.json", "JSON 解析失败：%s" % e)
        return

    for k in ("id", "name", "version", "apiVersion"):
        if k not in m:
            add("warn", "manifest", k, "字段缺失")
        else:
            add("ok", "manifest", k, str(m[k]))

    # 默认群白名单
    st = m.get("settings") or {}
    allow = st.get("groupAllowlist", "")
    if not allow:
        add("warn", "manifest", "groupAllowlist",
            "为空 → 群聊内完全禁用，只能私聊。要开群就把群号填进去")
    else:
        add("ok", "manifest", "groupAllowlist", str(allow)[:60])


# ══════════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════════

ICON = {"ok": "✅", "warn": "⚠️ ", "fail": "❌", "skip": "➖"}

HINTS = {
    ("依赖", "ffmpeg"): "装 ffmpeg 并加进 PATH，或在技能目录建 paths.json 指定绝对路径",
    ("依赖", "node"): "装 Node.js >= 18，或 paths.json 里填 node 路径",
    ("依赖", "aria2c"): "磁力下载才需要；装 aria2 后填 paths.json 的 aria2",
    ("依赖", "playwright 浏览器"): "cd <装了 playwright 的目录> && npx playwright install chromium",
    ("依赖", "py:twscrape"): "pip install -r requirements.txt",
    ("账号库", "账号数"): "twscrape --db <路径> add_cookie <名字> \"auth_token=...; ct0=...\"",
    ("账号库", "active"): "cookie 可能过期了，重新从浏览器取 auth_token 和 ct0",
    ("代理", "配置"): "在 data/config.json 的 skills.x-media-finder.proxy 填代理地址",
    ("manifest", "groupAllowlist"): "在 config.json 里填允许使用的群号，逗号分隔",
}


def print_report(use_json=False, show_hints=False):
    if use_json:
        fails = sum(1 for r in RESULTS if r["level"] == "fail")
        warns = sum(1 for r in RESULTS if r["level"] == "warn")
        print(json.dumps({
            "ok": fails == 0,
            "fails": fails,
            "warns": warns,
            "results": RESULTS,
        }, ensure_ascii=False, indent=2))
        return 0 if fails == 0 else 1

    print("=" * 66)
    print("x-media-finder 部署自检")
    print("=" * 66)
    print("技能目录: %s" % HERE)
    print("Python:   %s" % sys.executable)
    print()

    cat = None
    for r in RESULTS:
        if r["category"] != cat:
            cat = r["category"]
            print("【%s】" % cat)
        line = "  %s %-22s %s" % (ICON.get(r["level"], "?"), r["name"], r["message"])
        print(line)
        if show_hints and r["level"] in ("fail", "warn"):
            h = HINTS.get((r["category"], r["name"]))
            if h:
                print("       → %s" % h)

    fails = [r for r in RESULTS if r["level"] == "fail"]
    warns = [r for r in RESULTS if r["level"] == "warn"]
    oks = [r for r in RESULTS if r["level"] == "ok"]

    print()
    print("=" * 66)
    print("结果: %d 通过 / %d 警告 / %d 失败" % (len(oks), len(warns), len(fails)))
    if fails:
        print()
        print("❌ 有致命问题，必须修：")
        for r in fails:
            print("   · [%s] %s：%s" % (r["category"], r["name"], r["message"]))
    elif warns:
        print()
        print("⚠️  能用，但这些建议修一下：")
        for r in warns:
            print("   · [%s] %s：%s" % (r["category"], r["name"], r["message"]))
    else:
        print()
        print("✅ 全部通过，可以正常使用")
    print("=" * 66)
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description="x-media-finder 部署前自检")
    ap.add_argument("--quick", action="store_true", help="跳过网络探测")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--fix-hints", action="store_true", help="打印修复建议")
    args = ap.parse_args()

    t0 = time.time()

    check_files()
    check_imports()
    check_deps()
    check_accounts()
    check_proxy(quick=args.quick)
    check_data_dir()
    check_manifest()

    code = print_report(use_json=args.json, show_hints=args.fix_hints)
    if not args.json:
        print("耗时 %.1f 秒" % (time.time() - t0))
    return code


if __name__ == "__main__":
    sys.exit(main())
