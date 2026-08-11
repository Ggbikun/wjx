# 问卷星自动填写脚本(Python 版)

与仓库根目录 `wjx-automation/fill_wjx.js`(JavaScript 版)功能一致的 Python 实现,基于 Playwright。

> 完整的分步操作说明(安装、解析、AI 比例、批量填写、常见问题)见 [使用手册.md](使用手册.md)。

## 功能

- 自动识别题型:单选题、多选题、填空题、下拉题、矩阵题(量表)、评分星;
- `weights` 按比例填写:单选按占比分布、多选按每项选中率(总和不必为 100);
- 填空题支持答案池数组,多份提交自动轮换;
- `count` 批量提交,结束后输出每题实际分布统计;
- 只在失败/未填/调试时截图,成功默认不截图(`shots_success: true` 可开启);
- 提交前校验必填题,有未填成功的不提交,避免卡在页面校验。
- 支持多页问卷:自动识别"下一页/下一题"按钮并逐页填写。

## 换问卷:自动解析链接生成 config

换新问卷不需要手写 config,用自带的解析工具:

```bash
python parse_survey.py --url <问卷链接> --out config.json
```

工具会自动:

1. 打开问卷页面,识别每道题的题型和选项;
2. 为每题生成默认规则:单选/下拉等权重、多选每项 50% 选中率、填空 random;
3. 自动避开条件选项——名字含"其他/请举例"的选项权重设为 0(选中会弹出条件填空);
4. 覆盖写回 config.json,原配置自动备份为 config.backup.json。

生成后直接运行填写脚本即可;要自定义比例,编辑 config.json 里的 `weights` 数值。

### 让 AI 生成建议比例(DeepSeek)

解析时加 `--ai`,会自动调用 DeepSeek 为每道题生成符合人群分布的比例:

```bash
python parse_survey.py --url <问卷链接> --out config.json --ai
```

API Key 配置(三选一,按优先级):

1. 命令行 `--key sk-xxxx`;
2. 环境变量 `DEEPSEEK_API_KEY`;
3. 同目录 `.env` 文件,内容为 `DEEPSEEK_API_KEY=sk-xxxx`(已被 gitignore,不会提交)。

其他参数:`--model`(默认 `deepseek-chat`,第三方中转可用其他模型名)、`--base-url`(默认官方接口)。

AI 生成规则说明:单选题按权重归一化到 100,多选按"每项被选中的概率"输出;含"其他/请举例"的条件选项一律强制权重 0,避免触发条件填空。调用失败会自动回退为等权重,不影响使用。

## 环境安装(PyCharm 中)

在 PyCharm 的 Terminal 里执行:

```bash
pip install -r requirements.txt
```

如果本机没有 Chrome/Edge,或想用 Playwright 自带浏览器内核:

```bash
playwright install chromium
```

脚本默认优先使用系统已安装的 Chrome/Edge,不需要下载内核。

## 使用

复制 `config.example.json` 为 `config.json`,填入问卷链接和答案规则后:

```bash
python fill_wjx.py --config config.json
```

首次建议加 `--headed` 打开浏览器观察效果。其他参数与 JS 版一致:

```text
--config <file>   配置文件路径
--url <url>       问卷链接(覆盖配置文件)
--count <n>       提交次数
--headed          有头模式
--headless        无头模式(默认)
--delay <min-max> 操作间隔,如 500-1500
--timeout <ms>    提交等待超时
--seed <n>        随机种子
--shots <dir>     截图目录
--debug           输出识别到的题目结构
--stats           强制输出分布统计(count>1 时默认自动输出)
```

## 配置格式

与 JS 版相同,可直接复用项目根目录的 `config.json`(路径用绝对路径或相对当前工作目录)。

```json
{
  "url": "https://www.wjx.cn/vm/xxxxxxxx.aspx",
  "count": 100,
  "rules": [
    { "match": "性别", "weights": { "男": 40, "女": 60 } },
    { "match": ".*爱好.*", "weights": { "阅读": 70, "运动": 50 } },
    { "match": "姓名", "value": ["用户甲", "用户乙", "用户丙"] }
  ]
}
```

## 使用须知

本脚本仅适用于填写自己创建或已获授权的问卷(如问卷测试、批量录入)。请勿用于伪造调查数据、刷票或干扰他人问卷,相关平台有反作弊机制,后果自负。
