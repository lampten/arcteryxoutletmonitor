#!/usr/bin/env python3
"""
Unified entrypoint: runs both catalog-change monitoring (new/price-change/removed)
and size restock monitoring.

Config is JSON. Telegram credentials are configured via environment variables:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID (or TELEGRAM_CHAT_IDS)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from logging_utils import setup_logging

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = "monitor_config.json"
DEFAULT_LOG_FILE = os.path.join("logs", "monitor_unified.log")


def now_local_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_baseline_products(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    try:
        data = load_json_file(path)
        if isinstance(data, dict):
            products = data.get("products") or []
        else:
            products = data
        return products if isinstance(products, list) else []
    except Exception:
        return []


def save_baseline_products(path: str, products: List[dict]) -> None:
    save_json_file(
        path,
        {
            "products": products,
            "count": len(products),
            "timestamp": datetime.now().isoformat(),
        },
    )


def compare_catalog_products(old_products: List[dict], new_products: List[dict]) -> Dict[str, Any]:
    old_ids = {str(p.get("id")): p for p in old_products if p.get("id")}
    new_ids = {str(p.get("id")): p for p in new_products if p.get("id")}

    added = [p for pid, p in new_ids.items() if pid not in old_ids]
    removed = [p for pid, p in old_ids.items() if pid not in new_ids]

    price_changes: List[Dict[str, Any]] = []
    for pid in set(old_ids.keys()) & set(new_ids.keys()):
        old_price = old_ids[pid].get("price")
        new_price = new_ids[pid].get("price")
        if old_price and new_price and old_price != new_price:
            price_changes.append(
                {
                    "product": new_ids[pid],
                    "old_price": old_price,
                    "new_price": new_price,
                }
            )

    return {"added": added, "removed": removed, "price_changes": price_changes}


def apply_change_filters(changes: Dict[str, Any], notify_cfg: Dict[str, Any]) -> Dict[str, Any]:
    enabled_added = bool(notify_cfg.get("added", True))
    enabled_removed = bool(notify_cfg.get("removed", True))
    enabled_price = bool(notify_cfg.get("price_changes", True))

    if not enabled_added:
        changes = dict(changes)
        changes["added"] = []
    if not enabled_removed:
        changes = dict(changes)
        changes["removed"] = []
    if not enabled_price:
        changes = dict(changes)
        changes["price_changes"] = []
    return changes


def parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def fetch_catalog_products_uc(
    *,
    url: str,
    headless: bool = True,
    render_wait_seconds: int = 20,
    scroll_times: int = 3,
    scroll_sleep_seconds: float = 3.0,
    max_products: int = 0,
    chrome_version_main: Optional[int] = None,
    page_load_timeout_seconds: int = 90,
) -> List[dict]:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By

    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    if headless:
        options.headless = True

    driver = None
    try:
        try:
            if chrome_version_main:
                driver = uc.Chrome(options=options, version_main=int(chrome_version_main))
            else:
                driver = uc.Chrome(options=options)
        except Exception:
            driver = uc.Chrome(options=options)

        driver.set_page_load_timeout(page_load_timeout_seconds)
        driver.get(url)

        if render_wait_seconds > 0:
            time.sleep(render_wait_seconds)

        for _ in range(max(0, scroll_times)):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(max(0.0, scroll_sleep_seconds))

        elems = driver.find_elements(By.CSS_SELECTOR, ".qa--product-tile__link, a[href*='/shop/']")
        products: List[dict] = []
        seen: set[str] = set()

        for e in elems:
            href = e.get_attribute("href")
            if not href or "/shop/" not in href:
                continue
            href = href.split("?")[0]
            if href in seen:
                continue
            seen.add(href)

            product_id = href.rstrip("/").split("/")[-1] if href else ""
            name = ""
            price = ""

            node = e
            for _ in range(8):
                try:
                    node = node.find_element(By.XPATH, "..")
                except Exception:
                    break

                if not name:
                    try:
                        name_elems = node.find_elements(By.CSS_SELECTOR, ".product-tile-name, [class*='product-tile-name'], [class*='tile-name']")
                        for ne in name_elems:
                            t = (ne.text or "").strip()
                            if t:
                                name = t
                                break
                    except Exception:
                        pass

                if not price:
                    try:
                        price_elems = node.find_elements(By.CSS_SELECTOR, ".qa--product-tile__prices, [class*='price']")
                        if price_elems:
                            t = " ".join((price_elems[0].text or "").split())
                            if t:
                                price = t
                    except Exception:
                        pass

                if name and price:
                    break

            if not name:
                name = product_id or href

            products.append(
                {
                    "id": product_id or href,
                    "name": name,
                    "price": price or None,
                    "link": href,
                    "timestamp": datetime.now().isoformat(),
                }
            )

            if max_products and max_products > 0 and len(products) >= max_products:
                break

        return products
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def run_catalog_changes_task(task: Dict[str, Any], *, dry_run: bool) -> bool:
    name = str(task.get("name") or "catalog")
    url = str(task.get("url") or task.get("target_url") or "")
    if not url:
        raise ValueError("catalog_changes task requires `url`")

    baseline_file = str(task.get("baseline_file") or os.path.join("data", f"catalog_baseline_{name}.json"))
    notify_on_first_run = parse_bool(task.get("notify_on_first_run"), False)
    notify_cfg = task.get("notify") if isinstance(task.get("notify"), dict) else {}

    headless = parse_bool(task.get("headless"), True)
    render_wait_seconds = parse_int(task.get("render_wait_seconds"), 20)
    scroll_times = parse_int(task.get("scroll_times"), 3)
    scroll_sleep_seconds = parse_float(task.get("scroll_sleep_seconds"), 3.0)
    max_products = parse_int(task.get("max_products"), 0)
    chrome_version_main = task.get("chrome_version_main")
    if chrome_version_main is None:
        env_ver = os.getenv("CHROME_VERSION_MAIN")
        chrome_version_main = int(env_ver) if env_ver and env_ver.isdigit() else None
    else:
        chrome_version_main = int(chrome_version_main)

    baseline_exists = os.path.exists(baseline_file)
    old_products = load_baseline_products(baseline_file) if baseline_exists else []

    logger.info(f"[{name}] Fetching catalog products: {url}")
    new_products = fetch_catalog_products_uc(
        url=url,
        headless=headless,
        render_wait_seconds=render_wait_seconds,
        scroll_times=scroll_times,
        scroll_sleep_seconds=scroll_sleep_seconds,
        max_products=max_products,
        chrome_version_main=chrome_version_main,
    )
    logger.info(f"[{name}] Current products: {len(new_products)} baseline_exists={baseline_exists}")

    if not baseline_exists:
        save_baseline_products(baseline_file, new_products)
        logger.info(f"[{name}] First run: baseline written to {baseline_file}")

        if notify_on_first_run and not dry_run:
            from telegram_notifier import TelegramNotifier

            notifier = TelegramNotifier()
            notifier.send_message(
                "\n".join(
                    [
                        f"🏔️ Arc'teryx Outlet baseline created: {name}",
                        f"Time: {now_local_str()}",
                        f"Products: {len(new_products)}",
                        f"URL: {url}",
                    ]
                )
            )
        return True

    changes = compare_catalog_products(old_products, new_products)
    changes = apply_change_filters(changes, notify_cfg)

    added_n = len(changes.get("added") or [])
    removed_n = len(changes.get("removed") or [])
    price_n = len(changes.get("price_changes") or [])
    has_changes = any([added_n, removed_n, price_n])

    if has_changes:
        logger.info(f"[{name}] Changes detected: added={added_n} price_changes={price_n} removed={removed_n}")
        if not dry_run:
            from telegram_notifier import send_change_notification

            send_change_notification(changes)
    else:
        logger.info(f"[{name}] No changes")

    save_baseline_products(baseline_file, new_products)
    return True


def run_stock_watch_task(task: Dict[str, Any], *, dry_run: bool) -> bool:
    import watch_stock

    config_data: Optional[Dict[str, Any]] = None
    if isinstance(task.get("config"), dict):
        config_data = task["config"]
    elif isinstance(task.get("config_file"), str) and task.get("config_file"):
        config_data = load_json_file(task["config_file"])
        if not isinstance(config_data, dict):
            raise ValueError("stock_watch config_file must point to a JSON object")
    else:
        # Allow inlining watch_stock config directly in the task (excluding type/name).
        config_data = {k: v for k, v in task.items() if k not in {"type", "name"}}

    cfg = watch_stock.build_config_from_file(config_data)
    rc = watch_stock.run_stock_watch(cfg, dry_run=dry_run or bool(task.get("dry_run")))
    return rc == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Arc'teryx unified runner (catalog_changes + stock_watch)")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG, help=f"Unified JSON config (default: {DEFAULT_CONFIG})")
    parser.add_argument("--dry-run", action="store_true", help="Run and log only; do not send Telegram")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        logger.error(f"Config file not found: {args.config}")
        return 2

    cfg = load_json_file(args.config)
    if not isinstance(cfg, dict):
        logger.error("Config file must be a JSON object")
        return 2

    setup_logging(level=str(cfg.get("log_level") or "INFO"), log_file=str(cfg.get("log_file") or DEFAULT_LOG_FILE))

    tasks = cfg.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        logger.error("Config file must contain a non-empty `tasks` array")
        return 2

    all_ok = True
    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            logger.error(f"tasks[{idx}] must be an object")
            all_ok = False
            continue

        task_type = str(task.get("type") or "").strip()
        task_name = str(task.get("name") or f"task-{idx+1}")
        logger.info(f"==> Running task: {task_type} name={task_name}")

        try:
            if task_type == "catalog_changes":
                ok = run_catalog_changes_task(task, dry_run=bool(args.dry_run))
            elif task_type == "stock_watch":
                ok = run_stock_watch_task(task, dry_run=bool(args.dry_run))
            else:
                raise ValueError(f"Unknown task type: {task_type}")
            all_ok = all_ok and ok
        except Exception as e:
            all_ok = False
            logger.error(f"Task failed: type={task_type} name={task_name} error={e}", exc_info=True)

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
