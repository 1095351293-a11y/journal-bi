# 共享中心单据提取（FSC）

## 干什么

从需要登录的财务共享系统（中兴新云 FSC，如 `http://10.15.1.133`）里，把列表里每一笔
单据的完整详情（表头、费用行、支付信息、附件清单、审批流）批量取出来，落成一张明细表。

取完之后通常接两件事：按笔核对收款方与付款账户，或者据此登记日记账。

## 怎么跑（只有一条路：run_script）

用的是本系统自带脚本，**不要自己拼命令行、不要写新脚本**：

```
run_script(skill_id="fsc-boe-extract", script="run_fsc.py", args=[
  "--list-url", "<列表页 URL>",
  "--match", "getBoeDetailById",
  "--from-row", "5", "--to-row", "17",
  "--out", "{data}/imports/fsc",
  "--work-dir", "{data}/fsc_profile"
], background=true)
```

- `{data}` 会被替换成本系统的数据目录绝对路径（通常是 `…/银行日记账BI系统/data`）。
  产物因此落在 `data/imports/fsc/`，属于用户数据、不入库。
- `--work-dir` 存浏览器 profile 副本，**同一目录下次通常免登录**，建议固定写这个值。
- 也能按单据号：`--items "NY-BX2611110070,NY-BX2611120051"`。
- 只要列表不要详情：`script="grab_boe.py"`、`args=["--dump-rows", "--list-url", "...", "--out", "{data}/imports/fsc/rows.json"]`。

**必须后台跑**（默认就是 background=true）。脚本里有「等用户手动登录」的阻塞，
最长 300 秒；前台会卡住整轮对话。

## 跑起来之后

1. 立刻把 `job_id` 告诉用户，说一句「正在开浏览器，如果弹登录页请在那窗口里登录」。
2. 用 `check_script(job_id=...)` 看日志尾部。看到「等待登录」就把那句话原样转告，
   **不要自己重试、不要加 sleep 轮询几十次**；用户说登录好了再查一次。
3. 结束（`status=done`）后读 `tail` 里的汇总行，报告抓到多少笔、落在哪个目录。
   再需要逐笔内容时用 `parse_boe.py` 转 CSV，或直接读 `data/imports/fsc/*.json`。

## 核心原理（踩过的坑，照做）

1. **CDP 接管**：运行中的 Edge 无法 attach，必须带 `--remote-debugging-port` 启动。
   `attach_edge.py` 会复制一份 profile 到临时目录再启动，不动用户原浏览器。
2. **session cookie 不落盘**：复制 profile 后仍是未登录状态。必须让用户在新窗口
   手动登录一次，图形验证码只能人工输。**这是硬约束，不要试图自动化。**
3. **详情字段不在 DOM 里**：详情页是表单，`inner_text` 只有 label，值是空的。
   真实数据在 XHR 响应 JSON 里，脚本靠拦截 `--match` 指定的接口拿。
4. **详情是同页 iframe**：点下一笔前必须重载列表页，否则元素被遮挡、点击超时。
5. **别去解密码**：profile 里的 Edge 密码库有 App-Bound Encryption（`v20` 前缀），
   Python 侧解不开，靠「鼠标点用户名框触发自动填充」即可。

## 字段对照

需要知道某个字段在 JSON 里的键名时，读参考文件：

```
run_skill(id="fsc-boe-extract", ref="fsc-fields.md")
```

## 边界

- 只操作用户本人账号、本人有权查看的数据；不绕权限、不爬他人数据、不规避审计。
- 抓下来的 JSON 含收款账号、金额，默认只在本地 `data/imports/`，不要提交到仓库。
- 公司 IT 制度可能禁止自动化访问财务系统，这属于合规风险，提醒用户自行确认，
  **不要替用户下「没问题」的结论**。
- 抓取结果要入账时，走本系统正常的 `propose_import` / `propose_entry`，不要绕过审核。
