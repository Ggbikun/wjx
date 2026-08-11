#!/usr/bin/env node
'use strict';

/**
 * 问卷星自动填写脚本 (Wenjuanxing Auto-Fill)
 *
 * 基于 Playwright 驱动本机 Chrome / Edge,自动打开问卷、按规则填写并提交。
 * 支持题型:单选题、多选题、填空题、下拉题、矩阵题(量表)、评分星。
 *
 * 使用须知:
 * 本脚本仅适用于填写自己创建或已获得授权的问卷(如问卷测试、批量录入)。
 * 请勿用于伪造调查数据、刷票或干扰他人问卷;相关平台有反作弊机制,后果自负。
 */

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// 加载 Playwright:优先本地 node_modules,其次 Codex 运行时自带模块
// ---------------------------------------------------------------------------
const RUNTIME_NODE_MODULES =
  'C:/Users/江建锋/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';

let playwright = null;
for (const p of [
  path.join(process.cwd(), 'node_modules', 'playwright'),
  path.join(__dirname, 'node_modules', 'playwright'),
  path.join(RUNTIME_NODE_MODULES, 'playwright'),
]) {
  try {
    playwright = require(p);
    break;
  } catch (_) {
    /* 尝试下一个路径 */
  }
}
if (!playwright) {
  console.error('[错误] 未找到 Playwright 模块,请先运行: npm install playwright');
  process.exit(1);
}
const { chromium } = playwright;

// ---------------------------------------------------------------------------
// 默认配置与命令行参数
// ---------------------------------------------------------------------------
const DEFAULT_CONFIG = {
  url: '',
  count: 1,
  headless: true,
  delay_min_ms: 300,
  delay_max_ms: 1200,
  timeout_ms: 30000,
  rules: [],
  unmatched: 'random', // 'random' | 'skip'
  random_seed: null,
  screenshots_dir: 'screenshots',
  shots_success: false, // 是否在提交成功后截图(默认只截失败/未填/调试图)
  debug: false,
  stats: false,
};

function printHelp() {
  console.log(`
问卷星自动填写脚本

用法:
  node fill_wjx.js --config config.json [--headed] [--count N] [--url URL]

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
  --debug           输出问卷结构调试信息
  --stats           输出每题答案分布统计(默认 count>1 时自动输出)
  -h, --help        显示本帮助
`);
}

function parseArgs(argv) {
  const cli = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    switch (a) {
      case '--config': cli.configPath = next(); break;
      case '--url': cli.url = next(); break;
      case '--count': cli.count = parseInt(next(), 10); break;
      case '--headless': cli.headless = true; cli.headlessSet = true; break;
      case '--headed': cli.headless = false; cli.headlessSet = true; break;
      case '--delay': {
        const v = String(next()).split('-');
        cli.delay_min_ms = parseInt(v[0], 10) || DEFAULT_CONFIG.delay_min_ms;
        cli.delay_max_ms = parseInt(v[1] || v[0], 10) || cli.delay_min_ms;
        break;
      }
      case '--timeout': cli.timeout_ms = parseInt(next(), 10); break;
      case '--seed': cli.random_seed = parseInt(next(), 10); break;
      case '--shots': cli.screenshots_dir = next(); break;
      case '--debug': cli.debug = true; break;
      case '--stats': cli.stats = true; break;
      case '-h':
      case '--help':
        printHelp();
        process.exit(0);
        break;
      default:
        break;
    }
  }
  return cli;
}

function loadConfig(cli) {
  let fileCfg = {};
  if (cli.configPath) {
    if (!fs.existsSync(cli.configPath)) {
      console.error(`[错误] 配置文件不存在: ${cli.configPath}`);
      process.exit(1);
    }
    fileCfg = JSON.parse(fs.readFileSync(cli.configPath, 'utf8'));
  }
  const cfg = { ...DEFAULT_CONFIG, ...fileCfg };
  if (cli.url) cfg.url = cli.url;
  if (cli.count !== undefined) cfg.count = cli.count;
  if (cli.headlessSet) cfg.headless = cli.headless;
  if (cli.delay_min_ms !== undefined) cfg.delay_min_ms = cli.delay_min_ms;
  if (cli.delay_max_ms !== undefined) cfg.delay_max_ms = cli.delay_max_ms;
  if (cli.timeout_ms !== undefined) cfg.timeout_ms = cli.timeout_ms;
  if (cli.random_seed !== undefined) cfg.random_seed = cli.random_seed;
  if (cli.screenshots_dir) cfg.screenshots_dir = cli.screenshots_dir;
  if (cli.debug) cfg.debug = true;
  if (cli.stats) cfg.stats = true;
  return cfg;
}

// ---------------------------------------------------------------------------
// 随机数工具(支持种子,便于复现)
// ---------------------------------------------------------------------------
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function randInt(rng, min, max) {
  return min + Math.floor(rng() * (max - min + 1));
}

function shuffle(rng, arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = randInt(rng, 0, i);
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function delay(rng, cfg) {
  const ms = randInt(rng, cfg.delay_min_ms, cfg.delay_max_ms);
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// 浏览器启动:优先系统 Chrome,其次 Edge
// ---------------------------------------------------------------------------
function findSystemBrowser() {
  const candidates = [
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env['PROGRAMFILES(X86)'] && path.join(process.env['PROGRAMFILES(X86)'], 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env['PROGRAMFILES(X86)'] && path.join(process.env['PROGRAMFILES(X86)'], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  ].filter(Boolean);
  return candidates.find((p) => fs.existsSync(p)) || null;
}

async function launchBrowser(headless) {
  const common = {
    headless,
    args: ['--disable-blink-features=AutomationControlled', '--lang=zh-CN', '--window-size=1280,800'],
  };
  const errors = [];
  for (const channel of ['chrome', 'msedge']) {
    try {
      return await chromium.launch({ ...common, channel });
    } catch (e) {
      errors.push(`${channel}: ${String(e.message).split('\n')[0]}`);
    }
  }
  const exe = findSystemBrowser();
  if (exe) {
    try {
      return await chromium.launch({ ...common, executablePath: exe });
    } catch (e) {
      errors.push(`executablePath: ${String(e.message).split('\n')[0]}`);
    }
  }
  throw new Error('无法启动浏览器:\n' + errors.join('\n'));
}

// ---------------------------------------------------------------------------
// 问卷结构识别(在浏览器内执行)
// ---------------------------------------------------------------------------
async function collectStructure(page) {
  const data = await page.evaluate(() => {
    const text = (el) =>
      el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
    const labelOf = (input) => {
      // 兼容多种模板:label 包裹、ui-radio/ui-checkbox 容器、li 列表
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

    // 优先按“每题一个容器”的常见模板识别
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

      // 注意:wjx 手机版模板所有 radio/checkbox 输入框都是 display:none,
      // 因此这里不做可见性过滤,只排除 type=hidden
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
  });
  return data;
}

// ---------------------------------------------------------------------------
// 规则匹配与填写
// ---------------------------------------------------------------------------
function findRule(rules, questionText) {
  for (const r of rules || []) {
    if (!r.match || r.match === '*') return r;
    try {
      if (new RegExp(r.match, 'i').test(questionText)) return r;
    } catch (_) {
      /* 无效正则忽略 */
    }
  }
  return null;
}

// 点击某个选项:输入框可能是 display:none(如 wjx 手机版模板),
// 依次尝试点击可见的 label / a.jqradio / 外层 wrapper,最后强制勾选输入框本身
async function clickOption(c, inputType, idx) {
  const input = c.locator(`input[type="${inputType}"]`).nth(idx - 1);
  const candidates = [
    input.locator('xpath=ancestor::label[1]'), // 经典模板:label 包裹
    input.locator('xpath=following-sibling::a[1]'), // wjx 手机版:a.jqradio / a.jqcheckbox
    input.locator('xpath=ancestor::span[contains(@class,"wrapper")][1]'), // wjx 手机版:外层 span
    input.locator(
      'xpath=ancestor::div[contains(@class,"ui-radio") or contains(@class,"ui-checkbox")][1]//div[contains(@class,"label")][1]'
    ),
    input.locator('xpath=following-sibling::div[contains(@class,"label")][1]'),
  ];
  for (const loc of candidates) {
    try {
      if (await loc.isVisible({ timeout: 1000 })) {
        await loc.click({ timeout: 2000 });
      }
    } catch (_) {
      continue;
    }
    try {
      if (await input.isChecked()) return true;
    } catch (_) {
      /* 继续尝试下一个候选 */
    }
  }
  try {
    await input.check({ force: true, timeout: 3000 });
    return await input.isChecked();
  } catch (_) {
    return false;
  }
}

async function clickInput(locator) {
  try {
    await locator.click({ timeout: 3000 });
  } catch (_) {
    await locator.click({ force: true, timeout: 3000 });
  }
}

async function fillTextInput(loc, value) {
  try {
    await loc.fill(String(value), { timeout: 3000 });
    return;
  } catch (_) {
    /* 隐藏输入框用 JS 兜底 */
  }
  await loc.evaluate(
    (el, v) => {
      el.value = v;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    },
    String(value)
  );
}

function resolveOptionIndex(value, options, rng) {
  // 返回 1-based 选项序号;失败返回 null
  if (value === 'random') return randInt(rng, 1, options.length);
  if (typeof value === 'number') return value >= 1 && value <= options.length ? value : null;
  const s = String(value).trim();
  if (/^\d+$/.test(s)) {
    const idx = parseInt(s, 10);
    if (idx >= 1 && idx <= options.length) return idx;
  }
  const exact = options.findIndex((o) => o.label === s);
  if (exact >= 0) return exact + 1;
  // 模糊匹配时优先最短标签,避免“满意”被“非常不满意”抢先匹配
  let best = -1;
  let bestLen = Infinity;
  options.forEach((o, i) => {
    if (o.label.includes(s) || s.includes(o.label)) {
      if (o.label.length < bestLen) {
        best = i;
        bestLen = o.label.length;
      }
    }
  });
  if (best >= 0) return best + 1;
  return null;
}

function randomTextFor(q, rng, subNo) {
  const first = q.inputs[0] || {};
  const hint = `${first.placeholder || ''} ${q.text || ''}`;
  if (first.type === 'email' || /邮箱|email/i.test(hint)) return `tester${randInt(rng, 1000, 9999)}@example.com`;
  if (first.type === 'tel' || /手机|电话|手机号/i.test(hint)) return `13${randInt(rng, 100000000, 999999999)}`;
  if (first.type === 'number') return String(randInt(rng, 1, 100));
  if (first.type === 'date') return `2026-0${randInt(rng, 1, 9)}-${String(randInt(rng, 1, 28)).padStart(2, '0')}`;
  if (/姓名|名字/i.test(hint)) return `测试用户${subNo}`;
  if (first.kind === 'textarea') return `这是第 ${subNo} 份自动化测试填写,内容仅供参考。`;
  return `测试${randInt(rng, 1000, 9999)}`;
}

function randomMatrixCol(rng, count) {
  if (count <= 0) return 1;
  // 半数概率选中间列,更接近真实填写习惯
  if (rng() < 0.5) {
    const mid = Math.floor((count + 1) / 2);
    return Math.max(1, Math.min(count, mid + randInt(rng, -1, 1)));
  }
  return randInt(rng, 1, count);
}

function resolveMatrixCol(value, row, headers, rng) {
  if (value === 'random' || value === undefined || value === null) return randomMatrixCol(rng, row.count);
  if (typeof value === 'number') return Math.max(1, Math.min(row.count, value));
  const s = String(value).trim();
  if (/^\d+$/.test(s)) return Math.max(1, Math.min(row.count, parseInt(s, 10)));
  // 数据行的列单元格通常只有输入框(标签为空),此时用表头作为列标签
  const rowLabels = row.colLabels && row.colLabels.some((l) => l) ? row.colLabels : headers;
  const labels = rowLabels || [];
  const exact = labels.findIndex((l) => l === s);
  if (exact >= 0) return exact + 1;
  const prefix = labels.findIndex((l) => l.startsWith(s));
  if (prefix >= 0) return prefix + 1;
  return randomMatrixCol(rng, row.count);
}

// 按权重随机选一个选项,返回 1-based 序号
// weights: { "选项文字": 权重 } 或 { "1": 权重, "2": 权重 },权重自动归一化
function weightedIndex(weights, options, rng) {
  const entries = [];
  for (let i = 0; i < options.length; i++) {
    const o = options[i];
    let w = null;
    if (weights[o.label] !== undefined) w = Number(weights[o.label]);
    else if (weights[String(i + 1)] !== undefined) w = Number(weights[String(i + 1)]);
    if (w !== null && w > 0) entries.push({ idx: i + 1, w });
  }
  if (!entries.length) return null;
  const total = entries.reduce((s, e) => s + e.w, 0);
  let r = rng() * total;
  for (const e of entries) {
    r -= e.w;
    if (r <= 0) return e.idx;
  }
  return entries[entries.length - 1].idx;
}

// 多选题按权重随机勾选:每个选项按权重(百分比 0-100,0-1 视为概率小数)
// 独立决定是否选中;保证至少选一个(一个都没选中时选权重最高的选项)。
// 返回 1-based 序号数组
function weightedCheckboxPicks(weights, options, rng) {
  const picks = [];
  let maxW = -1;
  let maxIdx = -1;
  for (let i = 0; i < options.length; i++) {
    const o = options[i];
    let w = null;
    if (weights[o.label] !== undefined) w = Number(weights[o.label]);
    else if (weights[String(i + 1)] !== undefined) w = Number(weights[String(i + 1)]);
    if (w === null || w <= 0) continue;
    if (w <= 1) w *= 100;
    if (w > maxW) {
      maxW = w;
      maxIdx = i + 1;
    }
    if (rng() * 100 < w) picks.push(i + 1);
  }
  if (!picks.length && maxIdx > 0) picks.push(maxIdx);
  return picks;
}

async function fillQuestion(page, containerSelector, q, value, rng, subNo) {
  const c = page.locator(containerSelector).nth(q.rawIndex);
  const summary = [];
  const chosen = [];
  let filled = false;

  switch (q.type) {
    case 'radio': {
      let idx = null;
      if (value && value.__weights) {
        idx = weightedIndex(value.__weights, q.options, rng);
        if (!idx) throw new Error('权重未匹配到任何选项');
      } else {
        const raw = Array.isArray(value) ? value[0] : value;
        idx = resolveOptionIndex(raw, q.options, rng);
        if (!idx) throw new Error(`无法匹配选项: ${JSON.stringify(raw)}`);
      }
      const ok = await clickOption(c, 'radio', idx);
      if (!ok) throw new Error(`点击选项失败: ${q.options[idx - 1].label || idx}`);
      filled = true;
      const label = q.options[idx - 1].label || `选项${idx}`;
      summary.push(label);
      chosen.push(label);
      break;
    }

    case 'checkbox': {
      let picks;
      if (value && value.__weights) {
        picks = weightedCheckboxPicks(value.__weights, q.options, rng);
      } else if (value === 'random') {
        picks = shuffle(rng, q.options.map((o) => o.index)).slice(0, randInt(rng, 1, Math.min(3, q.options.length)));
      } else {
        picks = Array.isArray(value) ? value : [value];
      }
      const chosenLabels = [];
      for (const v of picks) {
        const idx = resolveOptionIndex(v, q.options, rng);
        if (!idx) continue;
        const ok = await clickOption(c, 'checkbox', idx);
        if (!ok) continue;
        filled = true;
        const label = q.options[idx - 1].label || `选项${idx}`;
        chosenLabels.push(label);
      }
      if (!chosenLabels.length) throw new Error(`无法匹配选项: ${JSON.stringify(value)}`);
      summary.push(chosenLabels.join('、'));
      chosen.push(...chosenLabels);
      break;
    }

    case 'select': {
      const opts = q.selectOptions;
      let label = null;
      if (value && value.__weights) {
        const usable = opts.filter((o) => o.label && o.value !== '');
        const idx = weightedIndex(value.__weights, usable, rng);
        if (!idx) throw new Error('权重未匹配到任何下拉选项');
        label = usable[idx - 1].label;
      } else if (value === 'random') {
        const usable = opts.filter((o) => o.label && o.value !== '');
        if (!usable.length) throw new Error('下拉框无可选选项');
        label = usable[randInt(rng, 0, usable.length - 1)].label;
      } else if (typeof value === 'number') {
        const o = opts.find((x) => x.index + 1 === value);
        if (o) label = o.label;
      } else {
        const s = String(value).trim();
        if (/^\d+$/.test(s)) {
          const o = opts.find((x) => x.index + 1 === parseInt(s, 10));
          if (o) label = o.label;
        } else {
          const exact = opts.find((x) => x.label === s);
          if (exact) {
            label = exact.label;
          } else {
            const incl = opts
              .filter((x) => x.label.includes(s) || s.includes(x.label))
              .sort((a, b) => a.label.length - b.label.length)[0];
            if (incl) label = incl.label;
          }
        }
      }
      if (!label) throw new Error(`无法匹配下拉选项: ${JSON.stringify(value)}`);
      for (let i = 0; i < (q.selectCount || 1); i++) {
        const sel = c.locator('select').nth(i);
        try {
          await sel.selectOption({ label });
        } catch (_) {
          await sel.selectOption({ label }, { force: true });
        }
      }
      filled = true;
      summary.push(label);
      chosen.push(label);
      break;
    }

    case 'text': {
      const vals = Array.isArray(value)
        ? value
        : [String(value).replace(/\{n\}/g, String(subNo))];
      for (let i = 0; i < q.inputs.length; i++) {
        const input = q.inputs[i];
        let v;
        if (vals.length > 1 && q.inputs.length === 1) {
          // 答案池:多份问卷轮换使用
          v = vals[(subNo - 1) % vals.length];
        } else {
          v = vals[i] !== undefined ? vals[i] : value === 'random' ? randomTextFor(q, rng, subNo) : '';
        }
        if (v === '') continue;
        const loc = c.locator(input.kind === 'textarea' ? 'textarea' : 'input').nth(input.tagIndex);
        await fillTextInput(loc, v);
        filled = true;
        summary.push(String(v));
        chosen.push(String(v));
      }
      break;
    }

    case 'matrix_radio':
    case 'matrix_checkbox': {
      for (const row of q.matrixRows) {
        let col;
        if (value === 'random') {
          col = randomMatrixCol(rng, row.count);
        } else if (typeof value === 'object' && !Array.isArray(value)) {
          const v = value[row.rowText] ?? value[row.name] ?? value['*'];
          col = resolveMatrixCol(v, row, q.matrixHeaders, rng);
        } else {
          col = resolveMatrixCol(value, row, q.matrixHeaders, rng);
        }
        const tr = c.locator('tr').nth(row.trIndex);
        await clickInput(tr.locator('input').nth(col - 1));
        filled = true;
        const cell = `${row.rowText}:${col}列`;
        summary.push(cell);
        chosen.push(cell);
      }
      break;
    }

    case 'star': {
      const idx =
        value === 'random'
          ? Math.min(q.starCount, Math.max(1, Math.round(q.starCount * (rng() * 0.6 + 0.3))))
          : Math.min(q.starCount, Math.max(1, parseInt(String(value), 10) || 1));
      await clickInput(c.locator('.star').nth(idx - 1));
      filled = true;
      const cell = `第${idx}颗星`;
      summary.push(cell);
      chosen.push(cell);
      break;
    }

    default:
      throw new Error(`暂不支持该题型(type=${q.type})`);
  }

  return { summary: summary.join(' | ') || '(已填写)', filled, chosen };
}

// ---------------------------------------------------------------------------
// 提交与结果验证
// ---------------------------------------------------------------------------
async function submitSurvey(page, cfg, rng) {
  const selectors = [
    '#submit_button',
    'button[type="submit"]',
    'input[type="submit"]',
    '.btn-submit',
    '.submitbtn a, .submitbtn input, .submitbtn button',
    'a:has-text("提交")',
    'button:has-text("提交")',
  ];
  let clicked = false;
  for (const sel of selectors) {
    const loc = page.locator(sel).first();
    try {
      if (await loc.isVisible({ timeout: 1000 })) {
        await loc.click();
        clicked = true;
        break;
      }
    } catch (_) {
      /* 继续尝试下一个选择器 */
    }
  }
  if (!clicked) {
    try {
      const any = page.getByText('提交', { exact: false }).first();
      if (await any.isVisible({ timeout: 1500 })) {
        await any.click();
        clicked = true;
      }
    } catch (_) {
      /* 忽略 */
    }
  }
  if (!clicked) {
    console.warn('[警告] 未找到提交按钮');
    return false;
  }

  await delay(rng, cfg);

  // 处理“确认提交”之类的弹窗
  try {
    const confirm = page
      .locator('.layui-layer-btn0, .layui-layer-btn a:has-text("确定"), button:has-text("确定"), a:has-text("确定")')
      .first();
    if (await confirm.isVisible({ timeout: 2500 })) await confirm.click();
  } catch (_) {
    /* 没有弹窗 */
  }

  try {
    await page.waitForFunction(
      () => {
        const t = (document.body && document.body.innerText) || '';
        return (
          /提交成功|答卷已提交|提交完成|感谢您的参与|答卷已完成/.test(t) ||
          /(complete|finish|success)(\.aspx|\.html|\?|$)/i.test(location.href)
        );
      },
      null,
      { timeout: cfg.timeout_ms }
    );
    return true;
  } catch (_) {
    return false;
  }
}

async function detectCaptcha(page) {
  try {
    const iframes = await page.locator('iframe').count();
    const t = await page.evaluate(() => (document.body.innerText || '').slice(0, 600));
    return iframes > 0 || /滑块|拖动|验证码|请完成验证/.test(t);
  } catch (_) {
    return false;
  }
}

async function takeShot(page, cfg, name) {
  try {
    const dir = path.resolve(cfg.screenshots_dir);
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `${name}-${Date.now()}.png`);
    await page.screenshot({ path: file });
    console.log(`[截图] ${file}`);
  } catch (_) {
    /* 截图失败不影响主流程 */
  }
}

// ---------------------------------------------------------------------------
// 单次填写流程
// ---------------------------------------------------------------------------
async function runOne(page, cfg, subNo, rng) {
  const t0 = Date.now();
  await page.goto(cfg.url, { waitUntil: 'domcontentloaded', timeout: cfg.timeout_ms });
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  await delay(rng, cfg);

  // 部分问卷进入后需要先点击“开始作答”
  try {
    const startBtn = page.getByText('开始作答', { exact: true }).first();
    if (await startBtn.isVisible({ timeout: 3000 })) {
      await startBtn.click();
      console.log('[信息] 已点击“开始作答”');
      await delay(rng, cfg);
    }
  } catch (_) {
    /* 无此按钮 */
  }

  const struct = await collectStructure(page);
  if (!struct.questions.length) {
    console.error('[错误] 未能识别到问卷题目,可能页面结构不兼容、问卷已结束或需要登录。');
    await takeShot(page, cfg, `debug-${subNo}`);
    return { ok: false, stats: {} };
  }
  console.log(`[信息] 识别到 ${struct.questions.length} 道题`);
  if (cfg.debug) console.log(JSON.stringify(struct, null, 2));

  const unfilled = [];
  const stats = {};
  for (const q of struct.questions) {
    const rule = findRule(cfg.rules, q.text);
    let value;
    if (rule) {
      if (rule.weights && Object.keys(rule.weights).length) {
        value = { __weights: rule.weights };
      } else {
        value = rule.value;
      }
    } else if (cfg.unmatched === 'random') {
      value = 'random';
    }
    if (value === undefined) {
      console.log(`[${q.n}] ${q.text || '(无题目标题)'} -> 跳过`);
      continue;
    }
    try {
      const { summary, filled, chosen } = await fillQuestion(page, struct.containerSelector, q, value, rng, subNo);
      console.log(`[${q.n}] ${q.text || '(无题目标题)'} -> ${summary}`);
      if (!filled) unfilled.push(q);
      for (const ch of chosen) {
        if (!stats[q.n]) stats[q.n] = { text: q.text || '', counts: {} };
        stats[q.n].counts[ch] = (stats[q.n].counts[ch] || 0) + 1;
      }
    } catch (e) {
      console.warn(`[${q.n}] ${q.text || '(无题目标题)'} 填写失败: ${e.message}`);
      unfilled.push(q);
    }
    await delay(rng, cfg);
  }

  if (unfilled.length) {
    const names = unfilled.map((q) => `${q.n}.${q.text || '(无题目标题)'}`).join('; ');
    console.warn(`[警告] 有 ${unfilled.length} 道题未填成功,为避免提交卡在必填校验,本次不提交:`);
    console.warn(`       ${names}`);
    await takeShot(page, cfg, `unfilled-${subNo}`);
    return { ok: false, stats };
  }

  // 滚动到底部,等待提交按钮出现
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await delay(rng, cfg);

  const ok = await submitSurvey(page, cfg, rng);
  if (!ok) {
    await takeShot(page, cfg, 'fail');
    const cap = await detectCaptcha(page);
    if (cap) {
      console.warn('[提示] 页面出现验证码/滑块,请改用 --headed 模式手动完成,或稍后重试。');
    } else {
      console.warn('[警告] 未检测到提交成功提示,请查看截图确认(可能是必填题未填或页面结构变化)。');
    }
    return { ok: false, stats };
  }
  if (cfg.shots_success) await takeShot(page, cfg, 'success');
  console.log(`[信息] 提交成功,耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  return { ok: true, stats };
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------
function printDistribution(allStats, total) {
  console.log(`\n===== 答案分布统计(共 ${total} 份) =====`);
  const keys = Object.keys(allStats).sort((a, b) => Number(a) - Number(b));
  for (const key of keys) {
    const { text, counts } = allStats[key];
    const parts = Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([v, c]) => `${v} ${((c / total) * 100).toFixed(1)}%(${c})`);
    console.log(`${key}.${text || ''}: ${parts.join(', ')}`);
  }
}

async function main() {
  const cli = parseArgs(process.argv.slice(2));
  const cfg = loadConfig(cli);
  if (!cfg.url) {
    console.error('[错误] 请通过 --url 或配置文件提供问卷链接。运行 --help 查看帮助。');
    process.exit(1);
  }
  const rng = mulberry32(cfg.random_seed !== null && cfg.random_seed !== undefined ? cfg.random_seed : Date.now() ^ 0x9e3779b9);

  console.log(`[信息] 问卷: ${cfg.url}`);
  console.log(`[信息] 提交次数: ${cfg.count},模式: ${cfg.headless ? '无头' : '有头'}`);

  const browser = await launchBrowser(cfg.headless);
  const context = await browser.newContext({
    locale: 'zh-CN',
    viewport: { width: 1280, height: 800 },
  });
  const page = await context.newPage();

  let okCount = 0;
  const allStats = {};
  for (let i = 1; i <= cfg.count; i++) {
    console.log(`\n===== 第 ${i}/${cfg.count} 次提交 =====`);
    const res = await runOne(page, cfg, i, rng);
    if (res && res.ok) okCount += 1;
    for (const key of Object.keys((res && res.stats) || {})) {
      if (!allStats[key]) allStats[key] = { text: res.stats[key].text, counts: {} };
      for (const [v, c] of Object.entries(res.stats[key].counts)) {
        allStats[key].counts[v] = (allStats[key].counts[v] || 0) + c;
      }
    }
  }

  await browser.close();
  if (cfg.count > 1 || cfg.stats) printDistribution(allStats, cfg.count);
  console.log(`\n[完成] 成功 ${okCount}/${cfg.count} 次。`);
  if (okCount < cfg.count) process.exitCode = 1;
}

main().catch((e) => {
  console.error('[错误]', e.message || e);
  process.exit(1);
});
