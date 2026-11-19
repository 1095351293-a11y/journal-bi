#!/usr/bin/env python
"""批量抓取列表页每一笔单据的接口响应 JSON。

两种模式:
  --dump-rows   只抓表格 DOM 文本（快，字段少）
  默认          逐笔点开详情，拦截指定接口的响应（准，字段全）

用法:
  python grab_boe.py --dump-rows --list-url "http://host/fsc/auditList" --out ./rows.json
  python grab_boe.py --list-url "http://host/fsc/auditList" --match getBoeDetailById \
      --items "NY-BX2611110070,NY-BX2611120051" --out ./boe_json
  python grab_boe.py --list-url "..." --match getBoeDetailById --from-row 5 --to-row 17 --out ./boe_json
"""
import argparse
import json
import os
import sys

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LIST_READY = "table tbody tr"


def load_list(pg, url, wait=5000):
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(wait)
    try:
        pg.wait_for_selector(LIST_READY, timeout=15000)
    except Exception:
        print("[!] 未等到表格行，可能未登录或列表未加载")


def dump_rows(pg):
    return pg.evaluate(
        """() => Array.from(document.querySelectorAll('table tbody tr'))
                 .map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()))"""
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--list-url", required=True)
    ap.add_argument("--match", default="getBoeDetailById", help="响应 URL 关键字")
    ap.add_argument("--items", default=None, help="单据号，逗号分隔")
    ap.add_argument("--from-row", type=int, default=None, help="按列表序号起（含）")
    ap.add_argument("--to-row", type=int, default=None, help="按列表序号止（含）")
    ap.add_argument("--dump-rows", action="store_true")
    ap.add_argument("--out", required=True, help="dump-rows 时为 .json 文件；否则为目录")
    ap.add_argument("--wait", type=int, default=5000, help="列表加载等待毫秒")
    a = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(a.cdp)
        ctx = b.contexts[0]
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.bring_to_front()

        load_list(pg, a.list_url, a.wait)

        if a.dump_rows:
            rows = dump_rows(pg)
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
            print(f"[✓] {len(rows)} 行 -> {a.out}")
            for r in rows[:40]:
                if any(r):
                    print("   ", json.dumps(r, ensure_ascii=False))
            return

        os.makedirs(a.out, exist_ok=True)

        items = []
        if a.items:
            items = [(None, x.strip()) for x in a.items.split(",") if x.strip()]
        elif a.from_row is not None:
            rows = dump_rows(pg)
            for r in rows:
                cells = [c for c in r if c]
                if not cells:
                    continue
                try:
                    idx = int(cells[0])
                except ValueError:
                    continue
                no = next((c for c in cells[1:3] if any(ch.isalpha() for ch in c) and "-" in c), None)
                if no is None:
                    continue
                if idx >= a.from_row and (a.to_row is None or idx <= a.to_row):
                    items.append((idx, no))
        else:
            raise SystemExit("需要 --items 或 --from-row")

        print(f"[i] 待抓 {len(items)} 笔")
        failures = []

        for idx, no in items:
            fn = os.path.join(a.out, f"{idx if idx else 'x'}_{no}.json")
            if os.path.exists(fn):
                print(f"[跳过] {no} 已存在")
                continue

            ok = False
            for attempt in (1, 2):
                try:
                    load_list(pg, a.list_url, a.wait)
                    with pg.expect_response(lambda r: a.match in r.url, timeout=45000) as ri:
                        pg.locator(f"text={no}").first.click(timeout=15000, force=True)
                    body = ri.value.text()
                    json.loads(body)
                    with open(fn, "w", encoding="utf-8") as f:
                        f.write(body)
                    print(f"[✓] {idx} {no}  {len(body)} bytes")
                    ok = True
                    break
                except Exception as e:
                    print(f"[!] {idx} {no} 第{attempt}次失败: {type(e).__name__} {str(e)[:110]}")
            if not ok:
                failures.append(no)

        if failures:
            with open(os.path.join(a.out, "_failures.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(failures))
            print(f"[!] 失败 {len(failures)} 笔，见 _failures.txt")


if __name__ == "__main__":
    main()
