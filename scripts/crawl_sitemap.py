"""Sitemap-driven crawler for the HMM web UI.

Unlike ``crawl_hmm.py`` (which clicks every visible element and gets
stuck in a loop because the HMM SPA never changes URL on click), this
script walks a **list of known pages** directly via ``driver.get(...)``,
waits for the page's XHRs to settle, then moves on.

The page list comes from mining captured HTML in a previous run — the
HMM ships its menu links inline (see ``index.html``), giving us the
full sitemap without any guessing.

Usage::

    HMM_CAPTURE_DIR=$PWD/discovery/api/<stamp> \\
    python scripts/crawl_sitemap.py --proxy http://127.0.0.1:8080

Pair with a running mitmdump on port 8080 — it's the mitm that
actually persists the captured flows.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("hmm-sitemap")

# Sitemap mined from captured HTML on 2026-05-02 (run 20260502T193022Z).
STATIC_PAGES: tuple[str, ...] = (
    "alarm_config.html",
    "blackbox_switch_control.html",
    "cabinet_basic_setting.html",
    "cabinet_bios.html",
    "cabinet_blade.html",
    "cabinet_configrecovery.html",
    "cabinet_fc_port.html",
    "cabinet_smm.html",
    "cabinet_swi.html",
    "cabinet_umate_setting.html",
    "computer_manage.html",
    "computer_manage_mac_manage.html",
    "computer_manage_node_manage.html",
    "computer_manage_uuid_manage.html",
    "pem.html",
    "power.html",
    "smm.html",
    "switch_config_central_control.html",
    "system_manage.html",
    "system_manage_ntp.html",
)

# Slots populated on this chassis (per project_hardware.md):
# 10x CH121 in 1,2,3,4,8,9,10,11,12,16; 3x CH222 in 13,14,15.
BLADE_SLOTS: tuple[int, ...] = (1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14, 15, 16)
FAN_SLOTS: tuple[int, ...] = (1, 2, 3, 4, 5, 7, 8, 10, 11, 13)
SWITCH_SLOTS: tuple[str, ...] = ("Swi2", "Swi3")


def _build_pages() -> list[str]:
    pages = [f"{p}?chassisid=0" for p in STATIC_PAGES]
    pages += [f"blade.html?chassisid=0&bladename=Slot{n}" for n in BLADE_SLOTS]
    pages += [f"fan.html?chassisid=0&bladename=Fan{n}" for n in FAN_SLOTS]
    pages += [f"switch.html?chassisid=0&bladename={s}" for s in SWITCH_SLOTS]
    return pages


def _build_driver(proxy: str | None, headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--window-size=1600,1000")
    if proxy:
        opts.add_argument(f"--proxy-server={proxy}")
    return webdriver.Chrome(options=opts)


def _login(driver: webdriver.Chrome, base_url: str, user: str, pwd: str, settle: float) -> bool:
    driver.get(base_url)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type=password]"))
        )
    except TimeoutException:
        return False
    pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type=password]")
    user_inputs = driver.find_elements(
        By.CSS_SELECTOR, "input[type=text], input[type=email], input:not([type])"
    )
    if not user_inputs:
        return False
    user_inputs[0].clear()
    user_inputs[0].send_keys(user)
    pwd_input.clear()
    pwd_input.send_keys(pwd)
    try:
        driver.find_element(By.CSS_SELECTOR, "button[type=submit], input[type=submit]").click()
    except WebDriverException:
        from selenium.webdriver.common.keys import Keys

        pwd_input.send_keys(Keys.RETURN)
    time.sleep(settle * 2)
    return "login" not in driver.current_url.lower()


def _resolve_run_dir() -> Path:
    override = os.environ.get("HMM_CAPTURE_DIR")
    if override:
        return Path(override).resolve()
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (Path.cwd() / "discovery" / "api" / stamp).resolve()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=None)
    p.add_argument("--proxy", default=None)
    p.add_argument("--settle", type=float, default=2.5)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="Stop after N pages (0 = all)")
    args = p.parse_args()

    host = os.environ.get("HMM_HOST")
    user = os.environ.get("HMM_USER")
    pwd = os.environ.get("HMM_PASSWORD")
    if not (host and user and pwd):
        raise SystemExit("HMM_HOST / HMM_USER / HMM_PASSWORD required (set in .env)")

    base_url = args.base_url or f"https://{host}/"
    run_dir = _resolve_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "_sitemap_log.jsonl"
    log.info("capture dir: %s", run_dir)
    log.info("base url: %s, proxy: %s", base_url, args.proxy or "<none>")

    pages = _build_pages()
    if args.limit:
        pages = pages[: args.limit]
    log.info("walking %d pages", len(pages))

    driver = _build_driver(args.proxy, args.headless)
    fp = log_path.open("a", encoding="utf-8")
    visited = 0
    try:
        if not _login(driver, base_url, user, pwd, args.settle):
            log.error("login failed")
            return 2
        log.info("logged in, starting walk")

        for page in pages:
            target = base_url.rstrip("/") + "/" + page
            event: dict[str, str | bool | None] = {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "page": page,
                "ok": True,
                "title": "",
                "error": None,
            }
            try:
                driver.get(target)
                time.sleep(args.settle)
                event["title"] = (driver.title or "")[:120]
                visited += 1
                log.info("[%d/%d] %s -> %s", visited, len(pages), page, event["title"])
            except WebDriverException as exc:
                event["ok"] = False
                event["error"] = str(exc)[:200]
                log.warning("[%d/%d] %s FAILED: %s", visited + 1, len(pages), page, exc)
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
            fp.flush()
    except KeyboardInterrupt:
        log.warning("interrupted by user")
    finally:
        fp.close()
        driver.quit()
    log.info("walked %d / %d pages; log: %s", visited, len(pages), log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
