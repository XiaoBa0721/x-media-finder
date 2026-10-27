#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_release.py — 构建可发布的 x-media-finder 发布包

做什么
    1. 从技能目录复制**白名单文件**到 dist/（不复制任何测试脚本、缓存、数据）
    2. 扫描产物，确认没有个人凭据 / 绝对路径 / 私人 QQ 号
    3. 生成 SHA256 清单，便于下载者校验
    4. 打成 zip

为什么用白名单而不是黑名单
    黑名单漏一个就把开发机的 paths.json、data/ 里的封面缓存、测试脚本里的
    真实群号一起发出去了。白名单只会少发，不会多发。

用法
    python build_release.py                 # 输出到 ./dist
    python build_release.py --out D:/out    # 指定输出目录
"""
import argparse
import hashlib
import io
import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

# 只有这些文件进发布包
INCLUDE = [
    # 技能入口与元数据
    "skill.json",
    "index.js",
    "sender.js",
    "dedupe.js",
    # Python 侧
    "x_worker.py",
    "grab_worker.py",
    "av_sites.py",
    "hls_curl.py",
    "image_source_worker.py",
    "xmf_paths.py",
    "preflight.py",
    "requirements.txt",
    # 文档
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
]

# 绝对不进包的东西（即使误加到 INCLUDE 也拦住）
FORBIDDEN_PATTERNS = [
    r"paths\.json$",
    r"\.env$",
    r"accounts\.db$",
    r"\.pyc$",
    r"__pycache__",
    r"data/",
    r"test",
    r"selftest",
    r"\.log$",
]

# 内容扫描规则：命中就中止构建
# 只标记「真正能定位到作者」的特征。
#
# 刻意放行的东西：
#   127.0.0.1:7890  —— Clash 的通用默认端口，任何教程里都有，不具识别性
#   C:\Program Files —— 标准安装目录
# 这些出现在文档示例里是正常的，拦掉只会逼作者把示例改成看不懂的样子。
SECRET_PATTERNS = [
    # 凭据
    (r"auth_token\s*=\s*[0-9a-f]{20,}", "硬编码的 auth_token"),
    (r"ct0\s*=\s*[0-9a-f]{20,}", "硬编码的 ct0"),
    (r"bf6ba9b1[0-9a-f]{20,}", "硬编码的 SauceNAO key"),
    (r'"password"\s*:\s*"[^"]{4,}"', "硬编码密码"),
    (r"sk-[0-9a-f]{20,}", "硬编码 API Key"),
    # 作者的机器特征（这些目录名是本项目特有的，不是通用路径）
    (r"[A-Za-z]:\\\\skill", "作者的技能开发目录"),
    (r"[A-Za-z]:\\\\qq-bot", "作者的机器人安装目录"),
    (r"[A-Za-z]:\\\\pw-browsers", "作者的 Playwright 目录"),
    (r"[A-Za-z]:\\\\tools\\\\", "作者的工具目录"),
    (r"[A-Za-z]:\\\\SnowLuma", "作者的协议端目录"),
    # 非通用端口（7890 是 Clash 默认值，放行）
    (r"127\.0\.0\.1:(?!7890)\d{4}", "硬编码本地端口（非通用值）"),
    # 个人标识
    (r"(?:613190425|1130989634|884924662|2429647101|3470178478)", "真实 QQ 号"),
    (r"[\w.+-]+@(?:qq|gmail|163|outlook)\.com", "邮箱地址"),
]


def is_forbidden(name):
    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, name, re.I):
            return True
    return False


def scan_text(path, rel):
    """扫描文件内容，返回问题列表。"""
    issues = []
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return [(rel, "读取失败: %s" % e)]
    for pat, desc in SECRET_PATTERNS:
        for m in re.finditer(pat, text):
            # 取前后文帮助定位
            start = max(0, m.start() - 30)
            ctx = text[start:m.end() + 30].replace("\n", " ")
            issues.append((rel, "%s → …%s…" % (desc, ctx[:90])))
            break  # 同一规则每个文件只报一次
    return issues


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    ap.add_argument("--skip-scan", action="store_true", help="跳过内容扫描（不建议）")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    pkg_name = "x-media-finder"
    stage = os.path.join(out_dir, pkg_name)

    # 清理旧的
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage, exist_ok=True)

    print("=" * 62)
    print("构建 x-media-finder 发布包")
    print("=" * 62)

    copied = []
    missing = []
    for name in INCLUDE:
        if is_forbidden(name):
            print("  ⚠️  白名单里的 %s 命中禁发规则，跳过" % name)
            continue
        src = os.path.join(HERE, name)
        if not os.path.exists(src):
            missing.append(name)
            continue
        dst = os.path.join(stage, name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(name)
        print("  ✓ %-26s %6.1f KB" % (name, os.path.getsize(dst) / 1024))

    if missing:
        print()
        print("  缺失（可选，不影响构建）: %s" % ", ".join(missing))

    # ── 内容扫描 ────────────────────────────────────────────────────────
    if not args.skip_scan:
        print()
        print("扫描敏感内容…")
        all_issues = []
        for name in copied:
            all_issues.extend(scan_text(os.path.join(stage, name), name))

        if all_issues:
            print("  ❌ 发现 %d 处问题，构建中止：" % len(all_issues))
            for rel, desc in all_issues:
                print("     [%s] %s" % (rel, desc))
            print()
            print("  修掉这些再重跑，或用 --skip-scan 强行构建（不建议）。")
            shutil.rmtree(stage, ignore_errors=True)
            return 1
        print("  ✓ 未发现凭据 / 绝对路径 / 私人 QQ 号")

    # ── 生成清单 ────────────────────────────────────────────────────────
    manifest_lines = []
    for name in sorted(copied):
        p = os.path.join(stage, name)
        manifest_lines.append("%s  %s" % (sha256_of(p), name))
    manifest = "\n".join(manifest_lines) + "\n"
    io.open(os.path.join(stage, "SHA256SUMS.txt"), "w", encoding="utf-8").write(manifest)
    print()
    print("已生成 SHA256SUMS.txt（%d 个文件）" % len(copied))

    # ── 打包 ────────────────────────────────────────────────────────────
    zip_path = os.path.join(out_dir, "%s-v%s.zip" % (pkg_name, read_version(stage)))
    if os.path.exists(zip_path):
        os.unlink(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(stage):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, out_dir)
                z.write(full, arc)

    print()
    print("=" * 62)
    print("完成")
    print("  目录: %s" % stage)
    print("  压缩包: %s  (%.1f KB)" % (zip_path, os.path.getsize(zip_path) / 1024))
    print("=" * 62)
    return 0


def read_version(stage):
    try:
        import json
        with io.open(os.path.join(stage, "skill.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


if __name__ == "__main__":
    sys.exit(main())
