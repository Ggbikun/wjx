#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费代理池更新工具
==================
从多个免费源抓取 HTTP 代理 -> 去重 -> 并发验证(必须能连通国内站点) -> 写入 proxies.txt

用法:
  python update_proxies.py                  # 更新默认 proxies.txt
  python update_proxies.py --out my.txt     # 输出到指定文件
  python update_proxies.py --min 10         # 可用代理数 < 10 时以错误码退出
  python update_proxies.py --verify-url http://www.baidu.com  # 自定义验证目标

数据源(均为公开免费代理,多源冗余,单个失效自动跳过):
  - Proxifly free-proxy-list  每 5 分钟自动验证更新,jsdelivr CDN 国内可直连
  - ProxyScrape 免费聚合 API

注意:
  免费代理可用率低(通常 <30%)、存活时间短(几分钟~几小时),
  建议每次批量提交前重新运行本工具刷新代理池。
"""

import argparse
import concurrent.futures as cf
import random
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from proxy_pool import parse_proxy, proxy_to_url, verify_proxy

DEFAULT_OUT = "proxies.txt"
# 用 HTTPS 验证:问卷星是 HTTPS 站点,可过滤掉不支持 HTTPS 隧道的代理
DEFAULT_VERIFY_URL = "https://www.baidu.com"

SOURCES = [
    # Proxifly - 每 5 分钟自动验证更新,jsdelivr CDN 国内可直连
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
    # ProxyScrape - 免费聚合 API(备用源)
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
]


def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--out", default=DEFAULT_OUT, help="输出文件路径(默认 proxies.txt)")
    p.add_argument("--min", type=int, default=0, help="可用代理数下限,不足则退出码 1")
    p.add_argument("--timeout", type=float, default=4.0, help="单个代理验证超时秒数(默认 4)")
    p.add_argument("--workers", type=int, default=80, help="并发验证线程数(默认 80)")
    p.add_argument("--verify-url", default=DEFAULT_VERIFY_URL, help="验证连通性使用的 URL")
    p.add_argument("-h", "--help", action="store_true")
    args = p.parse_args(argv)
    if args.help:
        print(
            """免费代理池更新工具

用法:
  python update_proxies.py [--out proxies.txt] [--min 10] [--timeout 4] [--verify-url URL]

参数:
  --out <file>       输出文件路径(默认 proxies.txt)
  --min <n>          可用代理数下限,不足则以退出码 1 结束
  --timeout <sec>    单个代理验证超时(默认 4 秒)
  --workers <n>      并发验证线程数(默认 80)
  --verify-url <url> 验证连通性使用的 URL(默认 https://www.baidu.com)
"""
        )
        sys.exit(0)
    return args


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect_proxies():
    """多源抓取并去重,返回代理字典列表"""
    seen = {}
    for url in SOURCES:
        try:
            raw = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"[跳过] 源不可用: {url} ({str(e).splitlines()[0]})")
            continue
        added = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            proxy = parse_proxy(line)
            if not proxy:
                continue
            key = proxy_to_url(proxy)
            if key not in seen:
                seen[key] = proxy
                added += 1
        print(f"[抓取] {url}\n       -> 新增 {added} 个(累计去重 {len(seen)})")
        time.sleep(0.5)
    return list(seen.values())


def verify_one(proxy, timeout, verify_url):
    try:
        return proxy, verify_proxy(proxy, timeout=timeout, test_url=verify_url)
    except Exception:  # noqa: BLE001
        return proxy, False


def main():
    args = parse_args(sys.argv[1:])
    print("===== 免费代理池更新 =====")
    proxies = collect_proxies()
    if not proxies:
        print("[错误] 所有源均未能获取到代理,请检查网络后重试。")
        sys.exit(1)

    # 打乱顺序,避免写入文件时把同一来源的代理排在一起
    random.shuffle(proxies)
    total = len(proxies)
    print(f"[信息] 开始并发验证 {total} 个代理(超时 {args.timeout}s,线程 {args.workers})...")

    ok_list = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [
            ex.submit(verify_one, p, args.timeout, args.verify_url) for p in proxies
        ]
        done = 0
        for fut in cf.as_completed(futures):
            proxy, ok = fut.result()
            done += 1
            if ok:
                ok_list.append(proxy)
            if done % 200 == 0 or done == total:
                print(f"[验证] {done}/{total},已可用 {len(ok_list)}...")

    elapsed = time.time() - t0
    ok_list.sort(key=lambda p: p["server"])  # 稳定排序,便于 diff
    out_path = Path(args.out)
    lines = [proxy_to_url(p) for p in ok_list]
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    print(f"\n===== 结果:抓取 {total} 个,可用 {len(ok_list)} 个(可用率 {len(ok_list) / total * 100:.1f}%),耗时 {elapsed:.0f}s =====")
    print(f"[信息] 已写入: {out_path.resolve()}")
    if ok_list:
        sample = ", ".join(proxy_to_url(p) for p in ok_list[:3])
        print(f"[示例] {sample} ...")
    if len(ok_list) < args.min:
        print(f"[错误] 可用代理 {len(ok_list)} 个,低于要求的 {args.min} 个。")
        sys.exit(1)


if __name__ == "__main__":
    main()
