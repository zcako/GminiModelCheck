#!/usr/bin/env python3
"""Gemini Relay Audit — 一键审计 Gemini 中转站的真实上游渠道

按 Gemini中转站渠道识别方案 v1.9 实现。
零依赖 (仅 Python 标准库),Windows / Linux / macOS 通用。

用法:
    # 单 key
    python audit.py --base https://yunwu.ai --key sk-xxx --name yunwu

    # 多 key (会跑跨 key sig 矩阵)
    python audit.py --base https://4sapi.com \\
        --key key1=sk-aaa \\
        --key key2=sk-bbb \\
        --key key3=sk-ccc \\
        --name 4sapi

    # 跳过昂贵的 Tier 4 探针(只跑 Tier 1 字段指纹)
    python audit.py --base https://example.com --key sk-x --name example --skip-tier4

输出:
    ./reports/<name>-<timestamp>/
        ├── verdict.json       # 机器可读的全部 raw + 判定结果
        ├── report.md          # 人读的最终报告
        └── raw/<keyname>/*    # 每个探针的原始响应抓包
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 让 probes 子包可被导入,无论从哪里调用
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probes import active, tier4, verdict, report  # noqa: E402


def parse_keys(items: list[str]) -> list[tuple[str, str]]:
    """解析 --key 参数,支持 'name=value' 或裸 'value'(自动命名 key1/key2/...)"""
    keys: list[tuple[str, str]] = []
    for i, raw in enumerate(items, start=1):
        if "=" in raw:
            name, _, value = raw.partition("=")
            name = name.strip()
            value = value.strip()
        else:
            name = f"key{i}"
            value = raw.strip()
        if not value:
            raise ValueError(f"--key #{i} 的值是空的")
        keys.append((name, value))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="audit.py",
        description="审计 Gemini 中转站的真实上游渠道(基于 v1.9 方案)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--base",
        required=True,
        help="中转 base URL,例如 https://yunwu.ai",
    )
    parser.add_argument(
        "--key",
        action="append",
        required=True,
        help="API key,格式 'name=value' 或裸 value;可重复多次",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="本次审计的名字,用于输出目录命名",
    )
    parser.add_argument(
        "--out-root",
        default="reports",
        help="输出根目录,默认 ./reports",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.1-pro-preview",
        help="主探测模型,默认 gemini-3.1-pro-preview",
    )
    parser.add_argument(
        "--n-samples",
        type=int,
        default=20,
        help="N=? 字段采样次数(§7),默认 20",
    )
    parser.add_argument(
        "--n-self-sig",
        type=int,
        default=8,
        help="自 sig 重复性 N(§12.3),默认 8",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="单次 HTTP 请求超时秒数,默认 120(OAuth 套壳上游较慢,留足空间)",
    )
    parser.add_argument(
        "--sig-model",
        default="gemini-3-flash-preview",
        help="自 sig 回灌用的模型(必须稳定返回 thoughtSignature),默认 gemini-3-flash-preview",
    )
    parser.add_argument(
        "--skip-active",
        action="store_true",
        help="跳过主动探针(§9)",
    )
    parser.add_argument(
        "--skip-tier4",
        action="store_true",
        help="跳过 Tier 4 探针(§12);快速冒烟时可用",
    )
    parser.add_argument(
        "--skip-cross-sig",
        action="store_true",
        help="跳过跨 key sig 矩阵(§12.2),即使有多 key 也不跑",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="减少进度输出",
    )
    args = parser.parse_args()

    # 解析 keys
    try:
        keys = parse_keys(args.key)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    # 创建输出目录
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out_root) / f"{args.name}-{ts}"
    raw_root = run_dir / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)

    # 构造 context
    ctx = {
        "base": args.base.rstrip("/"),
        "name": args.name,
        "model": args.model,
        "sig_model": args.sig_model,
        "n_samples": args.n_samples,
        "n_self_sig": args.n_self_sig,
        "timeout": args.timeout,
        "keys": keys,
        "run_dir": run_dir,
        "raw_root": raw_root,
        "started_at": ts,
        "quiet": args.quiet,
    }

    if not args.quiet:
        print(f"[*] base   = {ctx['base']}")
        print(f"[*] model  = {ctx['model']}")
        print(f"[*] keys   = {[name for name, _ in keys]}")
        print(f"[*] out    = {run_dir}")
        print()

    # 收集所有探针结果
    raw: dict = {
        "meta": {
            "base": ctx["base"],
            "name": ctx["name"],
            "model": ctx["model"],
            "n_samples": ctx["n_samples"],
            "n_self_sig": ctx["n_self_sig"],
            "started_at": ts,
            "keys": [name for name, _ in keys],
            "tool_version": "1.1.5",
            "scheme_version": "v1.9",
        },
        "per_key": {},
        "cross_sig_matrix": None,
    }

    # 对每个 key 跑探针
    for kname, kval in keys:
        if not args.quiet:
            print(f"========== {kname} ==========")
        per = {}
        if not args.skip_active:
            per["active"] = active.run_all(ctx, kname, kval)
        if not args.skip_tier4:
            per["tier4"] = tier4.run_self(ctx, kname, kval)
        raw["per_key"][kname] = per

    # 跨 key sig 矩阵
    if (
        not args.skip_tier4
        and not args.skip_cross_sig
        and len(keys) >= 2
    ):
        if not args.quiet:
            print("========== cross-key sig matrix ==========")
        raw["cross_sig_matrix"] = tier4.run_cross_sig_matrix(ctx)

    # 保存 raw verdict.json
    verdict_data = verdict.compute(raw)
    raw["verdict"] = verdict_data

    verdict_path = run_dir / "verdict.json"
    with open(verdict_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    # 生成 markdown 报告
    md_path = run_dir / "report.md"
    md_text = report.render(raw)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    if not args.quiet:
        print()
        print(f"[OK] verdict.json -> {verdict_path}")
        print(f"[OK] report.md    -> {md_path}")
        print()
        # 打印一句话结论
        for kname in raw["per_key"]:
            v = verdict_data["per_key"].get(kname, {})
            label = v.get("label", "?")
            confidence = v.get("confidence", "?")
            try:
                print(f"  [{kname}] -> {label}  (confidence: {confidence})")
            except UnicodeEncodeError:
                # Windows GBK 终端无法显示 emoji，回退到 ASCII
                label_safe = label.encode('ascii', 'ignore').decode('ascii')
                print(f"  [{kname}] -> {label_safe}  (confidence: {confidence})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
