#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
问卷链接解析 & config.json 自动生成工具

用法:
  python parse_survey.py --url <问卷链接> [--out config.json] [--headed]

行为:
  1. 打开问卷页面,自动识别每道题的题型和选项;
  2. 为每题生成默认规则:
       - 单选 / 下拉:各选项等权重(自动归一化到 100)
       - 多选:每个选项 50% 选中率(总和可不为 100)
       - 填空:random(脚本按输入框类型自动生成内容)
       - 矩阵 / 评分:random
       - 名称含"其他/请举例"的选项默认权重 0(避免触发条件填空,可自行调整)
  3. 覆盖写入 --out 指定的配置文件(默认 config.json),原文件自动备份;
  4. 打印解析出的题目清单,方便核对。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import fill_wjx
from fill_wjx import COLLECT_JS, DEFAULT_CONFIG, delay, launch_browser


def is_conditional_option(label):
    """名字含"其他/请举例"的选项,选中后通常会弹出条件填空"""
    s = (label or "").strip()
    return s == "其他" or "请举例" in s or "请注明" in s or "其他(" in s


def load_api_key(args):
    """读取 DeepSeek API Key:优先命令行 --key,其次环境变量,最后同目录 .env"""
    if args.key:
        return args.key
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def call_llm(base_url, model, api_key, prompt, timeout=120):
    """调用 OpenAI 兼容的对话补全接口(DeepSeek 使用同一协议)"""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一名问卷数据分析专家。请根据公开的市场调研、社会调研常识,"
                        "为问卷的每个问题设计符合真实人群分布的答案占比。只输出 JSON,不要输出其他文字。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def build_ai_prompt(questions):
    lines = [
        "请为下面这份匿名问卷设计合理的答案占比。要求:",
        "1. 单选题/下拉题:每个选项给出权重(整数百分比),同一题所有选项加起来必须等于 100;",
        "2. 多选题:每个选项给出\"被选中的概率\"(0-100 的整数百分比),总和不需要等于 100,但要符合真实人群的选择习惯;",
        "3. 填空题:生成 10-20 条简短、真实、互不重复的候选回答(每条不超过 40 字),用于随机填入;",
        "4. 矩阵/评分题:不需要给比例;",
        "5. 只输出一个 JSON 对象,格式:",
        '   {"proportions": [{"question": "题目原文", "type": "radio", "weights": {"选项A": 40, "选项B": 60}}, {"question": "题目原文", "type": "text", "answers": ["回答1", "回答2", "回答3"]}]}',
        "说明:单选/多选用 weights 字段,填空题用 answers 字段。",
        "题目清单:",
    ]
    for q in questions:
        qtype = q["type"]
        if qtype in ("radio", "select"):
            opts = " / ".join(o["label"] for o in (q.get("options") or q.get("selectOptions", [])))
            lines.append(f"{q['n']}. [{qtype}] {q['text']}  选项: {opts}")
        elif qtype == "checkbox":
            opts = " / ".join(o["label"] for o in q.get("options", []))
            lines.append(f"{q['n']}. [{qtype}] {q['text']}  选项: {opts}")
        else:
            lines.append(f"{q['n']}. [{qtype}] {q['text']}")
    return "\n".join(lines)


def apply_ai_proportions(questions, rules, ai_text):
    """把 AI 返回的 JSON 合并进规则;无法匹配或无效的项保留默认值"""
    try:
        data = json.loads(ai_text)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"AI 返回的不是有效 JSON: {e}")
    items = data.get("proportions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("AI 返回格式不正确,缺少 proportions 数组")

    rule_by_text = {}
    for q, r in zip(questions, rules):
        rule_by_text[q["text"]] = (q, r)

    for item in items:
        qtext = item.get("question")
        if not qtext or qtext not in rule_by_text:
            continue
        q, rule = rule_by_text[qtext]

        if q["type"] == "text":
            answers = item.get("answers")
            if isinstance(answers, list) and len(answers) >= 2:
                pool = [str(a).strip() for a in answers if str(a).strip()][:20]
                if len(pool) >= 2:
                    rule["value"] = pool
                    # 非必填填空题:20% 概率留空
                    if not q.get("required"):
                        rule["blank_rate"] = 20
            continue

        weights = item.get("weights")
        if not isinstance(weights, dict) or not weights:
            continue

        labels = [o["label"] for o in (q.get("options") or q.get("selectOptions", []))]
        known = {}
        for k, v in weights.items():
            if k in labels:
                try:
                    known[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
        if not known:
            continue
        # 条件选项(其他/请举例)强制 0,避免触发条件填空
        for l in list(known):
            if is_conditional_option(l):
                known[l] = 0.0

        if q["type"] in ("radio", "select"):
            total = sum(known.values())
            if total <= 0:
                continue
            normalized = {l: round(w / total * 100) for l, w in known.items()}
            diff = 100 - sum(normalized.values())
            if diff and normalized:
                first = next(iter(normalized))
                normalized[first] += diff
            rule["weights"] = normalized
        elif q["type"] == "checkbox":
            rule["weights"] = {l: int(round(min(100.0, max(0.0, w)))) for l, w in known.items()}
    return rules


def print_ai_summary(questions, rules):
    print("\n===== AI 建议比例 =====")
    for q, r in zip(questions, rules):
        if "weights" in r:
            parts = ", ".join(f"{k}={v}" for k, v in r["weights"].items())
            print(f"{q['n']}. {q['text']}")
            print(f"    {parts}")
        elif isinstance(r.get("value"), list):
            print(f"{q['n']}. {q['text']}")
            print(f"    答案池({len(r['value'])}条): {' | '.join(r['value'][:5])} ...")
        else:
            print(f"{q['n']}. {q['text']}")
            print(f"    value = {r.get('value')}")


def build_rule(q):
    """为一道题生成默认规则(与填写脚本 find_rule 的 match 正则兼容)"""
    match = re.escape(q["text"])
    qtype = q["type"]

    if qtype == "radio":
        labels = [o["label"] for o in q.get("options", [])]
        labels = [l for l in labels if not is_conditional_option(l)]
        if not labels:
            labels = [o["label"] for o in q.get("options", [])]
        base = 100 // len(labels)
        weights = {l: base for l in labels}
        weights[labels[0]] += 100 - base * len(labels)  # 余数分给第一个选项
        return {"match": match, "weights": weights}

    if qtype == "select":
        labels = [o["label"] for o in q.get("selectOptions", []) if o.get("label") and o.get("value") != ""]
        labels = [l for l in labels if not is_conditional_option(l)]
        if not labels:
            labels = [o["label"] for o in q.get("selectOptions", []) if o.get("label")]
        base = 100 // len(labels)
        weights = {l: base for l in labels}
        weights[labels[0]] += 100 - base * len(labels)
        return {"match": match, "weights": weights}

    if qtype == "checkbox":
        labels = [o["label"] for o in q.get("options", [])]
        weights = {l: 0 if is_conditional_option(l) else 50 for l in labels}
        if not any(w > 0 for w in weights.values()):
            weights = {l: 50 for l in labels}
        return {"match": match, "weights": weights}

    # 填空 / 矩阵 / 评分等,默认随机
    return {"match": match, "value": "random"}


def build_rules(questions):
    return [build_rule(q) for q in questions]


def print_questions(questions):
    print("\n===== 解析结果 =====")
    for q in questions:
        print(f"{q['n']}. [{q['type']}] {q['text'] or '(无题目标题)'}")
        if q.get("options"):
            for o in q["options"]:
                mark = "  <-- 条件选项,默认权重0" if is_conditional_option(o["label"]) else ""
                print(f"    - {o['label']}{mark}")
        elif q.get("selectOptions"):
            for o in q["selectOptions"]:
                if o.get("label"):
                    print(f"    - {o['label']}")
        elif q.get("inputs"):
            for i in q["inputs"]:
                print(f"    - 输入框[{i['kind']}]: {i.get('placeholder') or '(无占位符)'}")
        elif q.get("matrixRows"):
            print(f"    - 矩阵 {len(q['matrixRows'])} 行")
        elif q.get("starCount"):
            print(f"    - 评分 {q['starCount']} 颗星")


def write_config(out_path, url, rules):
    out = Path(out_path)
    base = dict(DEFAULT_CONFIG)
    if out.exists():
        backup = out.with_name(out.stem + ".backup" + out.suffix)
        backup.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[备份] 原配置已备份到 {backup}")
        try:
            base.update(json.loads(out.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    else:
        base["headless"] = False
        base["count"] = 10
    cfg = dict(base)
    cfg["url"] = url
    cfg["rules"] = rules
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[完成] 已写入 {out.resolve()}")
    print(f"[提示] 默认规则是等权重/50%选中率,如需自定义比例,直接编辑 weights 数值即可。")


def main():
    p = argparse.ArgumentParser(description="问卷链接解析 & config.json 生成工具")
    p.add_argument("--url", required=True, help="问卷链接")
    p.add_argument("--out", default="config.json", help="输出配置文件路径(默认 config.json)")
    p.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    p.add_argument("--ai", action="store_true", help="调用 DeepSeek API 生成建议比例")
    p.add_argument("--model", default="deepseek-chat", help="DeepSeek 模型名(默认 deepseek-chat)")
    p.add_argument("--base-url", default="https://api.deepseek.com", help="API 地址(默认官方)")
    p.add_argument("--key", default=None, help="API Key(默认读环境变量 DEEPSEEK_API_KEY 或同目录 .env)")
    args = p.parse_args()

    pw, browser = launch_browser(not args.headed)
    try:
        context = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
        page = context.new_page()

        print(f"[信息] 正在打开: {args.url}")
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)

        try:
            start_btn = page.get_by_text("开始作答", exact=True).first
            if start_btn.is_visible(timeout=3000):
                start_btn.click()
                print("[信息] 已点击“开始作答”")
                time.sleep(2)
        except Exception:  # noqa: BLE001
            pass

        struct = page.evaluate(COLLECT_JS)
        questions = struct.get("questions") or []
        if not questions:
            print("[错误] 未能识别到题目,可能页面结构不兼容或问卷已结束。")
            sys.exit(1)

        print_questions(questions)
        rules = build_rules(questions)
        print(f"\n[信息] 共 {len(questions)} 道题,已生成 {len(rules)} 条规则")

        if args.ai:
            api_key = load_api_key(args)
            if not api_key:
                print("[警告] 未找到 DEEPSEEK_API_KEY(环境变量或 .env),跳过 AI 建议,使用默认等权重。")
            else:
                try:
                    print(f"[信息] 正在调用 {args.model} 生成建议比例(约需 10-60 秒)...")
                    prompt = build_ai_prompt(questions)
                    ai_text = call_llm(args.base_url, args.model, api_key, prompt)
                    rules = apply_ai_proportions(questions, rules, ai_text)
                    print_ai_summary(questions, rules)
                except Exception as e:  # noqa: BLE001
                    print(f"[警告] AI 调用失败({e}),已回退为默认等权重。")

        write_config(args.out, args.url, rules)
    finally:
        try:
            browser.close()
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
