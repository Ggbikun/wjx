#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理 IP 池模块
===============
为问卷星自动填写脚本提供代理支持,让每次提交走不同出口 IP。

支持四种来源(按优先级,同时配置时只取第一个生效):
1. proxy        - 单个固定代理(字符串或对象)
2. proxy_list   - 内联代理列表(JSON 数组)
3. proxy_file   - 代理列表文件(每行一个代理,# 开头为注释行)
4. proxy_api    - 代理服务商 API,每次调用动态提取新 IP(适合"每请求一 IP"的隧道/提取代理)

代理格式兼容:
  "http://user:pass@ip:port"
  "socks5://ip:port"
  {"server": "http://ip:port", "username": "user", "password": "pass"}
"""

import json
import os
import random
import re
import sys
import threading
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROXY_RE = re.compile(
    r"^(?:(http|https|socks4|socks5)://)?"
    r"(?:([^:@/\s]+):([^@/\s]+)@)?"
    r"([^:/@\s]+):(\d+)$"
)


def parse_proxy(text):
    """将代理文本/对象解析为 Playwright 可用的 proxy 字典;失败返回 None"""
    if isinstance(text, dict):
        server = (text.get("server") or "").strip()
        if not server:
            return None
        return {
            "server": server,
            "username": text.get("username") or "",
            "password": text.get("password") or "",
        }
    m = PROXY_RE.match(str(text).strip())
    if not m:
        return None
    scheme, user, pwd, host, port = m.groups()
    return {
        "server": f"{scheme or 'http'}://{host}:{port}",
        "username": user or "",
        "password": pwd or "",
    }


def proxy_to_url(proxy):
    """把 proxy 字典还原为带账号密码的完整 URL(供 urllib 验证使用)"""
    server = proxy["server"]
    if proxy.get("username"):
        scheme, _, rest = server.partition("://")
        return f"{scheme}://{proxy['username']}:{proxy['password']}@{rest}"
    return server


def proxy_display(proxy):
    """脱敏显示代理信息,便于日志输出,避免泄露账号密码"""
    if not proxy:
        return "直连(无代理)"
    server = proxy.get("server", "")
    if proxy.get("username"):
        return server.replace("://", "://***:***@", 1)
    return server


def fetch_proxy_api(api_url, timeout=15):
    """调用代理 API 获取代理,兼容 JSON 数组 / {data:...} / 纯文本 / 逗号分隔等常见返回格式"""
    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = []
        for key in ("data", "result", "proxy", "proxies", "list", "ips"):
            val = data.get(key)
            if isinstance(val, list):
                items.extend(val)
            elif isinstance(val, str):
                items.append(val)
        if not items:
            for key, val in data.items():
                if isinstance(val, (list, str)):
                    items.extend(val if isinstance(val, list) else [val])
                    break
    else:
        items = [raw]
    result = []
    for item in items:
        if isinstance(item, dict):
            p = parse_proxy(item)
            if p:
                result.append(p)
            continue
        for piece in re.split(r"[\s,;]+", str(item)):
            piece = piece.strip()
            if not piece:
                continue
            p = parse_proxy(piece)
            if p:
                result.append(p)
    return result


def verify_proxy(proxy, timeout=8, test_url="http://www.baidu.com"):
    """验证代理能否连通;socks 代理 urllib 不支持,跳过预验证交由运行时检测"""
    scheme = (proxy["server"] or "").split("://", 1)[0].lower()
    if scheme.startswith("socks"):
        return True
    proxy_url = proxy_to_url(proxy)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    try:
        req = urllib.request.Request(test_url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        return False


class ProxyPool:
    """代理池:加载 -> 洗牌轮换取出 -> 失效标记跳过,尽量保证每次出口 IP 不同"""

    def __init__(self, cfg, rng=None):
        self.rng = rng or random.Random()
        self.verify = bool(cfg.get("proxy_verify", True))
        self.verify_timeout = float(cfg.get("proxy_verify_timeout", 8))
        # 默认用 HTTPS 验证:问卷星是 HTTPS 站点,不支持 HTTPS 隧道的代理必然失败
        self.verify_url = cfg.get("proxy_verify_url", "https://www.baidu.com")
        self.api = (cfg.get("proxy_api") or "").strip()
        self.mode = "none"  # none | single | pool | api
        self._lock = threading.Lock()  # 多线程并发取代理/标记失效时互斥
        self.proxies = []   # 全部代理(去重)
        self._urls = set()  # 已收录代理的 URL 标识(用于去重)
        self.queue = []     # 待轮换队列
        self.used = set()   # 本轮已使用
        self.bad = set()    # 已失效代理
        self.checked = set()  # 已验证可用(避免重复验证)
        self._load(cfg)

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def _load(self, cfg):
        p = parse_proxy(cfg.get("proxy")) if cfg.get("proxy") else None
        if p:
            self._add(p)
            self.mode = "single"
            return
        for item in cfg.get("proxy_list") or []:
            p = parse_proxy(item)
            if p:
                self._add(p)
        if self.proxies:
            self.mode = "pool"
            return
        file_path = cfg.get("proxy_file") or ""
        if file_path and os.path.exists(file_path):
            for line in Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                p = parse_proxy(line)
                if p:
                    self._add(p)
        if self.proxies:
            self.mode = "pool"
            return
        if self.api:
            self.mode = "api"

    def _add(self, proxy):
        with self._lock:
            if proxy_to_url(proxy) not in self._urls:
                self.proxies.append(proxy)
                self._urls.add(proxy_to_url(proxy))

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def enabled(self):
        return self.mode != "none"

    def mode_desc(self):
        if self.mode == "single":
            return "单个固定代理"
        if self.mode == "pool":
            return f"代理池(共 {len(self.proxies)} 个)"
        if self.mode == "api":
            return "API 动态提取"
        return "直连(未启用代理)"

    def next_proxy(self):
        """取出下一个可用代理(线程安全,轮换策略尽量不重复);无可用时返回 None"""
        if self.mode == "none":
            return None
        if self.mode == "api":
            return self._from_api()
        # 安全阀:最多尝试 池大小*2 次(每次取一个候选,失败则标记跳过)
        for _ in range(max(len(self.proxies) * 2, 8)):
            with self._lock:
                if not self.queue:
                    self._refill_locked()
                proxy = None
                while self.queue:
                    candidate = self.queue.pop()
                    key = proxy_to_url(candidate)
                    if key in self.bad or key in self.checked or key in self.used:
                        continue
                    proxy = candidate
                    break
                if proxy is None:
                    return None
                key = proxy_to_url(proxy)
                self.used.add(key)
            # 锁外验证,避免长时间占用锁阻塞其他线程
            if not self.verify:
                return proxy
            if key in self.checked:
                return proxy
            if verify_proxy(proxy, self.verify_timeout, self.verify_url):
                with self._lock:
                    self.checked.add(key)
                return proxy
            with self._lock:
                self.bad.add(key)
        return None

    def mark_bad(self, proxy):
        """标记代理失效(提交失败时调用),后续轮换自动跳过"""
        if proxy:
            with self._lock:
                self.bad.add(proxy_to_url(proxy))

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _refill_locked(self):
        """重置轮换队列(调用方必须已持有 self._lock);used 清空后代理可复用"""
        self.used.clear()
        self.queue = [p for p in self.proxies if proxy_to_url(p) not in self.bad]
        self.rng.shuffle(self.queue)

    def _from_api(self):
        try:
            got = fetch_proxy_api(self.api)
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 代理 API 获取失败: {e}")
            return None
        for p in got:
            self._add(p)
        if not got:
            return None
        if not self.verify:
            return got[0]
        for p in got:
            key = proxy_to_url(p)
            with self._lock:
                if key in self.checked:
                    return p
            if verify_proxy(p, self.verify_timeout, self.verify_url):
                with self._lock:
                    self.checked.add(key)
                return p
            with self._lock:
                self.bad.add(key)
        return None
