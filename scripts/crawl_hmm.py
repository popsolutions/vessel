"""Selenium-driven crawler that walks the HMM web UI to fire every XHR.

The point of this script is not to *parse* the UI — it is to **drive** it
so that a separately-running ``mitmdump -s scripts/capture_hmm.py``
records every endpoint the JS issues. We click every menu item, every
list-row, every "details" button reachable from the dashboard, with a
short settle delay so XHRs land before we move on.

Run mitmdump first, then::

    HMM_HOST=192.168.1.30 HMM_USER=root HMM_PASSWORD=... \\
    HMM_CAPTURE_DIR=$PWD/discovery/api/20260502T143015Z \\
    python scripts/crawl_hmm.py --proxy http://127.0.0.1:8080

The crawl is deliberately conservative: it only follows links/buttons
that look read-only (``GET``-ish — list, view, details, info). Anything
matching the destructive vocabulary (``delete``, ``reset``, ``power``,
``apply``, ``commit``, ``upgrade``, ``restore``) is skipped — we are
mapping the API surface, not driving the chassis.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("hmm-crawl")

# Vocabulary we refuse to click. We're enumerating endpoints, not driving ops.
_DESTRUCTIVE = re.compile(
    r"(delete|remove|reset|reboot|shutdown|power\s*off|power\s*cycle|"
    r"apply|commit|save|upgrade|update\s+firmware|restore|wipe|format|"
    r"clear|factory|enable|disable|start|stop)",
    re.IGNORECASE,
)

# Things we actively *want* to click — read-only verbs.
_SAFE_HINT = re.compile(
    r"(list|view|details?|info|inventory|status|monitor|overview|"
    r"summary|show|display|read)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CrawlConfig:
    base_url: str
    username: str
    password: str
    proxy: str | None
    capture_dir: Path
    max_clicks: int
    settle_seconds: float
    headless: bool

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "CrawlConfig":
        host = os.environ.get("HMM_HOST")
        if not host:
            raise SystemExit("HMM_HOST is required")
        user = os.environ.get("HMM_USER")
        pwd = os.environ.get("HMM_PASSWORD")
        if not (user and pwd):
            raise SystemExit("HMM_USER and HMM_PASSWORD are required")
        capture_dir = os.environ.get("HMM_CAPTURE_DIR")
        if capture_dir:
            cap_path = Path(capture_dir).resolve()
        else:
            stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            cap_path = (Path.cwd() / "discovery" / "api" / stamp).resolve()
        cap_path.mkdir(parents=True, exist_ok=True)
        return cls(
            base_url=args.base_url or f"https://{host}/",
            username=user,
            password=pwd,
            proxy=args.proxy,
            capture_dir=cap_path,
            max_clicks=args.max_clicks,
            settle_seconds=args.settle,
            headless=args.headless,
        )


class CrawlLogger:
    """Append-only JSONL log of every action the crawler takes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fp = path.open("a", encoding="utf-8")

    def event(
        self,
        action: str,
        *,
        selector: str | None = None,
        url_before: str | None = None,
        url_after: str | None = None,
        ok: bool = True,
        error: str | None = None,
        extra: dict | None = None,
    ) -> None:
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "action": action,
            "selector": selector,
            "url_before": url_before,
            "url_after": url_after,
            "ok": ok,
            "error": error,
        }
        if extra:
            record.update(extra)
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

    def close(self) -> None:
        self._fp.close()


def _build_driver(cfg: CrawlConfig) -> webdriver.Chrome:
    opts = Options()
    if cfg.headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--window-size=1600,1000")
    if cfg.proxy:
        opts.add_argument(f"--proxy-server={cfg.proxy}")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)


def _try_login(driver: webdriver.Chrome, cfg: CrawlConfig, crawl_log: CrawlLogger) -> bool:
    """Best-effort login: find a password field, fill the closest text/email
    field, submit. The HMM form selectors aren't documented, so we feel
    around and log what we did.
    """
    driver.get(cfg.base_url)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type=password]"))
        )
    except TimeoutException:
        crawl_log.event(
            "login", ok=False, error="no password input found within 15s",
            url_after=driver.current_url,
        )
        return False

    pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type=password]")
    user_input = _find_user_input(driver, pwd_input)
    if user_input is None:
        crawl_log.event(
            "login", ok=False, error="no plausible username field",
            url_after=driver.current_url,
        )
        return False

    user_input.clear()
    user_input.send_keys(cfg.username)
    pwd_input.clear()
    pwd_input.send_keys(cfg.password)

    submitted = _click_submit(driver, pwd_input)
    if not submitted:
        from selenium.webdriver.common.keys import Keys

        pwd_input.send_keys(Keys.RETURN)

    time.sleep(cfg.settle_seconds * 2)
    ok = "login" not in driver.current_url.lower() or _is_dashboardish(driver)
    crawl_log.event(
        "login", ok=ok, url_after=driver.current_url,
        extra={"submitted_via_button": submitted},
    )
    return ok


def _find_user_input(driver: webdriver.Chrome, pwd_input: WebElement) -> WebElement | None:
    candidates = driver.find_elements(
        By.CSS_SELECTOR,
        "input[type=text], input[type=email], input:not([type])",
    )
    if not candidates:
        return None
    hint = re.compile(r"(user|login|account|name)", re.IGNORECASE)
    for el in candidates:
        for attr in ("name", "id", "placeholder", "aria-label"):
            value = el.get_attribute(attr) or ""
            if hint.search(value):
                return el
    for el in candidates:
        if el.is_displayed():
            return el
    return None


def _click_submit(driver: webdriver.Chrome, pwd_input: WebElement) -> bool:
    selectors = (
        "button[type=submit]",
        "input[type=submit]",
        "button#login",
        "button.login",
        "button[name=login]",
    )
    for sel in selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
        except NoSuchElementException:
            continue
        try:
            btn.click()
            return True
        except (ElementClickInterceptedException, ElementNotInteractableException):
            continue
    return False


def _is_dashboardish(driver: webdriver.Chrome) -> bool:
    body_text = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    return any(token in body_text for token in ("logout", "sign out", "dashboard", "chassis"))


def _candidate_clickables(driver: webdriver.Chrome) -> list[WebElement]:
    selectors = (
        "a[href]",
        "[role=menuitem]",
        "[role=tab]",
        "button",
        "li.menu-item",
        ".nav a",
        ".sidebar a",
    )
    seen: set[str] = set()
    out: list[WebElement] = []
    for sel in selectors:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                key = el.id
            except StaleElementReferenceException:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(el)
    return out


def _label_of(el: WebElement) -> str:
    for attr in ("aria-label", "title", "data-original-title"):
        v = el.get_attribute(attr)
        if v:
            return v.strip()
    try:
        text = (el.text or "").strip()
    except StaleElementReferenceException:
        return ""
    return text


def _is_safe_to_click(label: str, href: str | None) -> bool:
    if _DESTRUCTIVE.search(label or ""):
        return False
    if href and _DESTRUCTIVE.search(href):
        return False
    if label and _SAFE_HINT.search(label):
        return True
    if href and ("javascript:" in href.lower()):
        return not _DESTRUCTIVE.search(label or "")
    return bool(href) or bool(label)


def crawl(driver: webdriver.Chrome, cfg: CrawlConfig, crawl_log: CrawlLogger) -> None:
    visited_urls: set[str] = set()
    clicks = 0

    url_before = driver.current_url
    if url_before in visited_urls:
        return
    visited_urls.add(url_before)
    crawl_log.event("navigate", url_after=url_before)

    items: Iterable[WebElement] = _candidate_clickables(driver)
    plan: list[tuple[str, str, str]] = []
    for el in items:
        try:
            href = el.get_attribute("href") or ""
            label = _label_of(el)
            tag = el.tag_name
        except StaleElementReferenceException:
            continue
        if not _is_safe_to_click(label, href):
            continue
        plan.append((tag, label, href))
    log.info("page %s -> %d candidate clickables", url_before, len(plan))

    for tag, label, href in plan:
        if clicks >= cfg.max_clicks:
            return
        if href and href.startswith(("http://", "https://")) and href not in visited_urls:
            target = urljoin(cfg.base_url, href)
            try:
                driver.get(target)
                time.sleep(cfg.settle_seconds)
                clicks += 1
                visited_urls.add(driver.current_url)
                crawl_log.event(
                    "navigate",
                    selector=f"{tag}:{label[:60]}",
                    url_before=url_before,
                    url_after=driver.current_url,
                )
            except WebDriverException as exc:
                crawl_log.event(
                    "navigate",
                    selector=f"{tag}:{label[:60]}",
                    url_before=url_before,
                    url_after=target,
                    ok=False,
                    error=str(exc)[:200],
                )
            try:
                driver.back()
                time.sleep(cfg.settle_seconds)
            except WebDriverException:
                driver.get(url_before)
        else:
            _click_by_label(driver, tag, label, cfg, crawl_log, url_before)
            clicks += 1
            if clicks >= cfg.max_clicks:
                return

    log.info("crawl finished: %d clicks, %d urls visited", clicks, len(visited_urls))


def _click_by_label(
    driver: webdriver.Chrome,
    tag: str,
    label: str,
    cfg: CrawlConfig,
    crawl_log: CrawlLogger,
    url_before: str,
) -> None:
    if not label:
        return
    xpath = f"//{tag}[normalize-space()={json.dumps(label)}]"
    try:
        el = driver.find_element(By.XPATH, xpath)
    except NoSuchElementException:
        crawl_log.event(
            "click", selector=xpath, ok=False, error="not found on second pass",
            url_before=url_before,
        )
        return
    try:
        el.click()
    except (ElementClickInterceptedException, ElementNotInteractableException) as exc:
        crawl_log.event(
            "click", selector=xpath, ok=False, error=str(exc)[:200],
            url_before=url_before,
        )
        return
    time.sleep(cfg.settle_seconds)
    crawl_log.event(
        "click", selector=xpath, url_before=url_before, url_after=driver.current_url,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=None, help="Override https://<HMM_HOST>/")
    p.add_argument("--proxy", default=None, help="HTTP proxy (e.g. http://127.0.0.1:8080)")
    p.add_argument("--max-clicks", type=int, default=400)
    p.add_argument("--settle", type=float, default=1.5, help="Seconds to wait after each click")
    p.add_argument("--headless", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = CrawlConfig.from_env(args)
    log.info("capture dir: %s", cfg.capture_dir)
    log.info("base url: %s, proxy: %s", cfg.base_url, cfg.proxy or "<none>")

    crawl_log = CrawlLogger(cfg.capture_dir / "_crawl_log.jsonl")
    driver = _build_driver(cfg)
    try:
        if not _try_login(driver, cfg, crawl_log):
            log.error("login failed - see _crawl_log.jsonl for details")
            return 2
        crawl(driver, cfg, crawl_log)
    except KeyboardInterrupt:
        log.warning("interrupted by user")
    finally:
        crawl_log.close()
        driver.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
