# FSC 单据结构与字段映射

适用于中兴新云财务共享系统（FSC），页面路径 `/fsc/auditList`（共享运营）、`/boe/#/initialApproval`（单据详情）。

## 关键接口

| 接口 | 方法 | 用途 |
|---|---|---|
| `/sys/fsc/fscboebase/getBoeDetailById` | POST | **单据完整详情**，抓这个就够 |
| `/sys/fsc/fscvoucherheaders/{boeHeaderId}` | GET | 预制凭证信息 |
| `/sys/op/opboeheader/getFmcHistory` | POST | 审批历史 |
| `/sys/base/boetype/findBizSmallList` | POST | 业务小类字典 |
| `/sys/base/paymentmode/findPaymentMode` | POST | 支付方式字典 |

请求体含 `boeHeaderId`，从详情页 iframe URL 的查询参数里可以拿到：
`/boe/#/initialApproval?boeNo=NY-BX...&boeHeaderId=<32位hex>&boeTypeCode=...`

## JSON 结构

```
{
  "code": 0, "msg": "success",
  "data": {
    "boeNo": "NY-BX2611110070",
    "boeHeaderId": "a9410bde...",
    "boeTypeName": "日常费用报账单",
    "boeFormDto": {
      "boeHeader": [ {...} ],        // 表头，通常 1 条
      "zfsBoeLines": [ {...} ],      // 费用行
      "zfsBoePayments": [ {...} ],   // 支付信息（收款方、付款账户）
      "zfsAttachmentDtos": [ ... ],  // 附件清单
      "apportion": [...],            // 费用分摊
      "costSharing": [...],          // 费用归属
      "zfsBoeSumAmounts": {...},     // 金额合计
      "invoiceDTOS": [...]           // 发票（可能为空）
    }
  }
}
```

## 字段映射

### boeHeader（表头）

| 字段 | 含义 | 备注 |
|---|---|---|
| `boeNo` | 单据编号 | 如 `NY-BX2611110070` |
| `boeDateStr` | 单据日期 | `2026-11-11 16:13:29` |
| `leName` | 核算主体 | 如 `南粤科技有限公司` |
| `boeTypeName` | 单据类型 | 日常费用/差旅/招待费报账单 |
| `operationTypeName` | 业务类型 | 水电费、租赁费、业务招待费… |
| `createName` | 申请人 | |
| `employeeDeptName` | 部门 | |
| `boeAbstract` | 摘要 | 真正的业务事由，登记日记账用它 |
| `applyAmount` | 报销金额 | 数值，未格式化 |
| `standardCurrency` | 报账币种 | `MOP` / `HKD` / `CNY` |
| `standardCurrencyName` | 币种名 | `澳门元` 等 |
| `boeStatusName` | 单据状态 | `待共享财务初审审批` 等 |
| `paymentStatusName` | 付款状态 | `待付款` / `已付款` |
| `lastApprovalDate` | 最后审批时间 | **毫秒时间戳**，需 /1000 转 datetime |
| `bktxt` | 合并描述 | `部门+姓名+职位+摘要` |
| `vendorName` | 往来方 | 表头级，支付级更准 |

### zfsBoePayments（支付信息）

| 字段 | 含义 | 备注 |
|---|---|---|
| `vendorName` | 收款方 | |
| `bankAccountNum` | 收款账号 | 常被脱敏为 `1832*******7650` |
| `bankBranchName` | 收款银行开户行 | |
| `paymentAccount` | **付款账号** | 如 `0120000004029-MOP`，与银行日记账对账的关键 |
| `paymentBank` | 付款银行 | 如 `澳门华人银行股份有限公司` |
| `paymentModeName` | 支付方式 | 汇款 / 转账 |
| `paymentMemo` | 资金用途 | |
| `paymentAmount` | 支付金额 | |
| `paymentStatusName` | 付款状态 | |

### zfsBoeLines（费用行）

一笔单据可能拆成多行费用，字段要跨行聚合。

| 字段 | 含义 | 示例 |
|---|---|---|
| `operationSubTypeName` | **业务小类** | `水电费-管理` |
| `budgetCode` | 预算科目编码 | `6602.11` |
| `budgetName` | 预算科目名 | `管理费用_办公费` |
| `expenseAmount` | 费用金额 | |
| `feeWithoutTax` | 不含税金额 | |
| `taxAmount` | 税额 | |
| `expenseDate` | 费用发生日期 | `2026-10-16` |
| `billTypeInvoiceName` | 票据类型 | `其他纸质票` |

⚠️ 容易猜错的字段名：没有 `bizSmallName` / `expenseTypeName` / `budgetSubjectCode`，别凭直觉写映射，先 dump 一条 JSON 的实际键名。

## 常见状态值

| 字段 | 值 | 含义 |
|---|---|---|
| `boeStatus` | 20 / 25 | 审批中 / 待共享财务初审审批 |
| `paymentStatus` | 10 | 待付款 |
| `evsImgStatus` | UPLOADED | 影像已上传 |

## 踩过的坑

1. **详情字段在 `input`/`select` 里，`inner_text` 读不到值**。只能拦接口。
2. **点完一笔必须重载列表**，否则下一笔的按钮被 iframe 遮挡，`click` 15 秒超时。用 `force=True` 也无效。
3. **列表行数 ≠ 单据数**：表格常含固定行，17 笔能返回 34 个 `tr`。按第 2 列的序号或单据号过滤。
4. **同一笔可能重复出现在日记账/流水里**（先付款后补报账单）。落库前按「金额 + 收款方 + 近 60 天」查重。
5. **付款账号币种可能与单据币种不一致**（例如单据标 MOP，付款账户是人民币户）。按付款账号实际账户登记，并在备注里记原币种。
6. `boeStatusName` 是「待付款」不代表单据没审批完——两者是独立字段，都要看。
