#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xmf_paths.py — 跨平台外部依赖探测

为什么单独一个文件
    以前每个 worker 都把 `<某台机器>\\ffmpeg\\bin\\ffmpeg.exe` 这类绝对路径写在
    文件顶部的 fallback 里。那是开发机的路径，换台机器就全废，而且会把作者
    的目录结构泄露出去。这里统一改成「环境变量 > PATH 查找 > 常见安装位置」，
    发布版里不再出现任何个人路径。

优先级
    1. 环境变量（XMF_FFMPEG / XMF_NODE / ...）—— 给特殊布局留逃生口
    2. shutil.which() —— PATH 里有就直接用，这是绝大多数情况
    3. 平台常见安装位置 —— Windows 的 Program Files / Scoop / winget，
       Linux 的 /usr/bin、/usr/local/bin，macOS 的 /opt/homebrew/bin
    4. 最后返回裸名字 —— 让子进程报错时能给出可读的「找不到 xxx」

对外只暴露 resolve() 和几个模块级常量，worker 直接 import 即可。
"""
import os
import shutil
import sys


def _which(name):
    """在 PATH 里找可执行文件，找不到返回空串。"""
    try:
        p = shutil.which(name)
        return p or ""
    except Exception:
        return ""


HERE = os.path.dirname(os.path.abspath(__file__))
_LOCAL_PATHS_FILE = os.path.join(HERE, "paths.json")


def _load_local_paths():
    """读技能目录下的 paths.json（可选）。

    为什么需要这个文件
        开发机上工具可能装在任意位置（比如某个自建目录），既不在 PATH
        也不在标准安装目录。与其把那个路径写死在代码里（换台机器就废、
        还会泄露作者的目录结构），不如放进一个**不随发布包分发**的
        paths.json：

            { "ffmpeg": "D:/tools/ffmpeg/bin/ffmpeg.exe",
              "node":   "D:/tools/node/node.exe",
              "aria2":  "D:/tools/aria2/aria2c.exe",
              "browsers": "D:/pw-browsers",
              "pw_module": "D:/mytools/pwtest",
              "proxy": "http://127.0.0.1:7890" }

        发布包用 .gitignore 式清单排除它，用户拿到的是干净的默认行为：
        走 PATH 查找。普通用户装了 ffmpeg 进 PATH 就能直接跑，不需要这个文件。
    """
    try:
        if not os.path.exists(_LOCAL_PATHS_FILE):
            return {}
        import json
        with open(_LOCAL_PATHS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_LOCAL = _load_local_paths()


def resolve(env_key, names, extra_candidates=(), local_key=None):
    """按「环境变量 → paths.json → PATH → 常见位置」的顺序解析可执行文件。

    :param env_key: 环境变量名，例如 "XMF_FFMPEG"
    :param names:   候选可执行文件名列表，例如 ["ffmpeg", "ffmpeg.exe"]
    :param extra_candidates: 额外的绝对路径候选（按顺序尝试）
    :param local_key: paths.json 里的键名（默认由 env_key 推导）
    :return: 可执行文件的绝对路径；都没找到时返回 names[0]（裸名字）
    """
    # 1) 环境变量优先（用户可以显式指定，也方便容器化部署）
    v = os.environ.get(env_key)
    if v and os.path.exists(v):
        return v

    # 2) paths.json（开发机本地覆盖，发布包不含此文件）
    key = local_key or env_key.replace("XMF_", "").lower()
    lv = _LOCAL.get(key)
    if lv and os.path.exists(str(lv)):
        return str(lv)

    # 3) PATH 查找
    for n in names:
        p = _which(n)
        if p:
            return p

    # 4) 常见安装位置
    for c in extra_candidates:
        if c and os.path.exists(c):
            return c

    # 5) 兜底：返回裸名字，让调用方的报错信息可读
    return names[0]


def _win_candidates(*rel_paths):
    """生成 Windows 下常见的安装路径候选。"""
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("USERPROFILE", ""),
        r"C:\tools",
        r"C:\ffmpeg",
    ]
    out = []
    for root in roots:
        if not root:
            continue
        for rel in rel_paths:
            out.append(os.path.join(root, rel))
    return out


def _posix_candidates(*abs_paths):
    return list(abs_paths)


# ── ffmpeg / ffprobe ────────────────────────────────────────────────────────
if sys.platform == "win32":
    _ff_extra = _win_candidates(
        r"ffmpeg\bin\ffmpeg.exe",
        r"ffmpeg\ffmpeg.exe",
        r"tools\ffmpeg\bin\ffmpeg.exe",
        r"scoop\shims\ffmpeg.exe",
    )
else:
    _ff_extra = _posix_candidates(
        "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg", "/snap/bin/ffmpeg",
    )

FFMPEG = resolve("XMF_FFMPEG", ["ffmpeg", "ffmpeg.exe"], _ff_extra)

if sys.platform == "win32":
    _fp_extra = _win_candidates(
        r"ffmpeg\bin\ffprobe.exe",
        r"ffmpeg\ffprobe.exe",
        r"tools\ffmpeg\bin\ffprobe.exe",
        r"scoop\shims\ffprobe.exe",
    )
else:
    _fp_extra = _posix_candidates(
        "/usr/bin/ffprobe", "/usr/local/bin/ffprobe",
        "/opt/homebrew/bin/ffprobe", "/snap/bin/ffprobe",
    )

FFPROBE = resolve("XMF_FFPROBE", ["ffprobe", "ffprobe.exe"], _fp_extra)


# ── node（Playwright 脚本用）──────────────────────────────────────────────
if sys.platform == "win32":
    _node_extra = _win_candidates(
        r"nodejs\node.exe",
        r"Programs\nodejs\node.exe",
        r"scoop\shims\node.exe",
    )
else:
    _node_extra = _posix_candidates(
        "/usr/bin/node", "/usr/local/bin/node", "/opt/homebrew/bin/node",
    )

NODE = resolve("XMF_NODE", ["node", "node.exe"], _node_extra)


# ── aria2c（磁力下载）────────────────────────────────────────────────────
if sys.platform == "win32":
    _aria_extra = _win_candidates(
        r"aria2\aria2c.exe",
        r"aria2\aria2-1.37.0-win-64bit-build1\aria2c.exe",
        r"scoop\shims\aria2c.exe",
    )
else:
    _aria_extra = _posix_candidates(
        "/usr/bin/aria2c", "/usr/local/bin/aria2c", "/opt/homebrew/bin/aria2c",
    )

ARIA2 = resolve("XMF_ARIA2", ["aria2c", "aria2c.exe"], _aria_extra)


# ── Playwright 浏览器目录 ──────────────────────────────────────────────────
def _playwright_browsers():
    """Playwright 下载的浏览器默认落在哪。

    优先级：PLAYWRIGHT_BROWSERS_PATH > paths.json > 各平台默认缓存目录。
    注意：设成 "0" 时 Playwright 会把浏览器放进 node_modules 里，
    那种情况这里返回空串（由 Playwright 自己处理）。
    """
    v = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if v == "0":
        return ""
    if v:
        return v

    lv = _LOCAL.get("browsers")
    if lv and os.path.exists(str(lv)):
        return str(lv)

    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "ms-playwright")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "ms-playwright")
    return os.path.join(home, ".cache", "ms-playwright")


BROWSERS = _playwright_browsers()


# ── Playwright 的 node_modules 目录 ────────────────────────────────────────
def _pw_module_dir(skill_dir):
    """找到「能 require('playwright')」的目录。

    临时 .mjs 脚本必须放在有 node_modules 的目录里，否则 Node 会报
    ERR_MODULE_NOT_FOUND。查找顺序：
      1. XMF_PW_MODULE 环境变量
      2. paths.json 的 pw_module
      3. 技能目录自己（用户把 playwright 装在技能里）
      4. 技能目录的上一级 / 上两级（monorepo 布局）
      5. 当前工作目录
    """
    v = os.environ.get("XMF_PW_MODULE")
    if v and os.path.isdir(os.path.join(v, "node_modules")):
        return v

    lv = _LOCAL.get("pw_module")
    if lv and os.path.isdir(os.path.join(str(lv), "node_modules")):
        return str(lv)

    candidates = [
        skill_dir,
        os.path.dirname(skill_dir),
        os.path.dirname(os.path.dirname(skill_dir)),
        os.getcwd(),
    ]
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "node_modules", "playwright")):
            return c
    # 退一步：只要有 node_modules 就行（playwright 可能装成别的形态）
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "node_modules")):
            return c
    return skill_dir


# ── 出站代理 ───────────────────────────────────────────────────────────────
def proxy_url():
    """出站 HTTP 代理。

    发布版不预设任何代理地址（以前默认写死 127.0.0.1:7890，等于把作者的
    本地环境写进代码）。用户必须自己配：环境变量 XMF_PROXY，或技能设置里的
    proxy 字段（index.js 会转成 XMF_PROXY 传进来）。

    返回空串表示「直连」—— 目标站能直连时这是合法的。
    """
    v = (os.environ.get("XMF_PROXY") or "").strip()
    if v:
        return v
    lv = _LOCAL.get("proxy")
    if lv:
        return str(lv).strip()
    return ""


PROXY = proxy_url()


# ── 诊断用 ─────────────────────────────────────────────────────────────────
def describe():
    """返回一份人类可读的依赖清单，给 selfcheck / 报错信息用。"""
    def _mark(p):
        if not p:
            return "未设置"
        if os.path.sep in p or "/" in p:
            return ("OK  " + p) if os.path.exists(p) else ("缺失 " + p)
        return "裸名字 " + p + "（未在 PATH 找到）"

    return {
        "python": sys.executable,
        "ffmpeg": _mark(FFMPEG),
        "ffprobe": _mark(FFPROBE),
        "node": _mark(NODE),
        "aria2c": _mark(ARIA2),
        "browsers": _mark(BROWSERS) if BROWSERS else "由 Playwright 自管",
        "proxy": PROXY or "直连（未设置 XMF_PROXY）",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(describe(), ensure_ascii=False, indent=2))
