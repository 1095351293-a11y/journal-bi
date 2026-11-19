#!/usr/bin/env python
"""把抓到的 FSC 单据 JSON 解析成明细表（CSV + 屏幕汇总）。

用法:
    python parse_boe.py --dir ./boe_json --out ./明细汇总.csv
"""
import argparse
import csv
import datetime
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def ts(v):
    if not v:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v)


def pick(d, *keys, default=""):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def parse_one(path):
    raw = json.load(open(path, encoding="utf-8"))
    d = raw.get("data") or {}
    fd = d.get("boeFormDto") or {}
    headers = fd.get("boeHeader") or [{}]
    h = headers[0]
    pays = fd.get("zfsBoePayments") or [{}]
    pay = pays[0]
    lines = fd.get("zfsBoeLines") or [{}]
    line = lines[0]

    fname = os.path.basename(path)
    idx = fname.split("_")[0] if "_" in fname else ""

    # 一笔单据可能拆多行费用，业务小类去重拼接
    sub_types = []
    for ln in lines:
        v = ln.get("operationSubTypeName")
        if v and v not in sub_types:
            sub_types.append(v)
    tax_total = sum(float(ln.get("taxAmount") or 0) for ln in lines)

    return {
        "序号": idx,
        "单据号": pick(h, "boeNo"),
        "单据日期": (pick(h, "boeDateStr") or "")[:10],
        "核算主体": pick(h, "leName"),
        "单据类型": pick(h, "boeTypeName"),
        "业务类型": pick(h, "operationTypeName"),
        "申请人": pick(h, "createName"),
        "部门": pick(h, "employeeDeptName"),
        "币种": pick(h, "standardCurrency"),
        "报销金额": pick(h, "applyAmount", "standardCurrencyAmount"),
        "税额": round(tax_total, 2),
        "摘要": pick(h, "boeAbstract"),
        "收款方": pick(pay, "vendorName"),
        "收款账号": pick(pay, "bankAccountNum"),
        "收款银行": pick(pay, "bankBranchName"),
        "付款账号": pick(pay, "paymentAccount"),
        "付款银行": pick(pay, "paymentBank"),
        "支付方式": pick(pay, "paymentModeName"),
        "资金用途": pick(pay, "paymentMemo"),
        "单据状态": pick(h, "boeStatusName"),
        "付款状态": pick(h, "paymentStatusName"),
        "审批完成": ts(h.get("lastApprovalDate")),
        "业务小类": "/".join(sub_types),
        "预算科目": pick(line, "budgetCode"),
        "预算科目名": pick(line, "budgetName"),
        "附件数": len(fd.get("zfsAttachmentDtos") or []),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    files = [f for f in os.listdir(a.dir) if f.endswith(".json")]
    files.sort(key=lambda x: (int(x.split("_")[0]) if x.split("_")[0].isdigit() else 999, x))

    rows = []
    for f in files:
        try:
            rows.append(parse_one(os.path.join(a.dir, f)))
        except Exception as e:
            print(f"[!] 解析失败 {f}: {e}")

    if not rows:
        raise SystemExit("没有可解析的 JSON")

    with open(a.out, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    total = 0.0
    for r in rows:
        try:
            total += float(str(r["报销金额"]).replace(",", ""))
        except Exception:
            pass
        print(f'{str(r["序号"]):>3} {r["单据号"]:<16} {str(r["业务类型"])[:14]:<16} '
              f'{str(r["报销金额"]):>12} {r["币种"]} | {str(r["摘要"])[:36]}')

    print(f"\n共 {len(rows)} 笔，合计 {total:,.2f}")
    print("付款账号:", sorted({r["付款账号"] for r in rows if r["付款账号"]}))
    print(f"已写入 {a.out}")


if __name__ == "__main__":
    main()
