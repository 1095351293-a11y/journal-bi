# 日记账 BI 系统

> 一个「拷贝文件夹就能跑」的本地日记账 + 资金看板工具。纯 Flask + SQLite，前端零框架、零构建、零 CDN，全部数据留在你自己的电脑上。

**Bank Journal BI** — a self-hosted, single-folder bank journal & treasury dashboard for small finance teams. Built with Flask + SQLite, no build step, no cloud, no telemetry. Multi-company, multi-currency, AI-assisted natural-language querying.

---

## 它解决什么问题

中小企业 / 家族办公室的银行日记账通常散落在十几个 Excel 里：每个银行账户一本、每个月一个版本，想知道「现在手上还有多少钱」「这笔钱是哪笔收入来的」「上个月现金盘点差了多少」，得手工翻表。

这个工具把日记账搬进一个本地 SQLite：

- 录入流水后自动出余额、月度收支、总资产
- 多公司、多币种、多账户（银行 + 库存现金）统一折算看板
- 定期存款到期提醒、现金盘点差异分析、待办标签
- 一键导出成**带格式的 Excel 打印版**（一公司一页，保留边框/颜色/合并单元格）
- 接一个大模型 API，就有一个**会自己查库的 Agent**：自然语言问账，还能帮你起草流水、改账（写入前先给你看草稿）

## 功能一览

| 模块 | 能力 |
|---|---|
| 📊 总览看板 | 总资产 / 银行合计 / 库存现金 / 未到账，账户余额卡片，近 6 个月收支柱状图 |
| 📖 日记账 | 分页浏览、按公司/账户/日期/关键词/标签筛选，借贷记账，附件上传，批量清标记 |
| 💵 现金管理 | 库存现金账簿、现金盘点单（按面额录入，自动算账面应有 / 实有 / 差异 / 白条抵库），盘点单可复制新增 |
| 💰 定期存款 | 存单登记，到期与 30 天内到期自动提醒，本金折合本币汇总 |
| 🏷️ 标签 | 自定义颜色标签，标记「未收收据」「未签字」等待办 |
| 🏢 公司与账户 | 多公司、多银行账户、多币种，期初余额与期初日期 |
| ⚙️ 设置 | 汇率维护、数据库一键备份 / 恢复（zip 打包 db + 附件） |
| 🤖 AI Agent | 流式思考过程、意图路由（闲聊不查库）、多轮工具调用、读回单截图、起草写入、多会话历史 |
| 📤 导出 | 带格式的 Excel 打印版（一公司一页）；现金盘点表导出三表（汇总 / 明细 / 面额） |

## 快速开始

### 环境要求

- Python 3.9+（开发环境为 3.12）
- 无数据库服务、无 Node、无浏览器插件

### Windows

```bat
git clone <repo-url>
cd bank-journal-bi
pip install -r requirements.txt
start.bat
```

### macOS / Linux

```bash
git clone <repo-url>
cd bank-journal-bi
pip install -r requirements.txt
chmod +x start.sh && ./start.sh
```

浏览器自动打开 <http://127.0.0.1:8765>。

首次启动如果 `data/journal.db` 不存在，脚本会自动调用 `tools/gen_demo_data.py` 生成一套**全虚构的演示数据**，直接就能看到完整界面效果。

单独生成 / 重置演示数据：

```bash
python tools/gen_demo_data.py          # 已存在则跳过
python tools/gen_demo_data.py --force  # 覆盖重建
```

## 接入自己的账本

系统只做「录入 + 展示」，不做银行直连。真实数据通过两种方式进来：

1. **界面录入**：日记账页面手工新增，或用 Excel 整理后逐条粘贴。
2. **导入脚本**：`tools/` 下有两个一次性导入模板，按你自己的表结构改列映射即可。

```bash
python tools/import_data.py   # 银行流水 CSV / XLSX → entries
python tools/import_cash.py   # 现金日记账 + 盘点表 → accounts / cash_counts
```

> 导入脚本里的文件名、列号是按原作者的表改的，属于**示例**，用之前请对照自己的表格调整。

## AI Agent（可选）

不配置也能用，只是「🤖 AI」会提示未设置 API Key。

在界面「设置 → AI 模型」里填三项即可，兼容一切 OpenAI 格式的 `/chat/completions` 接口：

| 项 | 示例 |
|---|---|
| Base URL | `https://api.deepseek.com/v1` |
| Model | `deepseek-flash`（支持视觉的模型可读回单截图） |
| API Key | `sk-...`（只存在本地 `data/agent_config.json`，界面显示为掩码） |

支持 DeepSeek / OpenAI / 通义 / 智谱 / Kimi / 硅基流动 / 本地 Ollama / 自建网关。

### 它不是「套壳问答」

- **先路由再动手**：寒暄、问能力、追问上一轮结果这类问题直接回答，一次工具都不调；只有真需要查库才进工具循环。所以不会「问什么都在查数据库」。
- **流式展示过程**：思考内容、每一次工具调用的名字/耗时/结果摘要都实时推到界面，最后才给结论，跟 ChatGPT / WorkBuddy 一个观感。
- **多轮工具循环**：拿到中间结果会继续查（先看总览、再定位账户、再取明细），最多 6 轮，可在配置里调。
- **结果带链接**：回答里引用到的流水会渲染成 `[摘要](#journal:123)`，点一下直接跳到那笔账。
- **附件**：拖入或粘贴银行回单截图（视觉模型）、Excel / CSV 对账单、PDF / DOCX，Agent 先读字段再拿去核账，核不到会明说「疑似漏记」。
- **多会话历史**：左侧会话列表，可新建、切换、重命名、清空，记录存在浏览器本地，不上传。

### 写入是「草稿 + 人工确认」

Agent 能新增、修改、删除流水，也能从表格批量导入，但**它自己不能直接落库**：

1. 模型只能调用 `propose_*` 系列工具生成一张**草稿卡片**（日期、账户、金额、摘要四列可当场改）；
2. 你点「确认写入」，服务端才校验字段、加写锁、真正写库；
3. 删除类草稿必须额外勾选一个确认框才允许提交，防手滑；
4. 每次写入都记进 `data/agent_writes.jsonl`，卡片上直接给「撤销」，撤销按 before 镜像回滚（新增→删掉，修改→写回旧值，删除→按原 id 补回）。

只读工具仍然走 `mode=ro` 的 SQLite 连接，写操作在独立的写锁里完成，模型没有任何裸 SQL 入口。

## 数据与隐私

- 所有数据在 `data/journal.db`（外加 `data/uploads/` 附件），**不上传任何服务器**，没有埋点、没有遥测。
- `data/` 下的账本、附件、API Key 配置全部写进 `.gitignore`，不会被误提交。
- 仓库里的演示数据由 `tools/gen_demo_data.py` 随机生成：公司名、往来单位、账号、人名一律带「示例」前缀或为占位串，与任何真实主体无关。
- 想整份迁移：直接复制整个文件夹，或用「设置 → 备份」导出 zip，到新机器「恢复」。

## 目录结构

```
bank-journal-bi/
├── app.py                  # Flask 主应用：路由 / 账务计算 / Excel 导出（约 2500 行）
├── agent.py                # 工具定义与实现：只读查询 / 聚合 / 对比
├── agent_core.py           # Agent 内核：意图路由 + SSE 流式 + 多轮工具循环 + 视觉
├── agent_write.py          # 写入通道：草稿 → 校验 → 写锁落库 → 撤销日志
├── agent_files.py          # 附件：落盘、图片转 data URL、表格/文档提文本
├── prompts/
│   └── agent_system.md     # Agent 系统提示词（外置，可自行改语气和规则）
├── static/
│   ├── index.html          # 单页外壳
│   ├── app.js              # 前端全部逻辑，原生 JS，无框架
│   └── style.css
├── tools/
│   ├── gen_demo_data.py    # 生成演示数据（虚构）
│   ├── import_data.py      # 银行流水导入模板
│   └── import_cash.py      # 现金账簿 / 盘点表导入模板
├── data/                   # 运行时数据（git 忽略）
│   ├── journal.db          # SQLite 账本
│   ├── uploads/            # 凭证附件
│   ├── agent_uploads/      # 对话里上传的文件
│   ├── agent_proposals.json# 待确认的写入草稿
│   ├── agent_writes.jsonl  # 写入与撤销流水（审计用）
│   └── agent_config.json   # AI 配置（含 API Key，git 忽略）
├── start.bat / start.sh
└── requirements.txt
```

## 技术选择

| 决定 | 原因 |
|---|---|
| Flask + SQLite | 单文件部署，双击即用；财务数据量级（万级流水）SQLite 完全够 |
| 前端零框架 | 没有 npm install、没有构建产物，改完刷新即生效；离线可用 |
| 服务只监听 127.0.0.1 | 定位是个人 / 小团队本地工具，默认不暴露到局域网 |
| Agent 只用标准库 urllib | 不为一个可选功能引入额外依赖（xlsx/pdf/docx 解析也走 zipfile + 正则，没装第三方库） |
| 写入走草稿而不是直接执行 | 财务场景宁可多点一次，也不要模型自作主张改账；撤销日志兜底 |
| 备份 = 复制文件夹 | 财务人员能理解、能自己操作的恢复方式 |

## 已知限制

- 单用户单机，**没有登录和权限**，不要直接暴露到公网。
- Flask 自带开发服务器，小团队内网用可以，公网部署请换 waitress / gunicorn。
- 导出 Excel 的版式是按 A4 横向、一公司一页调的，公司特别多时需要自己在 Excel 里再调分页。
- 没有做复式记账的借贷平衡校验（这是日记账，不是总账）。
- AI 的写入能力依赖模型自觉走 `propose_*` 工具：它能改的只有草稿，最终落库永远要你点确认；但模型偶尔会漏调工具、嘴上说「已生成草稿」，**看不到卡片就是没写**。
- 对话历史存在浏览器 localStorage，换浏览器/清缓存会丢（服务端只缓存 30 分钟内的事实）。

## 许可

MIT，见 [LICENSE](LICENSE)。自己用、改、商用都行，出事别找我。
