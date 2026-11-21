# 重症医学每日文献推送 — 部署说明

这份仓库是在 [yzbcs/Daily-Digest-Assistant](https://github.com/yzbcs/Daily-Digest-Assistant)（MIT）基础上改造的，
把文献来源从 arXiv 换成了 PubMed，并按临床口径重写了解读模板。

## 为什么必须换源

| 来源 | 近 7 天「critical care」相关命中 |
|---|---|
| arXiv | **0 篇** |
| PubMed（Europe PMC 统计） | 560 篇 |

arXiv 收录的是计算机、物理、数理方向，临床医学文献基本不在其中。不换源的话，日报每天都是空的。

## 改了什么

| 文件 | 改动 |
|---|---|
| `fetchers/pubmed_fetcher.py` | 新增文献源。主源走 NCBI E-utilities（esearch + efetch），备用源 Europe PMC；关键词限定 `[Title/Abstract]`，日期用 `[EDAT]` 收录日期，跳过无摘要记录并按 PMID/DOI 去重 |
| `main.py` | 新增 `paper_source: pubmed` 分支；抓取失败时生成占位页而不会让 workflow 变红 |
| `llm/filter_and_summarize.py` | 新增 `prompt_profile: clinical` 口径：按「研究设计与人群 / 干预与主要结局 / 结论与局限」解读，禁止编造摘要外的数字 |
| `templates/email.html` | 无小红书数据时自动单栏，不再留半页空白 |
| `.github/workflows/daily.yml` | 归档索引正则支持 PubMed 来源 |
| `config.yml` | 重症医学关键词、回溯天数、临床口径 |

## 上线三步

### 1. Fork 并在自己仓库启用 Actions

Fork 之后进 **Actions** 标签页，点 **I understand my workflows, go ahead and enable them**
（GitHub 默认不在 fork 上运行 workflow，不点这一步什么都不会发生）。

### 2. 填 4 个 Secret

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | 值从哪来 |
|---|---|
| `LLM_API_KEY` | DeepSeek 开放平台的 API key（推荐，国内快且便宜）；也可用智谱 / 通义 / Kimi |
| `EMAIL_USER` | 发件邮箱地址 |
| `EMAIL_PASS` | 邮箱**授权码**，不是登录密码 |
| `EMAIL_TO` | 收件邮箱 |

授权码获取：163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP → 生成授权码；Gmail → 两步验证 →
应用专用密码；QQ 邮箱 → 设置 → 账户 → 开启 SMTP。

> `XHS_COOKIE` 不用填。没填就会跳过小红书抓取，页面自动变单栏。

### 3. 手动试跑一次

**Actions → Daily Paper Digest → Run workflow**。跑完检查三处：
邮箱是否收到、`preview.html` 是否是正确内容、`docs/daily/` 是否多出当天归档。

之后每天北京时间 12:00 自动运行（`.github/workflows/daily.yml` 里 `cron: "0 4 * * *"`，UTC）。

## 本地试跑（不发邮件、不需要 key）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
LLM_API_KEY=dummy .venv/bin/python main.py --dry-run
open preview.html
```

`--dry-run` 只渲染不发送；`LLM_API_KEY=dummy` 会跳过模型调用、直接取前 N 篇，
用来验证抓取和排版是否正常。

> 需要 Python 3.10+（代码用了 `X | None` 写法）。

## 调参

`config.yml` 里最常改的四项：

| 配置 | 作用 | 建议 |
|---|---|---|
| `keywords` | 检索词，OR 关系，必须是英文 | 加你自己的细分方向，如 `acute pancreatitis`、`ARDS`、`fluid responsiveness` |
| `lookback_days` | 回看几天（按收录日期） | 2~3；周一跑要覆盖周末，别设 1 |
| `min_score` | 相关性门槛 1-10 | 7 比较合适；嫌少降到 6，嫌杂升到 8 |
| `candidate_pool` | 送进模型的候选数 | 50 左右；超过 80 单次 prompt 会偏长 |

想只看核心期刊时启用白名单（注释里有示例）：重症方向可以用 *Critical care*、
*Intensive care medicine*、*American journal of respiratory and critical care medicine*、
*Chest*、*Critical care medicine*。

## 已知限制

- **卡片上的日期是电子发表日期，不是入库日期。** 所以会出现「今天是 11-21，某篇显示 2026-09-22」
  ——那是它的在线发表日期，而它是在最近两天才进 PubMed 的。日报的筛选依据始终是收录日期。
- **不过滤证据等级。** 病例报告、动物实验、纯机制研究都可能出现在候选里，
  靠 `prompt_profile: clinical` 让模型按「是否涉及重症人群、是否报告临床结局」降分，不是硬过滤。
- **模型解读只基于摘要。** 摘要没写的设计细节和局限不会出现，需要自己去读全文。
- **成本**：按 44 篇候选、约 1.2 万输入 token 估算，deepseek-chat 每天在**几分钱**量级，
  一个月几毛到一两块。以实际账单为准。

## 许可

上游为 MIT（见 `LICENSE`）。本改造同样以 MIT 提供，请保留上游版权声明。
