#!/usr/bin/env python
"""导航到目标页并报告登录态 / 页面概况。

用法:
    python peek.py --cdp http://127.0.0.1:9222 --goto "http://10.15.1.133/fsc/auditList"
    python peek.py --cdp http://127.0.0.1:9222 --goto "<url>" --click-user --shot out.png
"""
import argparse
import sys

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--goto", default=None)
    ap.add_argument("--click-user", action="store_true", help="点击首个文本框触发密码库自动填充")
    ap.add_argument("--shot", default=None, help="截图保存路径")
    ap.add_argument("--wait", type=int, default=6000, help="等待毫秒，默认 6000")
    a = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(a.cdp)
        ctx = b.contexts[0]
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.bring_to_front()

        if a.goto:
            pg.goto(a.goto, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(a.wait)

        print("URL  :", pg.url)
        print("TITLE:", pg.title())
        print("FRAMES:", len(pg.frames))

        body = pg.inner_text("body")[:400].replace("\n", " | ")
        print("BODY :", body)

        if "login" in pg.url.lower():
            print("\n[!] 当前在登录页 —— 请用户在新窗口手动登录后再继续")

        if a.click_user:
            inputs = pg.query_selector_all("button, input[type=text], input:not([type])")
            if inputs:
                try:
                    inputs[0].click()
                    pg.wait_for_timeout(1200)
                    vals = [i.input_value() for i in pg.query_selector_all("input")]
                    print("[i] 自动填充后输入框值:", [v for v in vals if v][:4])
                except Exception as e:
                    print("[!] 点击失败:", e)

        if a.shot:
            pg.screenshot(path=a.shot)
            print("[i] 截图:", a.shot)


if __name__ == "__main__":
    main()
