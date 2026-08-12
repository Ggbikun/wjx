#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
问卷星自动填写脚本 (Python 版,基于 Playwright)

与 JS 版功能一致:
- 自动识别题目(单选/多选/填空/下拉/矩阵/评分星)
- 支持按权重比例填写(weights),多选按每项选中率
- 填空题支持答案池轮换(字符串数组)
- 批量提交(count),结束后输出每题实际分布统计
- 只在失败/未填/调试时截图,成功默认不截图
- 支持代理 IP 池(proxy_list/proxy_file/proxy_api),每次提交自动轮换出口 IP

使用须知:
本脚本仅适用于填写自己创建或已获授权的问卷。请勿用于伪造调查数据、
刷票或干扰他人问卷,相关平台有反作弊机制,后果自负。
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from proxy_pool import ProxyPool, proxy_display

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[错误] 未安装 playwright,请先执行: pip install -r requirements.txt")
    sys.exit(1)


DEFAULT_CONFIG = {
    "url": "",
    "count": 1,
    "headless": True,
    "delay_min_ms": 300,
    "delay_max_ms": 1200,
    "timeout_ms": 30000,
    "rules": [],
    "unmatched": "random",  # 'random' | 'skip'
    "random_seed": None,
    "screenshots_dir": "screenshots",
    "shots_success": False,
    "debug": False,
    "stats": False,
    "verbose": False,  # 详细日志:输出每题填写明细/代理明细(默认只输出提交结果)
    # ---- 代理配置(默认全部关闭,直连) ----
    "proxy": None,              # 单个代理,如 "http://user:pass@ip:port"
    "proxy_list": [],           # 代理池(内联数组)
    "proxy_file": "",           # 代理池(文件路径,每行一个代理,# 开头为注释)
    "proxy_api": "",            # 代理服务商 API,每次动态提取新 IP
    "proxy_verify": True,       # 使用前是否预验证代理连通性
    "proxy_verify_timeout": 8,  # 预验证超时(秒)
    "proxy_retry": 2,           # 代理失效时自动换代理重试次数
    "threads": 1,               # 并发提交线程数(1=串行;每线程一个浏览器实例)
}


def print_help():
    print(
        """
问卷星自动填写脚本 (Python 版)

用法:
  python fill_wjx.py --config config.json [--headed] [--count N] [--url URL]

参数:
  --config <file>   配置文件(JSON),见 config.example.json
  --url <url>       问卷链接(会覆盖配置文件中的 url)
  --count <n>       连续提交次数,默认 1
  --headed          有头模式(显示浏览器窗口,便于处理验证码)
  --headless        无头模式(默认)
  --delay <min-max> 操作间隔毫秒,如 500-1500
  --timeout <ms>    等待提交成功的超时时间
  --seed <n>        随机种子(相同种子结果可复现)
  --shots <dir>     截图保存目录
  --proxy-file <f>  代理列表文件(每行一个代理,覆盖配置文件的 proxy_file)
  --threads <n>     并发提交线程数(默认 1;每线程一个浏览器实例,count 较大时显著提速)
  --debug           输出问卷结构调试信息
  --verbose         详细日志:输出每题填写明细与代理明细(默认只输出提交结果)
  --stats           输出每题答案分布统计(默认 count>1 时自动输出)
  -h, --help        显示本帮助

代理配置(在 config.json 中,优先级 proxy > proxy_list > proxy_file > proxy_api):
  "proxy": "http://user:pass@ip:port"       单个固定代理
  "proxy_list": ["http://ip1:port", ...]   代理池(内联)
  "proxy_file": "proxies.txt"              代理池(文件,每行一个)
  "proxy_api": "https://api.xxx.com/get"   代理 API(每次调用提取新 IP)
"""
    )


def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--config")
    p.add_argument("--url")
    p.add_argument("--count", type=int)
    p.add_argument("--headless", action="store_true", dest="headless", default=None)
    p.add_argument("--headed", action="store_false", dest="headless")
    p.add_argument("--delay")
    p.add_argument("--timeout", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--shots")
    p.add_argument("--proxy-file")
    p.add_argument("--threads", type=int)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--stats", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    args = p.parse_args(argv)
    if args.help:
        print_help()
        sys.exit(0)
    return args


def load_config(args):
    file_cfg = {}
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            print(f"[错误] 配置文件不存在: {args.config}")
            sys.exit(1)
        file_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(file_cfg)
    if args.url:
        cfg["url"] = args.url
    if args.count is not None:
        cfg["count"] = args.count
    if args.headless is not None:
        cfg["headless"] = args.headless
    if args.delay:
        parts = args.delay.split("-")
        cfg["delay_min_ms"] = int(parts[0]) or DEFAULT_CONFIG["delay_min_ms"]
        cfg["delay_max_ms"] = int(parts[1]) if len(parts) > 1 else cfg["delay_min_ms"]
    if args.timeout is not None:
        cfg["timeout_ms"] = args.timeout
    if args.seed is not None:
        cfg["random_seed"] = args.seed
    if args.shots:
        cfg["screenshots_dir"] = args.shots
    if args.proxy_file:
        cfg["proxy_file"] = args.proxy_file
    if args.threads is not None:
        cfg["threads"] = max(1, args.threads)
    if args.debug:
        cfg["debug"] = True
    if args.verbose:
        cfg["verbose"] = True
    if args.stats:
        cfg["stats"] = True
    return cfg


def randint(rng, lo, hi):
    return rng.randint(lo, hi)


def shuffle(rng, arr):
    a = list(arr)
    rng.shuffle(a)
    return a


def delay(rng, cfg):
    ms = randint(rng, cfg["delay_min_ms"], cfg["delay_max_ms"])
    time.sleep(ms / 1000.0)


def vlog(verbose, *args):
    """详细日志:仅在 verbose 模式下输出,默认保持输出简洁"""
    if verbose:
        print(*args)


# ---------------------------------------------------------------------------
# 浏览器启动:优先系统 Chrome,其次 Edge
# ---------------------------------------------------------------------------
def find_system_browser():
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    return None


def launch_browser(headless):
    common = dict(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--lang=zh-CN",
            "--window-size=1280,800",
        ],
    )
    pw = sync_playwright().start()
    errors = []
    for channel in ("chrome", "msedge"):
        try:
            browser = pw.chromium.launch(**common, channel=channel)
            return pw, browser
        except Exception as e:  # noqa: BLE001
            errors.append(f"{channel}: {str(e).splitlines()[0]}")
    exe = find_system_browser()
    if exe:
        try:
            browser = pw.chromium.launch(**common, executable_path=exe)
            return pw, browser
        except Exception as e:  # noqa: BLE001
            errors.append(f"executable_path: {str(e).splitlines()[0]}")
    pw.stop()
    raise RuntimeError("无法启动浏览器:\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# 问卷结构识别(在浏览器内执行)
# ---------------------------------------------------------------------------
COLLECT_JS = r"""() => {
    const text = (el) =>
      el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
    const labelOf = (input) => {
      let p = input.closest('label');
      if (p) {
        const inner = p.querySelector('.label');
        return text(inner || p);
      }
      p = input.closest('.ui-radio, .ui-checkbox');
      if (p) {
        const inner = p.querySelector('.label');
        return text(inner || p);
      }
      p = input.closest('li');
      if (p) return text(p);
      return text(input.parentElement);
    };

    let containers = Array.from(document.querySelectorAll('div.field[data-role="fieldcontain"]'));
    let containerSelector = 'div.field[data-role="fieldcontain"]';
    if (!containers.length) {
      containers = Array.from(document.querySelectorAll('div.field'));
      containerSelector = 'div.field';
    }
    if (!containers.length) {
      containers = Array.from(document.querySelectorAll('fieldset'));
      containerSelector = 'fieldset';
    }
    if (!containers.length) {
      containers = Array.from(document.querySelectorAll('.qnr-question, .div_question'));
      containerSelector = '.qnr-question, .div_question';
    }

    const questions = [];
    let n = 0;
    for (const c of containers) {
      const titleEl =
        c.querySelector('.field-label .topichtml') ||
        c.querySelector('.field-label') ||
        c.querySelector('.qnr-question, .qtitle') ||
        c.querySelector('legend');
      let qText = text(titleEl) || text(c);
      qText = qText.replace(/^\s*\d+[.、)]?\s*/, '').replace(/\s*\*+\s*$/, '');
      const reqAttr = c.getAttribute ? c.getAttribute('req') : null;
      const required =
        reqAttr === '1' ||
        !!(c.querySelector('.qreq, .must, .req, [class*="required"]')) ||
        /\*/.test(text(titleEl) || '');

      const radios = Array.from(c.querySelectorAll('input[type="radio"]'));
      const checks = Array.from(c.querySelectorAll('input[type="checkbox"]'));
      const selects = Array.from(c.querySelectorAll('select'));
      const textareas = Array.from(c.querySelectorAll('textarea'));
      const textInputs = Array.from(
        c.querySelectorAll(
          'input[type="text"], input[type="email"], input[type="tel"], input[type="number"], input[type="url"], input[type="date"], input:not([type])'
        )
      ).filter((i) => (i.getAttribute('type') || 'text') !== 'hidden');

      const all = [...radios, ...checks];
      const distinctNames = new Set(all.map((r) => r.name)).size;

      n += 1;
      const q = {
        n,
        rawIndex: questions.length,
        text: qText,
        required,
        type: 'unknown',
        options: [],
        selectOptions: [],
        selectCount: 0,
        inputs: [],
        matrixRows: [],
        matrixHeaders: [],
        starCount: 0,
      };

      if ((radios.length || checks.length) && distinctNames > 1) {
        const rows = new Map();
        const firstTr = c.querySelector('table tr');
        q.matrixHeaders = firstTr
          ? Array.from(firstTr.children).slice(1).map((td) => text(td))
          : [];
        for (const r of all) {
          const tr = r.closest('tr');
          const rowKey = tr && tr.children.length ? text(tr.children[0]) : r.name;
          if (!rows.has(rowKey)) {
            rows.set(rowKey, {
              rowText: rowKey,
              name: r.name,
              count: 0,
              trIndex: tr ? Array.from(c.querySelectorAll('tr')).indexOf(tr) : 0,
              colLabels: tr ? Array.from(tr.children).slice(1).map((td) => text(td)) : [],
            });
          }
          rows.get(rowKey).count += 1;
        }
        q.type = radios.length ? 'matrix_radio' : 'matrix_checkbox';
        q.matrixRows = Array.from(rows.values()).map((r, i) => ({ ...r, index: i + 1 }));
      } else if (radios.length) {
        q.type = 'radio';
        q.options = radios.map((r) => ({
          index: Array.from(c.querySelectorAll('input[type="radio"]')).indexOf(r) + 1,
          label: labelOf(r),
          value: r.value,
        }));
      } else if (checks.length) {
        q.type = 'checkbox';
        q.options = checks.map((r) => ({
          index: Array.from(c.querySelectorAll('input[type="checkbox"]')).indexOf(r) + 1,
          label: labelOf(r),
          value: r.value,
        }));
      } else if (selects.length) {
        q.type = 'select';
        q.selectCount = selects.length;
        q.selectOptions = Array.from(selects[0].options).map((o, i) => ({
          index: i,
          label: text(o),
          value: o.value,
        }));
      } else if (textInputs.length || textareas.length) {
        q.type = 'text';
        q.inputs = [...textInputs, ...textareas].map((el) => ({
          kind: el.tagName.toLowerCase(),
          type: el.getAttribute('type') || '',
          placeholder: el.getAttribute('placeholder') || '',
          name: el.name || '',
          tagIndex: Array.from(c.querySelectorAll(el.tagName.toLowerCase())).indexOf(el),
        }));
      }

      const stars = Array.from(c.querySelectorAll('.star'));
      if (stars.length && q.type === 'unknown') {
        q.type = 'star';
        q.starCount = stars.length;
      }
      questions.push(q);
    }
    return { containerSelector, questions };
}"""


def collect_structure(page):
    return page.evaluate(COLLECT_JS)


# ---------------------------------------------------------------------------
# 规则匹配与填写
# ---------------------------------------------------------------------------
def find_rule(rules, question_text):
    for r in rules or []:
        if not r.get("match") or r["match"] == "*":
            return r
        try:
            if re.search(r["match"], question_text, re.IGNORECASE):
                return r
        except re.error:
            continue
    return None


def resolve_option_index(value, options, rng):
    """返回 1-based 选项序号;失败返回 None"""
    if value == "random":
        return randint(rng, 1, len(options))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if 1 <= int(value) <= len(options) else None
    s = str(value).strip()
    if s.isdigit():
        idx = int(s)
        if 1 <= idx <= len(options):
            return idx
    for i, o in enumerate(options):
        if o.get("label") == s:
            return i + 1
    # 模糊匹配时优先最短标签,避免"满意"被"非常不满意"抢先匹配
    best = -1
    best_len = float("inf")
    for i, o in enumerate(options):
        label = o.get("label") or ""
        if s in label or label in s:
            if len(label) < best_len:
                best = i
                best_len = len(label)
    return best + 1 if best >= 0 else None


def click_option(c, input_type, idx):
    """点击某个选项:输入框可能是 display:none,依次尝试可见标签,最后强制勾选"""
    input_loc = c.locator(f'input[type="{input_type}"]').nth(idx - 1)
    candidates = [
        input_loc.locator("xpath=ancestor::label[1]"),
        input_loc.locator("xpath=following-sibling::a[1]"),
        input_loc.locator('xpath=ancestor::span[contains(@class,"wrapper")][1]'),
        input_loc.locator(
            'xpath=ancestor::div[contains(@class,"ui-radio") or contains(@class,"ui-checkbox")][1]//div[contains(@class,"label")][1]'
        ),
        input_loc.locator('xpath=following-sibling::div[contains(@class,"label")][1]'),
    ]
    for loc in candidates:
        try:
            if loc.is_visible(timeout=1000):
                loc.click(timeout=2000)
        except Exception:  # noqa: BLE001
            continue
        try:
            if input_loc.is_checked():
                return True
        except Exception:  # noqa: BLE001
            pass
    try:
        input_loc.check(force=True, timeout=3000)
        return input_loc.is_checked()
    except Exception:  # noqa: BLE001
        return False


def click_input(loc):
    try:
        loc.click(timeout=3000)
    except Exception:  # noqa: BLE001
        loc.click(force=True, timeout=3000)


def fill_text_input(loc, value):
    try:
        loc.fill(str(value), timeout=3000)
        return
    except Exception:  # noqa: BLE001
        pass
    loc.evaluate(
        """(el, v) => {
            el.value = v;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        str(value),
    )


def select_option_fallback(loc, label):
    """隐藏下拉框用 JS 兜底"""
    loc.evaluate(
        """(el, label) => {
            const o = Array.from(el.options).find(x => (x.textContent || '').trim() === label || x.value === label);
            if (o) { el.value = o.value; }
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        label,
    )


def random_text_for(q, rng, sub_no):
    first = (q.get("inputs") or [{}])[0]
    hint = f"{first.get('placeholder', '')} {q.get('text', '')}"
    if first.get("type") == "email" or re.search(r"邮箱|email", hint, re.IGNORECASE):
        return f"tester{rng.randint(1000, 9999)}@example.com"
    if first.get("type") == "tel" or re.search(r"手机|电话|手机号", hint, re.IGNORECASE):
        return f"13{rng.randint(100000000, 999999999)}"
    if first.get("type") == "number":
        return str(rng.randint(1, 100))
    if first.get("type") == "date":
        return f"2026-0{rng.randint(1, 9)}-{str(rng.randint(1, 28)).zfill(2)}"
    if re.search(r"姓名|名字", hint, re.IGNORECASE):
        return f"测试用户{sub_no}"
    if first.get("kind") == "textarea":
        return f"这是第 {sub_no} 份自动化测试填写,内容仅供参考。"
    return f"测试{rng.randint(1000, 9999)}"


def random_matrix_col(rng, count):
    if count <= 0:
        return 1
    if rng.random() < 0.5:
        mid = (count + 1) // 2
        return max(1, min(count, mid + randint(rng, -1, 1)))
    return randint(rng, 1, count)


def resolve_matrix_col(value, row, headers, rng):
    if value == "random" or value is None:
        return random_matrix_col(rng, row["count"])
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(1, min(row["count"], int(value)))
    s = str(value).strip()
    if s.isdigit():
        return max(1, min(row["count"], int(s)))
    row_labels = row.get("colLabels") or []
    labels = row_labels if any(row_labels) else (headers or [])
    for i, l in enumerate(labels):
        if l == s:
            return i + 1
    for i, l in enumerate(labels):
        if l.startswith(s):
            return i + 1
    return random_matrix_col(rng, row["count"])


def weighted_index(weights, options, rng):
    """按权重随机选一个选项,返回 1-based 序号"""
    entries = []
    for i, o in enumerate(options):
        w = None
        if o.get("label") in weights:
            w = float(weights[o["label"]])
        elif str(i + 1) in weights:
            w = float(weights[str(i + 1)])
        if w is not None and w > 0:
            entries.append((i + 1, w))
    if not entries:
        return None
    total = sum(w for _, w in entries)
    r = rng.random() * total
    for idx, w in entries:
        r -= w
        if r <= 0:
            return idx
    return entries[-1][0]


def weighted_checkbox_picks(weights, options, rng):
    """多选题按权重(百分比)独立决定是否选中,保证至少选一个"""
    picks = []
    max_w = -1.0
    max_idx = -1
    for i, o in enumerate(options):
        w = None
        if o.get("label") in weights:
            w = float(weights[o["label"]])
        elif str(i + 1) in weights:
            w = float(weights[str(i + 1)])
        if w is None or w <= 0:
            continue
        if w <= 1:
            w *= 100
        if w > max_w:
            max_w = w
            max_idx = i + 1
        if rng.random() * 100 < w:
            picks.append(i + 1)
    if not picks and max_idx > 0:
        picks.append(max_idx)
    return picks


def fill_question(page, container_selector, q, value, rng, sub_no):
    c = page.locator(container_selector).nth(q["rawIndex"])
    summary = []
    chosen = []
    filled = False
    qtype = q["type"]
    weights = value.get("__weights") if isinstance(value, dict) else None

    if qtype == "radio":
        if weights is not None:
            idx = weighted_index(weights, q["options"], rng)
            if not idx:
                raise ValueError("权重未匹配到任何选项")
        else:
            raw = value[0] if isinstance(value, list) else value
            idx = resolve_option_index(raw, q["options"], rng)
            if not idx:
                raise ValueError(f"无法匹配选项: {raw!r}")
        ok = click_option(c, "radio", idx)
        if not ok:
            raise ValueError(f"点击选项失败: {q['options'][idx - 1].get('label') or idx}")
        filled = True
        label = q["options"][idx - 1].get("label") or f"选项{idx}"
        summary.append(label)
        chosen.append(label)

    elif qtype == "checkbox":
        if weights is not None:
            picks = weighted_checkbox_picks(weights, q["options"], rng)
        elif value == "random":
            n = randint(rng, 1, min(3, len(q["options"])))
            picks = shuffle(rng, [o["index"] for o in q["options"]])[:n]
        else:
            picks = value if isinstance(value, list) else [value]
        chosen_labels = []
        for v in picks:
            idx = resolve_option_index(v, q["options"], rng)
            if not idx:
                continue
            ok = click_option(c, "checkbox", idx)
            if not ok:
                continue
            filled = True
            label = q["options"][idx - 1].get("label") or f"选项{idx}"
            chosen_labels.append(label)
        if not chosen_labels:
            raise ValueError(f"无法匹配选项: {value!r}")
        summary.append("、".join(chosen_labels))
        chosen.extend(chosen_labels)

    elif qtype == "select":
        opts = q["selectOptions"]
        label = None
        if weights is not None:
            usable = [o for o in opts if o.get("label") and o.get("value") != ""]
            idx = weighted_index(weights, usable, rng)
            if not idx:
                raise ValueError("权重未匹配到任何下拉选项")
            label = usable[idx - 1]["label"]
        elif value == "random":
            usable = [o for o in opts if o.get("label") and o.get("value") != ""]
            if not usable:
                raise ValueError("下拉框无可选选项")
            label = usable[randint(rng, 0, len(usable) - 1)]["label"]
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            o = next((x for x in opts if x["index"] + 1 == int(value)), None)
            if o:
                label = o["label"]
        else:
            s = str(value).strip()
            if s.isdigit():
                o = next((x for x in opts if x["index"] + 1 == int(s)), None)
                if o:
                    label = o["label"]
            else:
                o = next((x for x in opts if x.get("label") == s), None)
                if o:
                    label = o["label"]
                else:
                    incl = [x for x in opts if s in (x.get("label") or "") or (x.get("label") or "") in s]
                    if incl:
                        label = sorted(incl, key=lambda x: len(x.get("label") or ""))[0]["label"]
        if not label:
            raise ValueError(f"无法匹配下拉选项: {value!r}")
        for i in range(q.get("selectCount") or 1):
            sel = c.locator("select").nth(i)
            try:
                sel.select_option(label=label)
            except Exception:  # noqa: BLE001
                select_option_fallback(sel, label)
        filled = True
        summary.append(label)
        chosen.append(label)

    elif qtype == "text":
        inputs = q.get("inputs", [])
        if not inputs:
            raise ValueError("填空题没有输入框")

        # 答案池:随机选一条;非必填题可按 blank_rate 概率留空
        if isinstance(value, dict) and value.get("__answers"):
            answers = value["__answers"]
            blank_rate = value.get("__blank_rate", 0)
            if blank_rate > 0 and not q.get("required") and rng.random() * 100 < blank_rate:
                v = ""
            else:
                v = rng.choice(answers)
        elif isinstance(value, list):
            # 兼容旧配置:数组答案池轮换
            v = value[(sub_no - 1) % len(value)]
        elif value == "random":
            v = random_text_for(q, rng, sub_no)
        else:
            v = str(value).replace("{n}", str(sub_no))

        # 第一个输入框填答案,其余输入框留空
        for i, input_info in enumerate(inputs):
            loc = c.locator("textarea" if input_info["kind"] == "textarea" else "input").nth(
                input_info["tagIndex"]
            )
            if i == 0 and v != "":
                fill_text_input(loc, v)
                filled = True
                summary.append(str(v))
                chosen.append(str(v))
            elif i == 0:
                # 有意留空
                filled = True
                summary.append("(留空)")
                chosen.append("(留空)")
            else:
                try:
                    fill_text_input(loc, "")
                except Exception:  # noqa: BLE001
                    pass

    elif qtype in ("matrix_radio", "matrix_checkbox"):
        for row in q.get("matrixRows", []):
            if value == "random":
                col = random_matrix_col(rng, row["count"])
            elif isinstance(value, dict):
                v = value.get(row["rowText"], value.get(row["name"], value.get("*")))
                col = resolve_matrix_col(v, row, q.get("matrixHeaders", []), rng)
            else:
                col = resolve_matrix_col(value, row, q.get("matrixHeaders", []), rng)
            tr = c.locator("tr").nth(row["trIndex"])
            click_input(tr.locator("input").nth(col - 1))
            filled = True
            cell = f"{row['rowText']}:{col}列"
            summary.append(cell)
            chosen.append(cell)

    elif qtype == "star":
        if value == "random":
            idx = min(q["starCount"], max(1, round(q["starCount"] * (rng.random() * 0.6 + 0.3))))
        else:
            idx = min(q["starCount"], max(1, int(str(value)) or 1))
        click_input(c.locator(".star").nth(idx - 1))
        filled = True
        cell = f"第{idx}颗星"
        summary.append(cell)
        chosen.append(cell)

    else:
        raise ValueError(f"暂不支持该题型(type={qtype})")

    return (" | ".join(summary) or "(已填写)", filled, chosen)


# ---------------------------------------------------------------------------
# 提交与结果验证
# ---------------------------------------------------------------------------
SUBMIT_SELECTORS = [
    "#submit_button",
    'button[type="submit"]',
    'input[type="submit"]',
    ".btn-submit",
    ".submitbtn a, .submitbtn input, .submitbtn button",
    'a:has-text("提交")',
    'button:has-text("提交")',
]

SUCCESS_JS = r"""() => {
    const t = (document.body && document.body.innerText) || '';
    return (
      /提交成功|答卷已提交|提交完成|感谢您的参与|答卷已完成/.test(t) ||
      /(complete|finish|success)(\.aspx|\.html|\?|$)/i.test(location.href)
    );
}"""


def submit_survey(page, cfg, rng):
    clicked = False
    for sel in SUBMIT_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=1000):
                loc.click()
                clicked = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        try:
            any_loc = page.get_by_text("提交", exact=False).first
            if any_loc.is_visible(timeout=1500):
                any_loc.click()
                clicked = True
        except Exception:  # noqa: BLE001
            pass
    if not clicked:
        print("[警告] 未找到提交按钮")
        return False

    delay(rng, cfg)

    # 处理"确认提交"之类的弹窗
    try:
        confirm = page.locator(
            '.layui-layer-btn0, .layui-layer-btn a:has-text("确定"), button:has-text("确定"), a:has-text("确定")'
        ).first
        if confirm.is_visible(timeout=2500):
            confirm.click()
    except Exception:  # noqa: BLE001
        pass

    try:
        page.wait_for_function(SUCCESS_JS, timeout=cfg["timeout_ms"])
        return True
    except Exception:  # noqa: BLE001
        return False


def detect_captcha(page):
    try:
        iframes = page.locator("iframe").count()
        t = page.evaluate("() => (document.body.innerText || '').slice(0, 600)")
        return iframes > 0 or bool(re.search(r"滑块|拖动|验证码|请完成验证", t))
    except Exception:  # noqa: BLE001
        return False


def take_shot(page, cfg, name):
    try:
        d = Path(cfg["screenshots_dir"]).resolve()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{name}-{int(time.time() * 1000)}.png"
        page.screenshot(path=str(f))
        print(f"[截图] {f}")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 单次填写流程
# ---------------------------------------------------------------------------
def find_next_button(page):
    """多页问卷:查找"下一页/下一题"按钮"""
    for text in ("下一页", "下一题"):
        try:
            loc = page.get_by_text(text, exact=True).first
            if loc.is_visible(timeout=800):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def run_one(page, cfg, sub_no, rng):
    t0 = time.time()
    page.goto(cfg["url"], wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    delay(rng, cfg)

    # 部分问卷进入后需要先点击"开始作答"
    try:
        start_btn = page.get_by_text("开始作答", exact=True).first
        if start_btn.is_visible(timeout=3000):
            start_btn.click()
            vlog(cfg.get("verbose"), "[信息] 已点击“开始作答”")
            delay(rng, cfg)
    except Exception:  # noqa: BLE001
        pass

    struct = collect_structure(page)
    if not struct.get("questions"):
        print("[错误] 未能识别到问卷题目,可能页面结构不兼容、问卷已结束或需要登录。")
        take_shot(page, cfg, f"debug-{sub_no}")
        return {"ok": False, "stats": {}}
    all_questions = struct["questions"]
    vlog(cfg.get("verbose"), f"[信息] 识别到 {len(all_questions)} 道题")
    if cfg.get("debug"):
        print(json.dumps(struct, ensure_ascii=False, indent=2))

    unfilled = []
    stats = {}
    filled_ns = set()
    page_no = 0
    while True:
        page_no += 1
        struct = collect_structure(page)
        questions = struct["questions"]
        container_selector = struct["containerSelector"]

        for q in questions:
            if q["n"] in filled_ns:
                continue
            # 多页问卷:当前页不可见的题目留给后续"下一页"
            try:
                container = page.locator(container_selector).nth(q["rawIndex"])
                if not container.is_visible(timeout=1000):
                    continue
            except Exception:  # noqa: BLE001
                continue

            rule = find_rule(cfg["rules"], q["text"])
            value = None
            if rule:
                if rule.get("weights") and len(rule["weights"]):
                    value = {"__weights": rule["weights"]}
                elif isinstance(rule.get("value"), list):
                    value = {
                        "__answers": rule["value"],
                        "__blank_rate": rule.get("blank_rate", 0),
                    }
                else:
                    value = rule.get("value")
            elif cfg["unmatched"] == "random":
                value = "random"
            if value is None:
                vlog(cfg.get("verbose"), f"[{q['n']}] {q['text'] or '(无题目标题)'} -> 跳过")
                if q.get("required"):
                    unfilled.append(q)
                continue
            try:
                summary, filled, chosen = fill_question(
                    page, container_selector, q, value, rng, sub_no
                )
                vlog(cfg.get("verbose"), f"[{q['n']}] {q['text'] or '(无题目标题)'} -> {summary}")
                if not filled:
                    unfilled.append(q)
                else:
                    filled_ns.add(q["n"])
                for ch in chosen:
                    if q["n"] not in stats:
                        stats[q["n"]] = {"text": q["text"] or "", "counts": {}}
                    stats[q["n"]]["counts"][ch] = stats[q["n"]]["counts"].get(ch, 0) + 1
            except Exception as e:  # noqa: BLE001
                print(f"[{q['n']}] {q['text'] or '(无题目标题)'} 填写失败: {e}")
                unfilled.append(q)
            delay(rng, cfg)

        if unfilled:
            break
        next_btn = find_next_button(page)
        if next_btn is None:
            break
        next_btn.click()
        vlog(cfg.get("verbose"), "[信息] 已点击“下一页”")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        delay(rng, cfg)
        if page_no > 30:  # 安全阀,防止死循环
            print("[警告] 翻页次数过多,中止翻页")
            break

    # 必填题必须全部填上才提交
    for q in all_questions:
        if q.get("required") and q["n"] not in filled_ns and q not in unfilled:
            unfilled.append(q)

    if unfilled:
        names = "; ".join(f"{q['n']}.{q['text'] or '(无题目标题)'}" for q in unfilled)
        print(f"[警告] 有 {len(unfilled)} 道题未填成功,为避免提交卡在必填校验,本次不提交:")
        print(f"       {names}")
        take_shot(page, cfg, f"unfilled-{sub_no}")
        return {"ok": False, "stats": stats}

    # 滚动到底部,等待提交按钮出现
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    delay(rng, cfg)

    ok = submit_survey(page, cfg, rng)
    if not ok:
        take_shot(page, cfg, f"fail-{sub_no}")
        if detect_captcha(page):
            print("[提示] 页面出现验证码/滑块,请改用 --headed 模式手动完成,或稍后重试。")
        else:
            print("[警告] 未检测到提交成功提示,请查看截图确认(可能是必填题未填或页面结构变化)。")
        return {"ok": False, "stats": stats}
    if cfg.get("shots_success"):
        take_shot(page, cfg, f"success-{sub_no}")
    return {"ok": True, "stats": stats}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def print_distribution(all_stats, total):
    print(f"\n===== 答案分布统计(共 {total} 份) =====")
    for key in sorted(all_stats, key=lambda k: int(k)):
        st = all_stats[key]
        parts = sorted(st["counts"].items(), key=lambda kv: -kv[1])
        line = ", ".join(f"{v} {c / total * 100:.1f}%({c})" for v, c in parts)
        print(f"{key}.{st['text']}: {line}")


def submit_worker(worker_id, task_ids, cfg, rng_seed, pool):
    """工作线程:独立浏览器实例,串行处理分配到的提交任务;返回 (成功数, 统计)"""
    # 每个线程独立的随机数生成器(基于种子+线程号),保证可复现
    rng = random.Random(rng_seed + worker_id * 1000003)
    tag = f"[线程{worker_id}]"

    try:
        # launch_browser 内部已启动 sync_playwright,不能在此重复 start(),
        # 否则第二次 start() 会检测到第一个正在运行的 asyncio loop 而报错
        pw, browser = launch_browser(cfg["headless"])
    except Exception as e:  # noqa: BLE001
        print(f"{tag} 启动浏览器失败: {e}")
        return 0, {}

    ok_count = 0
    stats = {}
    try:
        for i in task_ids:
            print(f"{tag} ==== 第 {i}/{cfg['count']} 次提交 ====")
            # 每次提交取一个新代理,保证出口 IP 不同(代理池全局线程安全共享)
            proxy = pool.next_proxy()
            if proxy:
                vlog(cfg.get("verbose"), f"{tag} 使用代理: {proxy_display(proxy)}")
            elif pool.enabled:
                print(f"{tag} [警告] 未获取到可用代理,本次尝试直连")

            res = None
            retry = int(cfg.get("proxy_retry", 0))
            t_submit = time.time()
            for attempt in range(retry + 1):
                # 代理是 context 级别的,每次提交新建独立 context(新会话+新代理)
                ctx_kwargs = dict(locale="zh-CN", viewport={"width": 1280, "height": 800})
                if proxy:
                    ctx_kwargs["proxy"] = proxy
                context = browser.new_context(**ctx_kwargs)
                page = context.new_page()
                try:
                    res = run_one(page, cfg, i, rng)
                    break
                except Exception as e:  # noqa: BLE001
                    # 只打印异常首行,避免 Page.goto 的长堆栈刷屏
                    detail = str(e).splitlines()[0] if str(e) else repr(e)
                    print(f"{tag} [警告] 第 {i} 次提交异常: {detail}")
                    if proxy:
                        pool.mark_bad(proxy)
                    if attempt < retry:
                        proxy = pool.next_proxy()
                        print(
                            f"{tag} [信息] 更换代理重试: "
                            f"{proxy_display(proxy) if proxy else '直连'}"
                        )
                        continue
                    res = {"ok": False, "stats": {}}
                    break
                finally:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        pass

            elapsed = time.time() - t_submit
            if res["ok"]:
                ok_count += 1
                print(f"{tag} [成功] 第 {i}/{cfg['count']} 次提交成功,耗时 {elapsed:.1f}s")
            else:
                print(f"{tag} [失败] 第 {i}/{cfg['count']} 次提交失败,耗时 {elapsed:.1f}s(原因见上方警告或截图)")
            for key, st in (res.get("stats") or {}).items():
                if key not in stats:
                    stats[key] = {"text": st["text"], "counts": {}}
                for v, c in st["counts"].items():
                    stats[key]["counts"][v] = stats[key]["counts"].get(v, 0) + c
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
    return ok_count, stats


def main():
    cfg = load_config(parse_args(sys.argv[1:]))
    if not cfg["url"]:
        print("[错误] 请通过 --url 或配置文件提供问卷链接。运行 --help 查看帮助。")
        sys.exit(1)

    seed = cfg["random_seed"] if cfg["random_seed"] is not None else int(time.time() * 1000) ^ 0x9E3779B9
    rng = random.Random(seed)
    threads = max(1, int(cfg.get("threads", 1)))

    print(f"[信息] 问卷: {cfg['url']}")
    print(f"[信息] 提交次数: {cfg['count']},模式: {'有头' if not cfg['headless'] else '无头'},线程数: {threads}")

    pool = ProxyPool(cfg, rng)
    print(f"[信息] 代理模式: {pool.mode_desc()}")
    if not pool.enabled and any(
        [cfg.get("proxy"), cfg.get("proxy_list"), cfg.get("proxy_file"), cfg.get("proxy_api")]
    ):
        print("[警告] 检测到代理配置但未能解析出有效代理(检查格式/文件路径),本次运行使用直连。")
    if threads > 1 and cfg["count"] < threads:
        print(f"[警告] 提交次数({cfg['count']})小于线程数({threads}),实际有效线程为 {cfg['count']}。")
    if threads > 1 and cfg["count"] > 1:
        print(f"[提示] 多线程模式下每个线程独立浏览器,内存占用随线程数增长,建议 2-5 个线程。")

    # 任务按轮询分配给各线程,保证任务数均衡
    task_lists = [[] for _ in range(threads)]
    for i in range(1, cfg["count"] + 1):
        task_lists[(i - 1) % threads].append(i)

    t0 = time.time()
    if threads == 1:
        results = [submit_worker(0, task_lists[0], cfg, seed, pool)]
    else:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = [
                ex.submit(submit_worker, w, tasks, cfg, seed, pool)
                for w, tasks in enumerate(task_lists)
            ]
            results = [f.result() for f in futures]

    ok_count = 0
    all_stats = {}
    for ok, stats in results:
        ok_count += ok
        for key, st in stats.items():
            if key not in all_stats:
                all_stats[key] = {"text": st["text"], "counts": {}}
            for v, c in st["counts"].items():
                all_stats[key]["counts"][v] = all_stats[key]["counts"].get(v, 0) + c

    if cfg["count"] > 1 or cfg.get("stats"):
        print_distribution(all_stats, cfg["count"])
    print(f"\n[完成] 成功 {ok_count}/{cfg['count']} 次,总耗时 {time.time() - t0:.0f}s。")
    if ok_count < cfg["count"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
