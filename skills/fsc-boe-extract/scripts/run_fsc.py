#!/usr/bin/env python
"""一键流程：启动浏览器 → 等待登录 → 批量抓详情 → 解析成表。

不需要拆成多步，适合"打开系统、点开每一笔、导出明细"这种整段任务。

用法:
  python run_fsc.py --list-url "http://10.15.1.133/fsc/auditList" \
      --match getBoeDetailById --from-row 5 --to-row 17 --out ./out

  python run_fsc.py --list-url "..." --items "NY-BX2611110070,..." --out ./out

登录说明:
  浏览器用 profile 副本启动，首次为未登录状态。脚本会暂停并提示用户在新窗口
  手动输入验证码登录，检测到离开登录页后自动继续。登录态会留在副本里，
  同一 --work-dir 下次运行通常无需重复登录。
"""
import argparse
import json
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "Service Worker", "DawnCache", "ShaderCache",
    "GrShaderCache", "Media Cache", "BrowserMetrics", "Crashpad",
    "component_crx_cache", "extensions_crx_cache",
}


def copy_profile(src, dst):
    if os.path.isdir(dst) and os.path.exists(os.path.join(dst, "Local State")):
        print(f"[i] 复用 profile 副本: {dst}")
        return
    print(f"[i] 首次运行，复制 profile -> {dst}（约 300MB，需十几秒）")
    os.makedirs(dst, exist_ok=True)
    ignore = shutil.ignore_patterns(*SKIP_DIRS, "*.pma", "*.log")
    skipped = 0
    for item in os.listdir(src):
        if item in SKIP_DIRS:
            continue
        sp, dp = os.path.join(src, item), os.path.join(dst, item)
        try:
            if os.path.isdir(sp):
                shutil.copytree(sp, dp, ignore=ignore, dirs_exist_ok=True,
                                ignore_dangling_symlinks=True)
            else:
                shutil.copy2(sp, dp)
        except shutil.Error as e:
            skipped += len(e.args[0])
        except Exception:
            skipped += 1
    if skipped:
        print(f"[i] {skipped} 个文件被运行中的 Edge 锁定未复制（不影响使用）")


def load_list(pg, url, wait):
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(wait)
    try:
        pg.wait_for_selector("table tbody tr", timeout=15000)
    except Exception:
        print("[!] 未等到表格行")


def dump_rows(pg):
    return pg.evaluate(
        """() => Array.from(document.querySelectorAll('table tbody tr'))
                 .map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()))"""
    )


def wait_login(pg, timeout):
    if "login" not in pg.url.lower():
        print("[✓] 已是登录态")
        return True
    print("\n" + "=" * 62)
    print("需要登录：请在刚弹出的浏览器窗口里输入账号、密码、验证码。")
    print(f"检测到离开登录页后自动继续，最长等待 {timeout} 秒。")
    print("=" * 62 + "\n")
    t0 = time.time()
    while time.time() - t0 < timeout:
        pg.wait_for_timeout(2000)
        if "login" not in pg.url.lower():
            print(f"[✓] 登录完成({int(time.time()-t0)}s)，继续执行")
            pg.wait_for_timeout(3000)
            return True
    print("[✗] 登录超时")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-url", required=True)
    ap.add_argument("--match", default="getBoeDetailById")
    ap.add_argument("--items", default=None)
    ap.add_argument("--from-row", type=int, default=None)
    ap.add_argument("--to-row", type=int, default=None)
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--profile-src", default=None)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--login-timeout", type=int, default=300)
    ap.add_argument("--wait", type=int, default=5000)
    ap.add_argument("--keep-open", action="store_true", help="结束后不关闭浏览器")
    a = ap.parse_args()

    src = a.profile_src or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")
    work = a.work_dir or os.path.join(os.environ.get("TEMP", "/tmp"), "edge_cdp_profile")
    copy_profile(src, work)

    json_dir = os.path.join(a.out, "json")
    os.makedirs(json_dir, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=work, channel="msedge", headless=False,
            args=["--no-first-run", "--no-default-browser-check",
                  "--disable-features=msEdgeFirstRunExperience"],
            viewport={"width": 1680, "height": 1000},
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        load_list(pg, a.list_url, a.wait)
        if not wait_login(pg, a.login_timeout):
            ctx.close()
            sys.exit(1)
        load_list(pg, a.list_url, a.wait)

        # 确定待抓清单
        if a.items:
            items = [(None, x.strip()) for x in a.items.split(",") if x.strip()]
        elif a.from_row is not None:
            items = []
            for r in dump_rows(pg):
                cells = [c for c in r if c]
                if not cells:
                    continue
                try:
                    idx = int(cells[0])
                except ValueError:
                    continue
                no = next((c for c in cells[1:3] if "-" in c and any(ch.isalpha() for ch in c)), None)
                if no and idx >= a.from_row and (a.to_row is None or idx <= a.to_row):
                    items.append((idx, no))
        else:
            for r in dump_rows(pg):
                if any(r):
                    print("   ", json.dumps(r, ensure_ascii=False))
            raise SystemExit("未指定 --items 或 --from-row")

        print(f"[i] 待抓 {len(items)} 笔")
        failures = []
        for idx, no in items:
            fn = os.path.join(json_dir, f"{idx if idx else 'x'}_{no}.json")
            if os.path.exists(fn):
                print(f"[跳过] {no}")
                continue
            ok = False
            for attempt in (1, 2):
                try:
                    load_list(pg, a.list_url, a.wait)
                    with pg.expect_response(lambda r: a.match in r.url, timeout=45000) as ri:
                        pg.locator(f"text={no}").first.click(timeout=15000, force=True)
                    body = ri.value.text()
                    json.loads(body)
                    open(fn, "w", encoding="utf-8").write(body)
                    print(f"[✓] {idx} {no}  {len(body)} bytes")
                    ok = True
                    break
                except Exception as e:
                    print(f"[!] {idx} {no} 第{attempt}次失败: {type(e).__name__} {str(e)[:100]}")
            if not ok:
                failures.append(no)

        if failures:
            open(os.path.join(json_dir, "_failures.txt"), "w", encoding="utf-8").write("\n".join(failures))
            print(f"[!] 失败 {len(failures)} 笔")

        if not a.keep_open:
            ctx.close()

    # 解析
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from parse_boe import parse_one
    rows = []
    for f in sorted(os.listdir(json_dir)):
        if f.endswith(".json"):
            try:
                rows.append(parse_one(os.path.join(json_dir, f)))
            except Exception as e:
                print(f"[!] 解析失败 {f}: {e}")
    if rows:
        import csv
        out_csv = os.path.join(a.out, "明细汇总.csv")
        with open(out_csv, "w", encoding="utf-8-sig", newline="") as fp:
            w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        total = sum(float(str(r["报销金额"]).replace(",", "") or 0) for r in rows)
        print(f"\n[✓] {len(rows)} 笔，合计 {total:,.2f} -> {out_csv}")


if __name__ == "__main__":
    main()
