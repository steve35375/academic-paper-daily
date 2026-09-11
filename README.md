# 学术文献每日自动检索邮件推送系统

一个可直接运行在 GitHub Actions 上的 Python 文献日报系统，默认每天北京时间 08:00 自动执行。

## 功能

- PubMed、arXiv、OpenAlex 三个数据库
- 配置文件管理关键词和检索策略
- 支持 `AND / OR / NOT / ()` 布尔表达式
- 默认滚动检索最近 24 小时
- DOI / PMID / arXiv ID 优先去重
- `data/sent_ids.json` 持久化已推送记录
- HTML 邮件：标题、作者、出版物、发表日期、影响因子、来源、摘要、原文链接
- SMTP：QQ、163、Gmail 均可配置
- GitHub Secrets 保存密码/API Key
- GitHub Actions 每日 UTC 00:00 自动运行，同时支持手动运行
- 日志输出到控制台和 `logs/run.log`

## 重要说明：影响因子

PubMed、arXiv 和 OpenAlex 的公共元数据接口并不等同于 Clarivate JCR 的 Journal Impact Factor 数据源。因此本项目不会伪造或“猜测”影响因子。

项目提供 `impact_factors.yaml`，由你手动维护：

```yaml
"Advanced Materials": "29.4"
"Acta Materialia": "9.7"
```

邮件中会显示匹配到的数值；未配置时显示“未配置”。

这样做可以避免把 CiteScore、OpenAlex 指标或其他指标误标成 JCR Impact Factor。

## 目录

```text
academic_paper_daily/
├── main.py
├── config.yaml
├── impact_factors.yaml
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── data/
│   └── sent_ids.json
└── .github/
    └── workflows/
        └── daily_paper.yml
```

## 1. 本地测试

建议 Python 3.11+。

```bash
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\activate
```

macOS/Linux：

```bash
source .venv/bin/activate
```

安装：

```bash
pip install -r requirements.txt
```

编辑 `config.yaml`：

- `search.queries`：关键词/布尔检索式
- `search.hours`：滚动时间范围，默认 24
- `sources.*.enabled`：启停数据库
- `email`：SMTP 主机、端口和收件人
- `impact_factors.yaml`：可选的 JCR IF 映射

本地运行前设置 SMTP 密码：

PowerShell：

```powershell
$env:SMTP_PASSWORD="你的SMTP授权码或应用专用密码"
$env:SMTP_USERNAME="你的邮箱"
$env:MAIL_FROM="你的邮箱"
$env:MAIL_TO="收件邮箱"
python main.py
```

macOS/Linux：

```bash
export SMTP_PASSWORD='你的SMTP授权码或应用专用密码'
export SMTP_USERNAME='你的邮箱'
export MAIL_FROM='你的邮箱'
export MAIL_TO='收件邮箱'
python main.py
```

### QQ 邮箱

典型配置：

```yaml
smtp_host: "smtp.qq.com"
smtp_port: 465
security: "ssl"
```

`SMTP_PASSWORD` 应填写 SMTP 授权码，而不是网页登录密码。

### 163 邮箱

```yaml
smtp_host: "smtp.163.com"
smtp_port: 465
security: "ssl"
```

同样建议使用 SMTP 授权码。

### Gmail

```yaml
smtp_host: "smtp.gmail.com"
smtp_port: 465
security: "ssl"
```

对于 Gmail，优先使用应用专用密码，不要把账户主密码写入代码。

## 2. 关键词和布尔检索

示例：

```yaml
queries:
  - '"amorphous alloy" AND ("machine learning" OR "data-driven")'
  - '("metal additive manufacturing" OR "3D printing") AND recycling'
  - '("LiFePO4" OR "LFP") AND leaching AND NOT graphite'
```

多条 `queries` 在最终结果中按 OR 关系处理；一篇文献只要匹配其中任意一个表达式即可。

检索字段：

- PubMed：将表达式交给 PubMed E-utilities。
- arXiv：转换为 arXiv API 搜索表达式，并再次在标题+摘要本地执行布尔判断。
- OpenAlex：先用关键词做候选发现，再在标题+摘要本地执行完整布尔判断。

这样可以尽可能让三套数据库的结果行为一致。

## 3. 时间范围

默认：

```yaml
search:
  hours: 24
```

表示每次运行都检索“当前时间向前 24 小时”。

也支持固定时间：

```yaml
search:
  start: "2026-09-10T00:00:00+00:00"
  end: "2026-09-11T00:00:00+00:00"
```

## 4. 去重

系统优先使用：

1. DOI
2. PMID
3. arXiv ID
4. 数据库内部 UID

去重文件：

```text
data/sent_ids.json
```

### 为什么 GitHub Actions 还需要提交这个文件？

GitHub Actions runner 是临时环境。如果仅在 runner 本地保存，下一天任务启动后文件会消失。

所以工作流最后会：

```text
git add data/sent_ids.json
git commit
git push
```

将状态写回默认分支，从而实现跨运行持久化。

因此仓库必须允许 GitHub Actions 写入 Contents。

## 5. GitHub Secrets

进入：

`GitHub 仓库 → Settings → Secrets and variables → Actions`

添加：

```text
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
MAIL_FROM
MAIL_TO
```

可选：

```text
NCBI_API_KEY
NCBI_EMAIL
OPENALEX_API_KEY
OPENALEX_MAILTO
```

建议：

- `SMTP_PASSWORD`：QQ/163 SMTP 授权码或 Gmail 应用专用密码
- `MAIL_TO`：多个收件人用英文逗号分隔
- 不要把密码直接写进 `config.yaml`

## 6. GitHub Actions

工作流：

```text
.github/workflows/daily_paper.yml
```

包含：

```yaml
schedule:
  - cron: "0 0 * * *"
workflow_dispatch:
```

`0 0 * * *` 是 UTC 00:00，对应北京时间 08:00。

同时可以在 GitHub：

`Actions → Daily Academic Paper Search → Run workflow`

手动测试。

GitHub 官方文档说明，scheduled workflow 默认使用 UTC；高负载时刻可能发生延迟，因此实际运行时间不保证精确到秒。

## 7. 推荐首次部署顺序

1. 本地创建项目并复制这些文件。
2. 修改 `config.yaml` 中的邮箱、关键词。
3. 配置 SMTP 密码环境变量。
4. 本地运行 `python main.py`。
5. 确认收到 HTML 邮件。
6. 检查 `data/sent_ids.json` 是否产生记录。
7. 建立 GitHub 仓库并 push 全部文件。
8. 添加 GitHub Secrets。
9. 确认 Actions workflow 具有 `Read and write permissions`。
10. 手动 Run workflow。
11. 确认邮件收到且 `data/sent_ids.json` 自动提交更新。
12. 等待每日 UTC 00:00 自动运行。

## 8. 常见问题

### 没有邮件

先检查：

- SMTP 主机/端口
- SMTP 授权码/应用专用密码
- `MAIL_FROM`
- `MAIL_TO`
- 邮箱是否开启 SMTP/第三方客户端服务

### GitHub Actions 能发邮件，但 sent_ids 没有提交

进入：

`Settings → Actions → General → Workflow permissions`

选择允许工作流写入仓库内容。

本工作流也显式声明：

```yaml
permissions:
  contents: write
```

### 为什么影响因子显示“未配置”

因为这不是公开数据库 API 稳定提供的字段。本项目不把其它引用指标冒充 JCR Impact Factor。

### API 临时失败怎么办

每个 HTTP 请求默认最多自动重试 3 次，并记录失败日志；单个数据库失败不会阻止另外两个数据库继续检索。

## 9. 后续可扩展

目前核心结构已经把“数据库适配器”“去重”“指标映射”“邮件发送”“工作流”分离，后续可以继续增加：

- Semantic Scholar
- Crossref
- Europe PMC
- Web of Science / Scopus（在具有合法 API 权限时）
- 论文关键词高亮
- 相关性评分
- 中英文摘要
- 每日 Markdown / Excel 附件
- Telegram / 企业微信 / 飞书推送
