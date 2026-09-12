# 学术文献每日自动检索邮件推送系统

一个基于 **Python + GitHub Actions** 的个人科研文献日报系统。

系统每天自动检索 **PubMed、arXiv、OpenAlex**，根据配置的关键词和布尔逻辑筛选论文，利用 DOI / PMID / arXiv ID 持久化去重，并通过 QQ、163、Gmail 等 SMTP 邮箱发送 HTML 格式日报。

当前版本为 **V1.1**，已经加入 GitHub Actions 调度延迟容错和运行状态持久化，可以根据实际两次成功运行之间的间隔动态扩大检索窗口，降低定时任务延迟导致漏检的风险。

---

## 1. 当前功能

### 文献检索

- PubMed
- arXiv
- OpenAlex
- 多数据库结果自动合并
- 支持 `AND / OR / NOT / ()` 布尔表达式
- 支持多个独立检索式
- 默认至少检索最近 24 小时
- GitHub Actions 延迟时自动扩大检索窗口
- 最大检索窗口可配置

### 去重

系统优先使用以下唯一标识进行去重：

1. DOI
2. PMID
3. arXiv ID
4. 数据库内部 UID

持久化文件：

```text
data/sent_ids.json
```

### 运行状态

新增：

```text
data/runtime_state.json
```

用于记录：

- 上一次成功运行时间
- 上一次邮件发送时间
- 上一次新论文数量
- 上一次候选论文数量
- 上一次实际检索窗口

它与 `sent_ids.json` 的作用不同：

```text
sent_ids.json
    ↓
判断论文以前是否已经推送

runtime_state.json
    ↓
判断上一次成功运行是什么时候，从而计算本次检索窗口
```

### HTML 邮件

邮件包含：

- 论文标题
- 作者
- 出版物
- 发表日期
- 影响因子（如已配置）
- 来源数据库
- 摘要
- DOI
- 原文链接
- 本次实际运行时间
- 上次成功运行时间
- 实际检索时间范围
- 实际检索窗口
- 数据库候选论文数量
- 历史重复论文数量
- 本次新论文数量

### 邮箱

支持配置：

- QQ 邮箱
- 163 邮箱
- Gmail
- 其他支持标准 SMTP / SSL / STARTTLS 的邮箱

### 云端执行

使用 GitHub Actions：

- 每天北京时间约 08:15 自动运行
- 支持手动 `workflow_dispatch`
- GitHub Secrets 管理邮箱密码和 API Key
- 自动将去重和运行状态提交回仓库
- 无需本地服务器

---

## 2. 项目结构

```text
academic_paper_daily/
├── main.py
├── config.yaml
├── impact_factors.yaml
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
├── data/
│   ├── sent_ids.json
│   └── runtime_state.json
└── .github/
    └── workflows/
        └── daily_paper.yml
```

### 文件作用

| 文件 | 作用 |
|---|---|
| `main.py` | 主程序：检索、布尔匹配、合并、去重、邮件、运行状态 |
| `config.yaml` | 关键词、数据库、邮箱及运行参数 |
| `impact_factors.yaml` | 可选的期刊 JCR 影响因子人工映射 |
| `requirements.txt` | Python 依赖 |
| `data/sent_ids.json` | 已推送论文 ID 持久化记录 |
| `data/runtime_state.json` | 上一次成功运行状态 |
| `.github/workflows/daily_paper.yml` | GitHub Actions 自动任务 |
| `.env.example` | 本地环境变量示例 |

---

## 3. 配置关键词

编辑 `config.yaml`：

```yaml
search:
  hours: 24
  delay_tolerance_hours: 12
  max_hours: 48

  queries:
    - '"amorphous alloy" AND ("machine learning" OR "data-driven")'
    - '"metallic glass" AND ("machine learning" OR "data-driven")'
    - '"amorphous alloy" AND "artificial intelligence"'
```

### 布尔逻辑

支持：

```text
AND
OR
NOT
()
```

例如：

```text
("LiFePO4" OR "LFP") AND leaching AND NOT graphite
```

表示：

```text
LiFePO4 或 LFP
        ↓
必须包含 leaching
        ↓
不能包含 graphite
```

多个 `queries` 之间按 OR 处理，一篇论文只要匹配任意一个检索式即可进入最终结果。

---

## 4. 自适应检索窗口

这是 V1.1 相比初版最重要的改进之一。

### 传统方式

如果固定：

```yaml
hours: 24
```

程序永远只查询当前时间往前 24 小时。

但是 GitHub Actions 的 scheduled workflow 并不保证严格按照设定分钟执行，可能因为平台负载而延迟。

例如：

```text
计划运行：08:15
实际运行：11:50
```

如果仍然只查固定 24 小时，就可能缩短实际覆盖区间。

### V1.1 的处理方式

程序读取：

```text
data/runtime_state.json
```

计算：

```text
本次运行时间 - 上次成功运行时间
```

然后动态确定检索窗口。

例如：

```text
上次成功运行：08:16
本次运行：11:50
实际间隔：27小时34分钟
```

程序会根据：

```yaml
hours: 24
delay_tolerance_hours: 12
max_hours: 48
```

自动扩大本次搜索窗口，同时将最大搜索窗口限制在 48 小时。

这样可以降低 GitHub Actions 延迟导致漏检的风险。

### 参数说明

```yaml
search:
  hours: 24
  delay_tolerance_hours: 12
  max_hours: 48
```

含义：

- `hours`：正常情况下的最低检索窗口
- `delay_tolerance_hours`：发生调度延迟时附加的安全缓冲
- `max_hours`：检索窗口上限

建议初始保持：

```yaml
hours: 24
delay_tolerance_hours: 12
max_hours: 48
```

---

## 5. 时间与 GitHub Actions

当前工作流使用：

```yaml
on:
  schedule:
    - cron: "15 8 * * *"
      timezone: "Asia/Shanghai"
  workflow_dispatch:
```

即：

```text
Asia/Shanghai
每天 08:15
```

选择 08:15 而不是 08:00，是为了避开整点调度高峰，降低 scheduled workflow 延迟概率。

### 注意

GitHub Actions 的 `schedule` 仍然不是精确到秒的实时定时器。即使设置为 08:15，也不能保证每一天都严格在 08:15:00 开始执行。

因此本项目将：

```text
非精确定时
+
runtime_state.json
+
自适应检索窗口
+
论文 ID 去重
```

结合起来，提升系统可靠性。

---

## 6. 邮箱配置

编辑 `config.yaml`：

```yaml
email:
  smtp_host: "smtp.qq.com"
  smtp_port: 465
  security: "ssl"

  username: "your_email@example.com"
  from: "your_email@example.com"
  from_name: "每日学术文献推送"

  to:
    - "recipient@example.com"

  subject_prefix: "每日学术文献推送"
  send_when_empty: true
```

### QQ 邮箱

```yaml
smtp_host: "smtp.qq.com"
smtp_port: 465
security: "ssl"
```

`SMTP_PASSWORD` 使用 QQ 邮箱 SMTP 授权码，不要使用 QQ 登录密码。

### 163 邮箱

```yaml
smtp_host: "smtp.163.com"
smtp_port: 465
security: "ssl"
```

### Gmail

```yaml
smtp_host: "smtp.gmail.com"
smtp_port: 465
security: "ssl"
```

建议 Gmail 使用应用专用密码。

---

## 7. 本地安装与测试

推荐 Python 3.11+，GitHub Actions 当前使用 Python 3.12。

### 创建虚拟环境

Windows：

```powershell
python -m venv .venv
.venv\Scripts\activate
```

macOS/Linux：

```bash
python -m venv .venv
source .venv/bin/activate
```

### 安装依赖

```bash
pip install -r requirements.txt
```

### 设置 SMTP 环境变量

PowerShell：

```powershell
$env:SMTP_HOST="smtp.qq.com"
$env:SMTP_PORT="465"
$env:SMTP_USERNAME="你的邮箱"
$env:SMTP_PASSWORD="你的SMTP授权码"
$env:MAIL_FROM="你的邮箱"
$env:MAIL_TO="收件邮箱"
```

运行：

```powershell
python main.py
```

正常情况下会看到：

```text
PubMed returned ... candidates
arXiv returned ... candidates
OpenAlex returned ... candidates
Total=...
Email sent successfully.
Persistent sent-ID store updated
Runtime state updated
```

首次运行后检查：

```text
data/
├── sent_ids.json
└── runtime_state.json
```

---

## 8. GitHub Secrets

进入：

```text
GitHub
→ Repository
→ Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

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

不要把 SMTP 授权码、账号密码、API Key 写入仓库。

---

## 9. GitHub Actions 部署

工作流文件：

```text
.github/workflows/daily_paper.yml
```

核心调度配置：

```yaml
on:
  schedule:
    - cron: "15 8 * * *"
      timezone: "Asia/Shanghai"
  workflow_dispatch:
```

### 手动测试

进入：

```text
GitHub
→ Actions
→ Daily Academic Paper Search
→ Run workflow
```

建议首次部署完成后先手动运行一次。

### 运行流程

```text
Checkout repository
        ↓
安装 Python 3.12
        ↓
安装 requirements.txt
        ↓
运行 main.py
        ↓
读取 sent_ids.json
        ↓
读取 runtime_state.json
        ↓
动态确定检索窗口
        ↓
PubMed / arXiv / OpenAlex
        ↓
合并 + 布尔筛选
        ↓
历史去重
        ↓
HTML 邮件
        ↓
QQ / 163 / Gmail SMTP
        ↓
更新 sent_ids.json
        ↓
更新 runtime_state.json
        ↓
GitHub commit + push
```

---

## 10. GitHub Actions 为什么需要写回状态文件？

GitHub Actions runner 是临时环境。

如果只在 runner 中修改：

```text
sent_ids.json
runtime_state.json
```

下一次任务启动时这些变化可能不会保留。

因此 workflow 会执行：

```bash
git add data/sent_ids.json
git add data/runtime_state.json
git commit -m "chore: update paper search state"
git push
```

从而把状态持久化到仓库。

因此 workflow 使用：

```yaml
permissions:
  contents: write
```

仓库的 Actions Workflow permissions 也必须允许写入内容。

---

## 11. 影响因子说明

PubMed、arXiv 和 OpenAlex 的公开 API 并不等同于 Clarivate JCR 数据源。

因此本项目不会把 CiteScore、OpenAlex 指标或其他指标冒充为 JCR Journal Impact Factor。

项目提供：

```text
impact_factors.yaml
```

可以手动维护：

```yaml
"Advanced Materials": "29.4"
"Acta Materialia": "9.7"
```

邮件会显示匹配值；没有配置时显示：

```text
未配置
```

---

## 12. 常见问题

### Q1：为什么明明设置 08:15，邮件却晚几个小时？

GitHub Actions scheduled workflow 可能受到平台调度负载影响，实际开始时间不一定等于计划时间。

本项目通过：

```text
08:15 非整点调度
+
runtime_state.json
+
自适应搜索窗口
```

降低这种问题对文献覆盖范围的影响。

### Q2：为什么没有新论文也会收到邮件？

如果：

```yaml
send_when_empty: true
```

系统会发送一封“本次没有新的符合条件论文”的日报，同时更新运行状态。

如果不希望无新论文时发送邮件：

```yaml
send_when_empty: false
```

### Q3：为什么同一篇论文不会重复推送？

系统优先使用：

```text
DOI → PMID → arXiv ID → UID
```

并将已推送 ID 写入：

```text
data/sent_ids.json
```

### Q4：`runtime_state.json` 和 `sent_ids.json` 有什么区别？

```text
sent_ids.json
    = 论文是否已经推送过

runtime_state.json
    = 上一次成功运行是什么时候
```

前者负责去重，后者负责计算下一次的检索时间窗口。

### Q5：数据库临时失败怎么办？

每个 HTTP 请求默认最多自动重试 3 次。

如果一个数据库最终失败，程序记录异常并继续执行其他数据库，不会因为单一数据库故障直接中断整封日报。

### Q6：如何修改运行时间？

修改 `.github/workflows/daily_paper.yml` 的：

```yaml
schedule:
  - cron: "15 8 * * *"
    timezone: "Asia/Shanghai"
```

例如每天北京时间 07:30：

```yaml
schedule:
  - cron: "30 7 * * *"
    timezone: "Asia/Shanghai"
```

建议尽量选择非整点时间。

---

## 13. 当前版本状态

### V1.0

- PubMed
- arXiv
- OpenAlex
- 布尔检索
- 24 小时检索
- DOI/PMID/arXiv 去重
- HTML 邮件
- SMTP
- GitHub Actions
- GitHub Secrets

### V1.1

在 V1.0 基础上增加：

- `runtime_state.json`
- 自适应检索时间窗口
- 调度延迟容错
- 最大检索窗口限制
- 邮件显示实际运行状态
- 无新论文时仍可更新运行状态
- GitHub Actions 使用 `Asia/Shanghai` 时区
- 计划时间调整为北京时间 08:15

---

## 14. 下一阶段计划

当前系统已经可以作为稳定运行的个人科研文献日报使用。

后续可继续增加：

### V1.2：论文相关性评分

```text
标题命中
摘要命中
关键词数量
主题匹配
期刊权重
        ↓
相关性评分
        ↓
TOP 10 重点论文
```

### V1.3：论文主题分类

例如：

```text
非晶合金
数据驱动
机器学习
增材制造
电池回收
冶金过程
```

### V1.4：AI 论文解读

自动生成：

```text
研究问题
研究方法
主要结论
创新点
局限性
与你当前研究方向的相关性
```

最终可以逐步发展为个人化的科研文献情报系统。

---

## 15. 推荐部署流程

```text
修改 config.yaml
        ↓
本地 python main.py 测试
        ↓
确认 QQ 邮件正常
        ↓
检查 sent_ids.json
        ↓
检查 runtime_state.json
        ↓
Push 到 GitHub
        ↓
配置 GitHub Secrets
        ↓
Actions 手动运行
        ↓
确认邮件到达
        ↓
确认两个状态文件自动提交
        ↓
每天自动运行
```

---

## 16. 安全提醒

不要提交以下信息：

```text
SMTP_PASSWORD
邮箱登录密码
SMTP 授权码
NCBI_API_KEY
OPENALEX_API_KEY
其他第三方 API 密钥
```

推荐全部使用 GitHub Secrets 或本地环境变量。

---

## License / Usage

本项目主要用于个人科研信息检索与自动化学习工作流。使用第三方数据库时，应遵守相应 API 的使用条款、频率限制和数据许可要求。
