# 问题与解决记录

每次修完一个问题，在这里补一条：**现象 → 原因 → 怎么改的 → 涉及文件 → 对应提交**。
按日期倒序，最新的在最上面。

---

## 2026-11-19

### 9. 独立的「打印模板」工具并入 BI，并且 Agent 能开票

- **现象**：桌面那个 `打印模板.html` 是个单文件工具，5 套票据（澳门支票、香港支票、请款单 A4 横、汇款申请书 A4、中银汇款单 A4）全靠浏览器 localStorage 存，换台机器就没了；AI 看不见也碰不到，想「照这笔支出开一张汇款单」只能手工再录一遍。
- **原因**：
  1. 单文件内嵌了 5 张 base64 底图，HTML 1.06MB，每次打开都要解析；模板数据只活在浏览器里，没有服务端。
  2. 它自带一套 `TPLAgent.exec()` 前端 API（list/create/update/setValues/…），但只能在页面里跑，后端拿不到。
  3. BI 的 Agent 工具面里完全没有票据这一块。
- **解决**：
  - **底图与字段抽出来**：`tools/extract_print_assets.py` 从旧 HTML 里把 5 张底图写成 `static/print-assets/*.jpg`、把 5 套字段定义（8 / 8 / 9 / 20 / 45 个字段，坐标即已校准值）写成 `print_seed/templates.json`。底图走 `/api/print/bg/assets/<file>`，可缓存，页面不再吞 1MB base64。
  - **服务端存储**（新 `print_tpl.py`）：`data/print/templates/<id>.json` 一套模板一个文件，`data/print/bg/` 存用户上传的底图，首次启动按种子灌入（已有模板则不动，避免覆盖人工校准的坐标）。接口 `GET/POST /api/print/templates`、`GET/PUT/DELETE .../<id>`、`.../records`、`.../values`、`.../fields`、`POST /api/print/bg`、`POST /api/print/import`（吃旧工具 `TPLAgent.export()` 的 JSON，dataURL 底图自动落盘，同名覆盖）。
  - **金额大写两端一致**：`print_tpl.to_chinese_amount` 与前端 `pWords` 逐字对齐（含「肆角整」这种末位补整规则）；派生字段不落库，读取时按金额来源现算，避免改了金额忘了同步大写。
  - **前端视图**（`app.js` 的 `renderPrint`）：BI 内新页面「🖨️ 票据打印」，左侧字段面板（值 / 类型 / X / Y / 字号 / 显示开关），右侧画布拖拽定位、双击勾选、A± 调字号；改完 600ms 防抖整份 PUT，状态显示「已保存 hh:mm」。`@page{size:Wmm Hmm;margin:0}` 随纸型动态注入——**缺这条浏览器会按 Letter/A4 出纸，票据必然错位**（第一版就漏了，PDF MeasureBox 直接是 612×792）。
  - **Agent 工具**（新 `agent_print.py`，9 个）：`list_print_templates` / `get_print_template` / `get_print_record` 只读；`create_print_record`（可传 `from_entry_id`，按字段名关键词把日期、收款人、金额、摘要带进去，对不上的列进 `need_input` 交人补）/ `update_print_record` / `delete_print_record` / `update_print_field` / `create_print_template` / `delete_print_template` 为写。
  - **权限**：写工具进 `agent.WRITE_NAMES`（只读模式不下发），并复用 `agent_admin._need_confirm` 出审核卡，`module='agent_print'`。四档语义：ask 全部出卡、auto 放行 create/update 但 delete 仍要确认、full 直写。坐标改动单次限幅 ±3mm、字号 ±0.5mm——AI 只有数字，看不出文字是否压到印刷字上。
  - **打印的边界**：浏览器不允许后台静默出纸，所以不给 Agent「打印」工具。工具返回 `#print/<模板id>/<序号>` 深链接，前端 `agentOpenLink` 新增 `print:` 分支，点开即待打印状态，最后 Ctrl+P 由人按。
- **涉及**：`print_tpl.py`（新）、`agent_print.py`（新）、`print_seed/templates.json`（新）、`static/print-assets/*.jpg`（新）、`tools/extract_print_assets.py`、`tools/test_print.py`、`tools/test_agent_print.py`（新）、`app.py`、`agent.py`、`static/app.js`、`static/style.css`、`static/index.html`、`.gitignore`
- **验证**：接口自测 43 项、Agent 工具自测 36 项全通过（副本数据，不碰真实库）；Playwright 实测三种纸型：90×275mm（支票横放顺时针）、210×297mm（A4）、拖拽 40px → 15.5mm 且落库持久、底图上传可用、打印 PDF 无侧栏无底图只有票面文字。
- **提交**：本次

### 8. 想用 fsc-boe-extract 这类要跑脚本的技能，还要能 / 调用

- **现象**：① 在 WorkBuddy 里写好的 `fsc-boe-extract`（接管 Edge 抓共享中心报账单）搬不进本系统；② 技能与 MCP 只能点 🧩 弹窗看，弹窗还挡住页面；③ 想在输入框打 `/技能名` 直接调用。
- **原因**：
  1. `agent_skill.py` 只认「按顺序调已注册工具」的声明式 JSON，跑不了需要起进程、等人扫码登录的活；而且技能只从 `data/skills/*.json` 读，不认目录形式。
  2. 🧩 走的是通用 `openModal`，天生覆盖主页面。
  3. 完全没有斜杠命令的解析。
- **解决**：
  - **技能分两种**：`steps`（流程型，原样保留）与 `guide`（说明型：一段 Markdown + 可选 `scripts/`）。说明型 `run_skill` 只回手册正文，不执行任何东西。
  - **技能目录**：新增仓库内 `skills/`（随代码发布、只读），`data/skills/` 为用户技能，同 id 用户覆盖内置；支持 `<id>/skill.json` 目录结构。内置技能禁止删除（接口返 400）。
  - **脚本执行器**（新文件 `agent_script.py`）：只能跑 `skills/<id>/scripts/` 下、扩展名在 `.py/.sh/.bat/.cmd` 白名单内的文件，`realpath` 必须落在该目录内（越界直接拒）。默认后台跑，返回 `job_id`，`check_script` 轮询日志尾部并支持终止——抓单据含「等用户登录」的阻塞，前台会把整轮对话卡死。参数里的 `{data}` 替换成系统数据目录绝对路径。
  - **权限**：`run_script` / `check_script` 进 `agent.WRITE_NAMES`，只读模式不下发；工具内部再查一次 `allow_write_tools()`；流程型技能里 `BLOCKED` 掉，脚本只能由主对话明着起。
  - **搬入 fsc-boe-extract**：`skills/fsc-boe-extract/`（skill.json + SKILL.md + 5 个脚本 + 字段对照表），`when` 写用户原话，`run_fsc.py` 用 `--out {data}/imports/fsc --work-dir {data}/fsc_profile` 保证产物与登录态都落在数据目录。
  - **斜杠命令**：输入框打 `/` 弹出技能列表（↑↓ 选、Enter/Tab 填入、Esc 收起，模糊匹配 id/名称/触发场景）。后端 `expand_slash()` 把 `/id 后面的话` 改写成一句明确指令再交给模型，不引入「当前激活技能」这类隐藏状态。
  - **面板改内嵌**：能力面板从 `openModal` 改成抽屉内 `.agent-cap` 滑出层（`agentCapOpen/agentCapClose`），只占 Agent 自己的宽度；输入框左侧新加 🧩 按钮，与头部入口同一个动作。
- **涉及**：`agent_script.py`（新）、`agent_skill.py`、`agent_core.py`、`agent.py`、`skills/fsc-boe-extract/**`（新）、`static/index.html`、`static/app.js`、`static/style.css`、`.gitignore`
- **提交**：本次

### 7. Agent 面板挡页面、点链接就把对话关了、看不到技能、没有 MCP

- **现象**：四个问题——① 打开助手后右侧像被盖住，主页面表格挤成一条；② 点回答里的流水链接，助手整个收起，回来还得重新找会话；③ 不知道 AI 到底存了哪些技能；④ 只能用它内置的 36 个工具，接不了外部 MCP。
- **原因**：
  1. 抽屉是 `position:fixed` 靠 `#main{margin-right}` 让位，但打开后没有触发 `resize`，图表按旧宽度画、表格列被压；窄屏更直接 `width:100vw` 全覆盖。
  2. `agentOpenLink()` 第一行就是 `agentClose()`，作者当时以为要"先收起抽屉让页面露出来"。
  3. 技能只存在 `data/skills/*.json` 里，除系统提示词注入给模型外，界面上没有任何入口。
  4. 完全没有 MCP 客户端。
- **解决**：
  - **让位布局**：默认宽度 400px；打开/关闭后 `agentRelayout()` 延时触发一次 `resize`，图表与表格按新宽度重排；`<900px` 改成半宽抽屉（`min(94vw,560px)`）+ 半透明遮罩，点遮罩才关，不再整屏糊住。统一改名「Agent」。
  - **链接弹窗**：`agentOpenLink` 去掉 `agentClose()`。`entry:` / `journal:` 改为 `agentPeekEntry` / `agentPeekAccount` 只读弹窗（新增 `/api/agent/peek/entry/<id>`、`/api/agent/peek/account/<id>`），带「在日记账中打开」；`dashboard` / `deposits` / `cash` 只切主区域页面，Agent 保持打开；附件与外部链接走新标签页。
  - **能力面板**：助手头部加 🧩，弹窗分「技能 / MCP」两个 tab。技能列出名称、触发场景、步骤链，支持「用一下」（把指令填进输入框）和删除；新增 `GET /api/agent/skills`、`POST /api/agent/skills/<id>/delete`。
  - **MCP 客户端**（新文件 `agent_mcp.py`）：stdio 子进程 + JSON-RPC 2.0 逐行，`initialize → notifications/initialized → tools/list → tools/call`，支持分页 `nextCursor`；工具命名 `mcp__<server>__<tool>`，在 `pick_tools` 里并入工具面（只读模式不下发），`run_tool` 按前缀分流，`build_system` 注入已连接工具清单。懒连接 + 失败 60 秒冷却 + 启动后台预热，任一服务器起不来不影响对话。配置存 `data/mcp.json`，接口 `GET/POST/DELETE /api/agent/mcp` 与 `POST /api/agent/mcp/<name>/connect`。
- **涉及**：`agent_mcp.py`（新）、`agent_core.py`、`app.py`、`static/index.html`、`static/app.js`、`static/style.css`、`docs/ISSUES.md`
- **提交**：本次

### 6. AI 有些操作没经过我审核就直接改了

- **现象**：让 AI 删公司、删账户、改期初余额，它说一句「已删除」就真删了，用户全程没看到确认；记账、改账、建账户更是一直直接落库，没有任何提示。
- **原因**：两处设计漏洞叠加。
  1. `agent_perm.py` 只有 `auto / confirm` 两个值，且默认 `create/update/admin = auto`——记账、改账、日常维护全部直接写，用户看不到草稿卡。
  2. 更严重的是 `agent_admin.py` 的 `_need_confirm` 返回的 note 写着「**他已经明确说确认时，立刻带 confirm=true 重新调用一次**」。`confirm` 是模型自己填的参数，等于**模型自己确认自己**：它判断「用户大概同意了」就直接执行，用户根本没点过任何按钮。
- **解决**：改成四档访问模式，闸门收到服务端。
  - **四档**：`readonly` 只读（写工具根本不下发）/ `ask` 逐条确认（所有写入出草稿卡，默认）/ `auto` 自动写入（记账改账日常维护直接写，删除·导入·删公司账户仍要确认）/ `full` 完全访问（任何操作直接执行）。存在 `data/agent_perm.json` 的 `mode` 字段，改完即时生效。
  - **只有 `full` 接受模型自带的 `confirm=true`**：`_approved(args)` 同时要求 `confirm` 与 `can_bypass()`，其他模式一律生成审核卡。
  - **审核卡只能人点**：高危操作改成 `add_proposal(kind='admin', danger=True)`，前端渲染成带「我已核对」勾选的卡片，确认动作走 `/api/agent/proposal/<pid>/confirm`。模型没有这条通路。
  - **防止伪造内部标记**：`run_tool` 和 `skill.run` 在注入 `__sid` 前先剥掉模型传来的所有 `__*` 键，否则模型传 `__approved=true` 就能跳过审核。
  - **新增接口**：`GET/POST /api/agent/mode`；助手右上角加模式选择器（只读灰 / 确认蓝 / 自动绿 / 完全访问红），切到完全访问时弹二次确认。
  - `merge` 此前不在任何策略表里，一直是「永远确认」，这次归入 `auto/full` 的直写范围。
  - 老格式 `{"create":"auto",...}` 读到时按松紧自动迁移成对应 mode，不让人升级后行为突变。
- **涉及**：`agent_perm.py`（重写）、`agent_admin.py`（`_approved` / `_need_confirm` / 7 处 gate）、`agent_write.py`（`_apply_admin`、`apply_proposal` 分发、`_ui` 带 impact）、`agent_core.py`（`pick_tools` 只读过滤、模式接口、`run_tool` 剥键）、`agent_extra.py`（删附件走审核卡）、`agent_skill.py`（剥键）、`prompts/agent_system.md`、`static/index.html`、`static/app.js`、`static/style.css`
- **提交**：本次

### 5. 聊到第 13 轮，前面说过的全没了

- **现象**：长对话里用户说「就按刚才那个口径」「把前面那三笔再算一遍」，模型答非所问，因为它根本看不到那些内容。
- **原因**：`agent_core.py` 只有 `for h in (history or [])[-12:]`——第 13 轮往前的内容直接蒸发，连摘要都不留。
- **解决**：新增 `compact_history()`：最近 12 条保留原文，更早的每轮压成一行（用户问题 70 字、回答 90 字），最多 8 行，头部标一句「更早还有 N 轮」。摘要末尾固定带一句「具体数字一律以重新查询为准」，避免它拿压缩过的旧数字下结论。会话事实缓存（`SESS`）继续负责数字，两者分工不重叠。
- **涉及**：`agent_core.py`（`compact_history`、`run` 里的历史装配）
- **提交**：本次

### 4. 附件只能看不能管

- **现象**：让 AI「看看这笔的发票在不在」做不到；发现挂错了附件，也只能让用户自己去页面删。
- **原因**：`/api/entries/<id>/attachments`、`/api/attachments/<id>` 两个接口没包成工具。
- **解决**：`agent_extra.py` 补两个工具
  - `list_attachments(entry_id|company_id|att_id)`：返回文件名、大小、上传时间、所属流水，带 `attachment:<id>` 链接可直接打开；
  - `delete_attachment(att_id, confirm)`：两段式，第一次只返回 `impact`（哪个文件、挂在哪笔流水上、删除不可恢复），用户明确同意后带 `confirm=true` 重调。删除复用 `app.del_attachment` 视图，走同一把写锁，不重写删文件逻辑。
  - 两个都进 `WRITE_NAMES`（删除是写操作），只读轮次不给。
- **涉及**：`agent_extra.py`、`app.py`（绑定 `del_attachment`）、`agent.py`
- **提交**：本次

### 3. 同一套活每次都要重新交代一遍

- **现象**：月末体检这套查询前后做了三次，每次都得把「查未兑现、查待跟进、按公司」重新说一遍；让 AI「记住这个流程」也做不到。
- **原因**：系统没有任何「把一串动作固化下来」的机制，只有 36 个原子工具。
- **解决**：新增 `agent_skill.py`
  - skill 存成 `data/skills/<id>.json`（声明式步骤，不是代码）：`{tool, args, as, label}` 按顺序执行，支持 `${参数名}` 占位与 `${步骤.字段}` 引用前一步结果；
  - 四个工具：`create_skill` / `run_skill` / `list_skills` / `delete_skill`；
  - **硬边界**：skill 只能调已注册工具，`BLOCKED` 禁掉自己（防递归），不能写代码、不能算数、不能碰数据库。需要 if/循环/计算的需求不做成技能，直接对话；
  - 注入只给 id / 名称 / `when`（最多 12 条），完整步骤留在文件里，不占上下文；
  - 缺参数时直接返回「需要哪个参数、要什么」，不静默失败。
- **涉及**：`agent_skill.py`（新）、`agent.py`、`agent_core.py`（`skill_block`）、`prompts/agent_system.md`
- **提交**：本次

### 2. 合并流水后，会话里缓存的余额/流水不失效

- **现象**：让 AI 合并两笔之后紧接着问余额，它仍然引用合并前的数字；而且**纯查询**的对话轮次也会把「合并」这个写工具挂上来。
- **原因**：`propose_merge_entries` 是写操作，却漏在 `agent.py` 的 `WRITE_NAMES` 集合外。连带的三个后果：
  1. `pick_tools(allow_write=False)` 剔不掉它，只读轮次也暴露；
  2. `agent_core.py:736` 的 `if name in WRITE_NAMES` 判断不成立，auto 直写后不调 `sess_mark_dirty`，会话事实缓存全部保持「干净」状态；
  3. 工具摘要走错分支，合并结果只能显示成通用文本。
- **解决**：`WRITE_NAMES` 补上 `propose_merge_entries`，并在集合上方注明为什么它必须归位。
- **涉及**：`agent.py`
- **提交**：本次

### 1. 「导出 Excel」「有哪些还没跟进」这两类页面操作，AI 做不了

- **现象**：页面上有导出按钮和提醒弹窗，但让 AI「导出一份 Excel」「看看哪些还没签收」，它只能回「请到页面点导出」。
- **原因**：盘了一遍 `app.py` 的 39 个 `/api` 接口，有 28 个已经包成工具，缺的 5 个里有三个是**有独立产出物或独立语义**的：`/api/export/excel`、`/api/export/cash-counts`、`/api/notes/reminders`。其余两个（附件上传/删除）属于用户拖文件的行为，不需要 AI 代劳。
- **解决**：新增 `agent_extra.py`
  - `export_data(kind=journal|deposits|cash_counts|master, ...)`：**不重写导出逻辑**，在同一进程里用 `app.test_request_context` 调原来的视图函数取 Response 字节——`export_excel` 上千行，动它有把真实导出搞坏的风险。生成的文件落 `data/exports/`（保留 7 天自动清），返回可点的 `url:/api/export/agent/...` 链接；新增该下载路由，只开放 `data/exports` 目录。
  - `list_reminders(...)`：对应「备注非空 **或** 挂了标签」，比 `list_tags` 全——只有备注、没挂标签的那批以前完全查不到。返回 `breakdown` 区分「只有备注」「只有标签」。
  - 视图返回 JSON（`bad(...)`）时识别为错误，不会把 JSON 存成 xlsx；`send_file` 的 `direct_passthrough` 要先关掉才能取字节。
  - 前端 `mdInline` 增加 `/api/...` 相对链接规则，AI 在正文里写的下载链接可直接点。
- **涉及**：`agent_extra.py`（新）、`agent.py`、`app.py`、`prompts/agent_system.md`、`static/app.js`、`static/index.html`（v2.10.2）
- **提交**：本次

---

## 2026-11-18

### 4. 只改标签时，加一个标签会把原来的标签全清掉；卡片上也看不到原标签

- **现象**：AI 说「加个标签」，执行后这笔流水原来挂的标签没了；草稿卡上「标签」一栏的「改前」永远是空的。
- **原因**：`propose_update_entry` 只有 `tags` 一个参数，语义是**整套替换**；模型想「加一个」也只能传一个元素的数组，于是把其余标签冲掉。变更卡片里 `from` 直接写死 `''`。
- **解决**：
  - 新增 `add_tags` / `remove_tags` 两个参数（追加 / 移除），`tags` 仍是整体替换；
  - 先在库里读出当前标签，卡片按「原标签 → 新标签」显示；目标与现状相同时直接返回「已经是这些标签，没有需要改的」，不再生成空草稿；
  - `resolve_tags` 统一走新的 `_tag_names`（去重、支持逗号/顿号分隔）。
- **涉及**：`agent_write.py`（`_tag_names`、`resolve_tags`、`w_propose_update`、`propose_update_entry` 的 schema）、`prompts/agent_system.md`
- **提交**：本次

### 3. 待办提醒在每次刷新页面时都弹出来；刷新后还会丢页面/丢筛选条件

- **现象**：按 F5，或者 AI 改完账自动重画页面时，提醒弹窗又弹一次；有时候还回到别的页面、日期范围和搜索词没了。
- **原因**：
  - `boot()` 里只要有待办就 `openReminderModal()`，没有「本会话已经弹过」的判断；
  - 日记账的筛选状态只在 `switchView()` 时写 sessionStorage，改日期/搜索/标签后没有保存，刷新就丢；
  - 写后刷新走 `switchView()` 全量重画，表格滚动位置丢失，弹窗开着时还会把正在填的表单冲掉。
- **解决**：
  - 提醒改成 `sessionStorage` 记一次（`jb.remPopped`），一次会话只自动弹第一次，之后点侧栏按钮看（角标仍在）；
  - `viewStateSave()` 覆盖日记账全部筛选（账户/日期/预设/搜索/标签）和现金页日期，改一个存一次；
  - `refreshAfterWrite()` 记住并恢复表格滚动位置与窗口滚动；有弹窗开着时先挂起，关掉弹窗再画；
  - `switchView()` 返回渲染 Promise，刷新能等到真正画完再恢复位置。
- **涉及**：`static/app.js`（`viewStateSave`、`viewStateRestore`、`switchView`、`refreshAfterWrite`、`boot`、`closeModal`）、`static/index.html`（版本号 2.10.0）
- **提交**：本次

### 2. AI 查「10月薪资」只会查 10 月记账的流水；改完一笔后再说改另一笔就不动了

- **现象**：
  - 11 月记的「10月薪金」，让 AI 查「10 月的薪资」，它按记账日期 10 月去查，回「10 月共 8 笔」但列的是 10 月发 9 月工资，真正的 10 月薪资一条没出来；
  - 第一次给它挂标签能成，第二次说「另外那笔…也加上」它反问一堆，压根不查库。
- **原因**：
  1. `query_entries` 的 `date_from/date_to` 过滤的是 `entries.date`（银行流水日期），工具描述没讲清；命中 0 笔时也不给任何提示，模型只能得出「没有记录」。
  2. 意图路由 `_RE_WRITE` 只认「加标签/改成/删掉」这类固定说法，「也加上」「合并成一笔」不匹配 → 走 `context` 模式，**工具被清空**，模型想查也调不了，只能反问。这才是「一个对话只能调一次工具」的真身。
  3. 写入工具的执行结果也被塞进「本次会话已查到的数据」，一轮批量改 8 笔就把 8 条事实位占满，且提示词写着「别重复查」，下一轮模型直接拿旧清单断言「没有这笔」。
  4. 自动重画没人触发：权限是 `auto` 时工具直接落库，不产生草稿卡，前端拿不到「写完了」的信号，日记账一直显示旧标签。
- **解决**：
  - `query_entries` / `aggregate_entries`：`keyword` 支持多个词（逗号分隔，同时满足）、连带搜组合分录小明细；**按记账日期查成 0 笔时自动放开日期重查**，并把结果连同 `hint` / `date_filter_ignored` 一起返回，模型一次就能答对；
  - 系统提示词加「记账日期 ≠ 业务所属期」的硬规则和「不许拿旧清单断言没有这笔」；
  - `_RE_WRITE` 补齐标签类动作（加上/挂上/贴上/标上/去掉…）与合并类说法；`context` 模式从「不给工具」改成「给只读工具」兜底；
  - 写入结果不再进会话事实；写成功后给会话打「脏」标记，事实块会提醒「账本已改过，涉及具体流水请重查」；
  - 写成功时推一个 `refresh` SSE 事件，前端防抖后重画当前页；轮数上限默认 6 → 8，轮数用完且确实写了东西时，兜底文案改成「已写入 N 笔」而不是「没收敛」；
  - 提示词补「批量改同一个字段要在同一条回复里并行发起多个 propose_update_entry」。
- **涉及**：`agent.py`（`_kw_terms`、`_kw_sql`、`t_query_entries`、`t_aggregate_entries`、schema）、`agent_core.py`（`_RE_WRITE`、`facts_block`、`sess_mark_dirty`、`run_agent_stream`、`DRAFT_ONLY`）、`agent_write.py`、`prompts/agent_system.md`、`static/app.js`（`agScheduleRefresh`）
- **提交**：本次

### 1. 多笔钱只能一笔笔记，账上能汇总但看不到每笔是谁、导出也没法拆
- **现象**：发一次工资、付一批供应商，银行只出一条总金额，但实际是十几个人的钱。分开记 → 银行流水对不上笔数；合成一笔 → 明细丢掉，导出给财务还是"工资 10000"一行，看不出张三李四各多少。
- **原因**：`entries` 表一笔就是一行，没有「父记录 + 子明细」的层次；导出也是按 `entries` 逐行写，天然拆不开。
- **解决**：
  - **数据**：新增 `entry_items(id, entry_id, seq, description, party, amount, note)`，保持 `entries` 不动 —— 父记录的 `debit/credit` 恒等于明细合计，所以余额、合计、看板、现金账本、资金规划这些既有逻辑**一行都不用改**。明细金额存绝对值，方向（收/支）继承父记录。
  - **录入**：编辑弹窗加「🧩 多笔合记 / 小明细」块，可增删行（摘要 / 对方 / 金额）。有 ≥2 笔有效明细时，上方金额框自动同步成合计，保存时以合计覆盖 `entries` 金额；`debit`/`credit` 都为 0 直接报错「请先填收入或支出金额来确定方向」，避免出现 0 元组合。
  - **列表**：父行摘要后加徽标 `▸ 🧩 3 笔`，点开就地展开子行（淡蓝底、缩进、`①②③` 序号），余额列写 `·` —— 父行才是账上那一笔，子行不重复计算余额。
  - **导出**：`write_entry()` 增加 `item` 参数；`emit_booked()` 把一笔按明细拆成多行，逐行 `上一行余额 ± 本行金额`，**只在最后一行做真实余额校准**（`adj`），保证拆开前后期末余额分毫不差。月度分组的 `SUM` 范围和月末余额引用同步改成"展开后的实际行数"，否则会少算。
  - **配套**：删除流水/删除账户级联清理 `entry_items`；关键词筛选把明细摘要也算进 haystack（搜"王五"能整组带出来）；AI 删除分录时明细一并存入 undo，撤销时还原。
- **涉及**：`app.py`（`SCHEMA`、`entry_items_map`、`sync_entry_items`、`serialize_entries`、`add_entry`、`upd_entry`、`del_entry`、`_delete_account_db`、`export_excel`）、`agent_write.py`、`static/app.js`（`journalRow`、`itemRows`、`openEntryModal`）、`static/style.css`
- **提交**：本次

### 2. 已经分开记的几笔想合成一笔，没有入口；AI 也不会合并

- **现象**：先逐笔记了「付甲 3000」「付乙 2000」，事后想合成一笔 5000 + 两条明细，界面只能删了重录；问 AI「这几笔合并」，它只会说做不到。
- **原因**：上一版只做了「一笔 → 多条小明细」的录入方向，没有反向的「多条流水 → 一笔明细组」；`agent_write` 的工具集只有新增/修改/删除。
- **解决**：
  - 抽出 `merge_entries_db(db, ids, date, desc)` 给界面和 AI 共用：校验同一账户、同方向 → 明细铺平（本身带明细的先展开，避免层级被压掉）→ 新建父记录（合计金额）→ 附件改挂到新记录 → 删原几笔 → 返回撤销快照。
  - **界面**：工具栏「🔗 多选合并」进选择模式（行首复选框、选中行蓝底高亮、已选合计与方向校验），选 ≥2 笔点「合并选中」弹出确认窗——可改日期/摘要，列出将要变成明细的每一笔，确认后一个事务完成。
  - **AI**：新增 `propose_merge_entries` 工具（草稿卡 → 确认 → 执行，走正常权限策略）；`propose_entry` 补 `items` 参数，一句话就能记成组合分录（只给明细不给总额时自动按合计记账）。
  - **撤销**：`undo` 增加 `merge` 类型——删掉合成的那一笔、原几笔连标签和明细一起还原、附件改挂回第一笔。
- **涉及**：`app.py`（`merge_entries_db`、`/api/entries/merge`）、`agent_write.py`（`w_propose_merge`、`DEFS`、`apply_proposal`、`undo_write`、`_norm_sub_items`）、`static/app.js`（`jrSelectMode`、`openMergeModal`、`itemRows`、草稿卡 `merge` 标签）、`static/style.css`
- **提交**：本次

---

## 2026-11-17

### 1. AI 助手只能查账，动不了账本

- **现象**：让 AI 记账、改账，它只会生成草稿卡；公司、账户、标签、定期、汇率、盘点这些功能它完全不会用。工具还是按关键词裁剪挂载的，问"余额"时连聚合工具都挂不上。
- **原因**：写入层设计成「一律出草稿等用户确认」，且没有管理类工具；`agent.py` 里按路由关键词挑选工具子集。
- **解决**：
  - 新增 `agent_perm.py`，把权限拆成六档（`create` / `update` / `delete` / `import` / `admin` / `admin_danger`），每档可配 `auto`（直接落库）或 `confirm`（出草稿）。默认新增、修改、日常维护直写，删流水、批量导入、级联删除仍需人工确认。
  - 新增 `agent_admin.py`，补上公司、账户、标签、定期存款、汇率、现金盘点、备份、撤销写入等 14 个工具。
  - `agent.py` 不再按关键词裁剪，agent 模式挂载全部 26 个工具，由模型自选。
  - 直写会记 `wid` 并写 `data/agent_writes.jsonl`，可用 `undo_write` 回滚。
- **涉及**：`agent_perm.py`（新增）、`agent_admin.py`（新增）、`agent.py`、`agent_core.py`、`agent_write.py`、`prompts/agent_system.md`
- **提交**：`1aeea91`

### 2. 草稿卡看不到完整信息

- **现象**：草稿卡里摘要被截断到 40 字，对方、项目、备注、支票号、标签都看不到，想核对就得先去流水页翻。
- **原因**：`agProp` 渲染时对摘要做了 `.slice(0, 40)`，且没有承载其余字段的位置。
- **解决**：卡片标题栏改为可点，点一下展开整张卡：摘要不再截断，每笔下方补一排灰色字段块（对方、账户、币种、项目、备注、支票号、标签、兑现状态、流水号），空字段自动隐藏。展开/收起时不再把聊天区强制滚到底部。
- **涉及**：`static/app.js`、`static/style.css`
- **提交**：`1aeea91`

### 3. 待办提醒：改状态要跳页、看不到是否兑现、跳过去没有返回键

- **现象**：提醒列表只能点「查看流水」，跳转到日记账页弹窗改，改完没有返回键；列表里也看不出这笔到底兑现了没有。
- **原因**：提醒弹窗只做了跳转入口，`/api/notes/reminders` 也没返回 `pending`、金额、对方等字段。
- **解决**：
  - 接口补上 `pending`、`party`、`project`、`debit`、`credit`、`check_no`。
  - 列表每条右侧加徽章：绿色「已兑现」、橙色「未兑现」，并显示金额（收绿支红）。
  - 去掉跳转，改为就地详情弹窗，里面可直接「标记为已兑现 / 未兑现」，底部有「← 返回提醒列表」。
- **涉及**：`app.py`、`static/app.js`、`static/style.css`
- **提交**：`1aeea91`

### 4. 提醒详情里改不了标签和备注

- **现象**：点进详情，标签是一排彩色只读标签、备注是只读文字，只能看不能改。
- **原因**：详情弹窗把这两项按展示字段渲染，没有编辑控件。
- **解决**：标签改成可点选（选中带 ✓ 和深色描边），备注改成文本框，并加「＋ 新标签」可直接建标签。
- **涉及**：`static/app.js`、`static/style.css`
- **提交**：本次

### 5. 改标签被自动保存，窗口突然消失

- **现象**：点完标签还没想好，0.7 秒后自动落库了；如果同时没有备注，这笔直接退出提醒列表，详情窗莫名消失。用户点掉两个标签后，提醒从 24 笔变成 22 笔。
- **原因**：当时为了实现「改完自动存」，加了 700ms 防抖自动提交，标签点击立即触发。
- **解决**：取消自动保存。改标签、改备注都只做本地变化，只有点「立即保存」才落库；保存成功后关闭详情、回到列表。有未保存改动时点「返回」或「关闭」会先确认一句，避免静默丢弃。
- **涉及**：`static/app.js`
- **提交**：本次

### 6. 保存后的提示弹窗打断操作

- **现象**：清空标签和备注保存后，弹出浏览器 `alert`「已保存。这笔已无标签和备注……」，必须点「确定」才能继续。
- **原因**：用 `alert` 做结果反馈。
- **解决**：改成列表顶部一行浅绿色内联提示，3 秒自动淡出，不阻塞任何操作。
- **涉及**：`static/app.js`、`static/style.css`
- **提交**：本次

### 7. 每次保存完，列表滚回最顶部

- **现象**：在提醒列表往下翻到某笔，改完保存回来，列表跳回第一条，得重新往下滚。
- **原因**：保存后整块重绘 `modalBox`，新 DOM 的 `scrollTop` 归零；进详情时列表 DOM 已被替换，位置信息丢失。
- **解决**：用 `remScrollTop` 记住位置——打开详情前先存一次，重绘后还原（`scrollTop` + `requestAnimationFrame` 再兜一次）。从侧栏重新打开提醒时仍从顶部开始。
- **涉及**：`static/app.js`
- **提交**：本次

### 8. 待办提醒一屏塞不下，想按公司/标签折起来看

- **现象**：提醒多了以后（单家公司 20 笔、5 类标签）整个弹窗是长条，往下翻找某一类很费劲，想先看总量再看细节。
- **原因**：提醒列表只有「公司 → 标签组 → 流水」三层结构，全部默认展开，没有折叠能力。
- **解决**：
  - 公司行、标签组行的标题都变成可点：左侧加 `▸/▾` 三角，右侧加「收起／展开」胶囊，点一下就地折叠，不重绘整块，滚动位置不丢。
  - 顶部加「全部收起 / 全部展开」两个按钮；收起后总高度变小，自动回到列表顶部。
  - 折叠状态存在 `remFold`（`co` / `grp` 两个 Set，键为 `c<公司ID>`、`g<公司ID>|<标签键>`），保存备注、切换兑现状态后重绘仍保持原样。
  - 收起时标题行仍显示「笔数 · 币种金额 · 类数」，不用展开也能判断值不值得点开。
- **涉及**：`static/app.js`、`static/style.css`
- **提交**：本次

### 9. 现金管理、定期存款没有按公司分类

- **现象**：「全部公司」下现金页是 8 张卡片平铺、盘点表混在一起；定期表也只是多一列公司名，看不出哪几笔属于哪家，公司间也不好比较。
- **原因**：两个页面只做了「加一列公司」，没做分组；看板和日记账的账户面板早就有分组，这两页没跟上。
- **解决**：
  - 定期表：按公司插入分组行（公司名 · 笔数 · 各币种本金 · 折 MOP · 到到期提醒），点分组行收起/展开；分组后去掉多余的「公司」列与行内公司小标签。
  - 现金页：现金账户卡片按公司分组（每组小计折 MOP），盘点记录表同样按公司分组（笔数 · 币种 · 有差异张数），都可点标题收起。
  - 收起状态存在 `cashFoldCo` / `depFoldCo`（Set），收起只切 DOM 显示、不重绘，滚动位置不丢；单公司模式不显示分组头。
  - 「全部公司」下「＋ 新增定期」按钮改为禁用（和「＋ 新建盘点」一致），避免把 `company_id` 写成 `all`。
- **涉及**：`static/app.js`（`renderCash`、`renderDeposits`、新增 `cashCard`/`ccRow`/`depRow`/`toggleCoFold`）、`static/style.css`
- **提交**：本次

### 10. 日记账选了月份就看不到别月的待兑付；全部公司下编辑盘点单会把公司写成 all

- **现象**：① 待兑付（未兑现/未到账）跟着日期范围走，选「本月」就只剩本月的，上个月开出去还没兑的支票看不见，容易漏。② 「全部公司」下点开一张盘点单直接保存，`company_id` 会被写成字符串 `all`。
- **原因**：① `pendRows = rows.filter(pending)`，而 `rows` 已经过日期过滤。② `openCashCount()` 里 `company_id: S.company`，全部公司模式下 `S.company === 'all'`。
- **解决**：
  - 待兑付段改为「不看日期范围」：直接从该账户全部流水里取 `pending`，标签和搜索仍然生效；标题行标注「含全部月份（不随上方日期范围变化）」，不在所选期间的日期旁标「非本期」；合计行文案改为「截至 X 已入账余额，扣除全部待兑付后」。
  - 盘点单改用单据自己的公司：`coId = 单据.company_id ?? (isAllCo() ? null : S.company)`，账面余额查询和保存都用 `coId`。
- **涉及**：`static/app.js`（`renderJournal`、`journalRow`、`openCashCount`）
- **提交**：本次

### 11. Agent 不知道界面上选的是哪家公司

- **现象**：左上角切到粤澳新资源后再问「这个月支出多少」，agent 仍按南粤科技回答；它也不知道另外 4 家公司叫什么，得先反问或自己 list_companies 猜。
- **原因**：`agent_core._company_name()` 直接取 `companies ORDER BY sort, id LIMIT 1`，即永远第一家；前端请求体里也没带当前公司，系统提示词只有一句「当前默认公司」。
- **解决**：
  - 前端 `/api/agent/chat/stream` 请求体加 `company_id: S.company`（`'all'` 或公司 id）。
  - `_company_name()` 换成 `_company_block(company_id)`，注入三段：① 用户当前在界面选的是哪家（或「全部公司」）；② 全部公司清单（名称 + company_id + 账户数）；③ 跨公司规则（查询省略 company_id＝全部；点名别家直接查、不要叫用户切界面；写入必须落到具体账户，全部公司或同名账户时先问清；跨公司汇总用 data_overview / aggregate_entries 一次算完）。
  - 提示词模板把结尾那句「当前默认公司：{company}」改成独立的「九、公司与范围（每次都要遵守）」章节。
- **涉及**：`agent_core.py`、`prompts/agent_system.md`、`static/app.js`
- **提交**：本次

### 12. aggregate_entries 带 date_to 一定查不到数据

- **现象**：让 agent 统计「2026 年 1—12 月按月支出」，工具返回 0 笔；只给 date_from 就正常。
- **原因**：`t_aggregate_entries` 里 `add_num()` 把 date_from、date_to 都拼成 `e.date>=?`，结束日变成「不早于结束日」，自然没有记录。
- **解决**：拆开写，起始 `>=`、结束 `<=`。复测 2026 全年 1071 笔、11 月 46 笔，和 `query_entries` 计数一致。
- **涉及**：`agent.py`
- **提交**：本次

### 13. 同名账户会静默落到第一家公司的账上

- **现象**：「库存现金（人民币）」在 5 家公司都有，`resolve_account()` 拿到这个名字直接返回第一条（南粤科技），agent 若在「全部公司」下收到「记一笔到库存现金」就会写错公司。
- **原因**：精确匹配用的是 `for ... if kw == a['name']: return a`，命中第一条就返回，没有歧义判断。
- **解决**：改成先收集全部精确命中，`==1` 才返回；`>1` 返回「有多家公司都有同名账户，请说清是哪一家」并列出「公司名·账户(币种)」；包含匹配、找不到的提示也都补上公司名。提示词里同步规定：全部公司或同名账户时先问清楚，不能猜。
- **涉及**：`agent_write.py`
- **提交**：本次

---

### 14. 日记账两段（已入账 / 未兑现）不能收起

- **现象**：按月分类能收起月份分组，但「✅ 已入账」「未兑现 / 未到账」这两个大段没有折叠开关；一个月几十笔时，想直接看下面待兑付必须一路滚。
- **原因**：两段是写死的 `<tr class="group-row">` 静态标题，明细行没有段落标记，无法批量显隐。
- **解决**：
  - `journalRow(e, sec)` 输出 `data-sec="booked|pend"`，按月分组头与空态行也带同一标记；
  - 段头改为 `.group-row.foldable` + `data-action="jr-fold"` + `data-sec`，带 `▸/▾` 与右侧「收起／展开」胶囊；
  - `toggleJrSec(sec)` 用 `tr[data-sec=X]:not(.group-row)` 只藏明细，**段头、期初余额行、两段合计行始终保留**，收起后仍能看到「已入账合计」的收/支/余额和「未兑现合计」的扣除后余额；
  - 状态存在 `jrFold = {booked, pend}`，`renderJournal` 末尾回放一次，**切换日期范围/标签/搜索重绘后折叠状态不丢**；
  - 收起是就地切 `display`，不重绘，滚动位置不动。
- **涉及**：`static/app.js`（`renderJournal`、`journalRow`、`toggleJrSec`、`jr-fold` 事件）
- **提交**：本次

---

## 2026-11-17 之前

### 二次修改丢草稿、写入后不刷新、刷新回首页

- **现象**：让 AI 改上一张草稿会丢内容；写入后界面数据不刷新；刷新页面会跳回首页。
- **解决**：重调 `propose_update_entry` 时作废旧草稿；写入后即时刷新视图；记住当前视图。
- **提交**：`47f298b`
