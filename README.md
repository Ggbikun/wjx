# 问卷星自动填写工具 (wjx-automation)

基于 Playwright 的问卷星自动填写脚本,支持:

- 自动识别题型:单选、多选、填空、下拉、矩阵(量表)、评分星;
- 按权重比例填写:单选按占比分布,多选按每项选中率;
- 批量提交(`count`),结束后输出每题实际分布统计;
- 自动解析问卷链接生成配置(`parse_survey.py`),可选调用 DeepSeek 生成建议比例与填空题答案池;
- 支持多页问卷自动翻页;仅在失败/未填时截图。

## 目录结构

- `wjx-automation/fill_wjx.js` — JavaScript(Node.js + Playwright)版本
- `wjx-automation/python/` — Python 版本
  - `fill_wjx.py` — 填写脚本
  - `parse_survey.py` — 问卷解析 + 配置生成(支持 `--ai` 调 DeepSeek)
  - `使用手册.md` — 完整使用说明

## 快速开始(Python 版)

```bash
cd wjx-automation/python
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 解析问卷并生成配置(可选 --ai 让 DeepSeek 生成建议比例)
python parse_survey.py --url "问卷链接" --out config.json --ai

# 填写(先 --headed 试一次,再 --count N 批量)
python fill_wjx.py --config config.json --headed
```

详细步骤见 [使用手册.md](wjx-automation/python/使用手册.md)。

## 使用须知

本工具仅适用于填写自己创建或已获授权的问卷。请勿用于伪造调查数据、刷票或干扰他人问卷;问卷星等平台有反作弊机制,滥用后果自负。

API Key 等敏感信息请放在 `wjx-automation/python/.env`(已被 git 忽略),不要提交到仓库。
