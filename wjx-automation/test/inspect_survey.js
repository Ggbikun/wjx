#!/usr/bin/env node
'use strict';

/**
 * 问卷页面结构检查工具(开发用)
 * 用法: node inspect_survey.js [问卷URL]
 * 输出真实 DOM 中的容器、题目、选项、分页/提交按钮信息。
 */

const path = require('path');
const RUNTIME_NODE_MODULES =
  'C:/Users/江建锋/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
let playwright;
try {
  playwright = require(path.join(process.cwd(), 'node_modules', 'playwright'));
} catch (_) {
  playwright = require(path.join(RUNTIME_NODE_MODULES, 'playwright'));
}
const { chromium } = playwright;

const url = process.argv[2] || 'https://www.wjx.cn/vm/wmHDxPu.aspx';

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: 'chrome',
    args: ['--disable-blink-features=AutomationControlled', '--lang=zh-CN'],
  });
  const page = await browser.newPage({ locale: 'zh-CN', viewport: { width: 1280, height: 900 } });

  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});

  try {
    const b = page.getByText('开始作答', { exact: true }).first();
    if (await b.isVisible({ timeout: 3000 })) {
      await b.click();
      console.log('[*] 已点击"开始作答"');
    }
  } catch (_) {
    console.log('[*] 页面无"开始作答"按钮');
  }
  await page.waitForTimeout(4000);

  const info = await page.evaluate(() => {
    const txt = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
    const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
    const checks = Array.from(document.querySelectorAll('input[type="checkbox"]'));
    return {
      bodyStart: (document.body.innerText || '').slice(0, 120),
      fieldsets: document.querySelectorAll('fieldset').length,
      divFields: document.querySelectorAll('div.field, div[data-role="fieldcontain"]').length,
      fieldLabels: document.querySelectorAll('.field-label').length,
      qnrContainers: document.querySelectorAll('.qnr-question, .div_question').length,
      radioCount: radios.length,
      checkboxCount: checks.length,
      firstRadioDisplay: radios.length ? getComputedStyle(radios[0]).display : 'none-found',
      visibleRadios: radios.filter((r) => r.getBoundingClientRect().width > 0).length,
      visibleChecks: checks.filter((r) => r.getBoundingClientRect().width > 0).length,
      titles: Array.from(document.querySelectorAll('.field-label .topichtml')).slice(0, 25).map(txt),
      optionLabelsSample: Array.from(document.querySelectorAll('#div1 .label, #div2 .label, .ui-radio .label, .ui-checkbox .label'))
        .slice(0, 10)
        .map(txt),
      pageButtons: Array.from(document.querySelectorAll('a, button'))
        .filter((el) => /下一页|下一题|继续|开始/.test(txt(el)))
        .map(txt)
        .slice(0, 8),
      submitButtons: Array.from(document.querySelectorAll('a, button, input'))
        .filter((el) => /提交/.test(txt(el)) || (el.value || '').includes('提交'))
        .map((el) => txt(el) || el.value)
        .slice(0, 8),
      questionAreaSnippet: (document.getElementById('divQuestion') || document.body).innerHTML.slice(0, 1200),
    };
  });

  console.log(JSON.stringify(info, null, 2));
  await browser.close();
})().catch((e) => {
  console.error('[错误]', e.message);
  process.exit(1);
});
