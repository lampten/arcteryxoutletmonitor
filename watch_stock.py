#!/usr/bin/env python3
"""
Arc'teryx Outlet: keyword + size stock watcher.

Example:
  # Monitor men's footwear. Alert when size 8 becomes available for items matching keywords.
  python3 watch_stock.py \
    --category-url "https://outlet.arcteryx.com/ca/en/c/mens/footwear" \
    --size 8 \
    --keyword waterproof --keyword gtx --keyword gore-tex

Default behavior:
  - Alerts are sent when stock transitions from out-of-stock -> in-stock (requires Telegram env vars).
  - By default, if a product+size is seen for the first time and is already in stock, an alert is sent
    (disable via `notify_on_first_run: false` in the config, or `--no-notify-on-first-run`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests

from logging_utils import setup_logging

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

try:
    from telegram_notifier import TelegramNotifier, send_stock_notification

    TELEGRAM_ENABLED = True
except Exception:
    TELEGRAM_ENABLED = False

logger = logging.getLogger(__name__)

DEFAULT_CATEGORY_URL = "https://outlet.arcteryx.com/ca/en/c/mens/footwear"
DEFAULT_KEYWORDS = ["waterproof", "gtx", "gore-tex"]
DEFAULT_SIZE = "8"
DEFAULT_DATA_DIR = "data"
DEFAULT_STATE_FILENAME = "stock_watch_state.json"
DEFAULT_LOG_FILE = os.path.join("logs", "watch_stock.log")

IN_STOCK_STATUSES: Set[str] = {"InStock", "LowStock"}

DEFAULT_ERROR_NOTIFY_ENABLED = True
DEFAULT_ERROR_NOTIFY_REPEAT_INTERVAL_SECONDS = 3600


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def strip_html(html: str) -> str:
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html)


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.lower()
    text = (
        text.replace("‑", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
        .replace("／", "/")
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def try_parse_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def size_label_matches(option_label: str, target_size: str) -> bool:
    option_label = (option_label or "").strip()
    target_size = (target_size or "").strip()

    a = try_parse_float(option_label)
    b = try_parse_float(target_size)
    if a is not None and b is not None:
        return abs(a - b) < 1e-9

    return normalize_text(option_label) == normalize_text(target_size)


def extract_next_data(html: str) -> Optional[Dict[str, Any]]:
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def fetch_product_json(session: requests.Session, product_url: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
    resp = session.get(
        product_url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        timeout=timeout,
    )
    resp.raise_for_status()

    next_data = extract_next_data(resp.text)
    if not next_data:
        return None

    page_props = next_data.get("props", {}).get("pageProps", {})
    product_blob = page_props.get("product")
    if not product_blob:
        return None

    if isinstance(product_blob, dict):
        product = product_blob
    elif isinstance(product_blob, str):
        product = json.loads(product_blob)
    else:
        return None

    product["_product_url"] = product_url
    return product


@dataclass(frozen=True)
class CategoryTile:
    product_url: str
    name: str
    description: str


def tile_matches_keywords(tile: CategoryTile, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    haystack = normalize_text(f"{tile.name} {tile.description}")
    if not haystack:
        return False
    for keyword in keywords:
        needle = normalize_text(keyword)
        if needle and needle in haystack:
            return True
    return False


def collect_product_tiles_from_category(
    category_url: str,
    headless: bool = True,
    render_wait_seconds: int = 10,
    scroll_times: int = 3,
) -> List[CategoryTile]:
    if not SELENIUM_AVAILABLE:
        raise RuntimeError("selenium is not available; cannot scrape product URLs from a category page. Install deps via `pip install -r requirements.txt`.")

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(category_url)
        if render_wait_seconds > 0:
            import time

            time.sleep(render_wait_seconds)
        for _ in range(max(0, scroll_times)):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            import time

            time.sleep(2)

        elems = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/shop/"]')
        tiles: List[CategoryTile] = []
        seen: Set[str] = set()
        for e in elems:
            href = e.get_attribute("href")
            if not href:
                continue
            if "/shop/" not in href:
                continue
            href = href.split("?")[0]
            if href in seen:
                continue
            seen.add(href)

            name = ""
            description = ""

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

                if not description:
                    try:
                        desc_elems = node.find_elements(By.CSS_SELECTOR, "[data-component='body1'], [data-component='body2'], [class*='subtitle'], [class*='description']")
                        best = ""
                        for de in desc_elems:
                            t = (de.text or "").strip()
                            if len(t) > len(best):
                                best = t
                        if best:
                            description = best
                    except Exception:
                        pass

                if name and description:
                    break

            if not name:
                name = href.rstrip("/").split("/")[-1]

            tiles.append(CategoryTile(product_url=href, name=name, description=description))

        return tiles
    finally:
        driver.quit()


def product_matches_keywords(product: Dict[str, Any], keywords: Sequence[str]) -> bool:
    if not keywords:
        return True

    text_parts = [
        str(product.get("name") or ""),
        str(product.get("marketingName") or ""),
        str(product.get("shortDescription") or ""),
        strip_html(str(product.get("description") or "")),
    ]
    haystack = normalize_text(" ".join(text_parts))
    if not haystack:
        return False

    for keyword in keywords:
        needle = normalize_text(keyword)
        if needle and needle in haystack:
            return True
    return False


def extract_size_ids(product: Dict[str, Any], target_size_label: str) -> List[str]:
    size_options = product.get("sizeOptions") or {}
    options = size_options.get("options") or []
    size_ids: List[str] = []

    for opt in options:
        label = opt.get("label")
        value = opt.get("value")
        if not label or value is None:
            continue
        if size_label_matches(str(label), target_size_label):
            size_ids.append(str(value))
    return size_ids


def build_colour_map(product: Dict[str, Any]) -> Dict[str, str]:
    colour_options = product.get("colourOptions") or {}
    options = colour_options.get("options") or []
    colour_map: Dict[str, str] = {}
    for opt in options:
        value = opt.get("value")
        label = opt.get("label")
        if value is None or not label:
            continue
        colour_map[str(value)] = str(label)
    return colour_map


@dataclass(frozen=True)
class StockResult:
    product_url: str
    product_id: str
    name: str
    currency: str
    price: Optional[float]
    discount_price: Optional[float]
    size_label: str
    size_ids: Tuple[str, ...]
    in_stock: bool
    in_stock_colours: Tuple[str, ...]
    stock_status_by_colour: Dict[str, str]


def compute_stock_for_size(product: Dict[str, Any], target_size_label: str) -> StockResult:
    product_url = str(product.get("_product_url") or "")
    product_id = str(product.get("id") or product.get("slug") or product_url)
    name = str(product.get("name") or product_id)
    currency = str(product.get("currencyCode") or "")
    price = product.get("price")
    discount_price = product.get("discountPrice")

    size_ids = extract_size_ids(product, target_size_label)
    size_id_set = set(size_ids)

    colour_map = build_colour_map(product)
    in_stock_colours: Set[str] = set()
    stock_status_by_colour: Dict[str, str] = {}

    for variant in product.get("variants") or []:
        variant_size_id = variant.get("sizeId")
        if variant_size_id is None:
            continue
        if str(variant_size_id) not in size_id_set:
            continue

        colour_id = variant.get("colourId")
        colour_label = colour_map.get(str(colour_id), str(colour_id) if colour_id is not None else "Unknown")

        stock_status = str(variant.get("stockStatus") or "")
        stock_status_by_colour[colour_label] = stock_status
        if stock_status in IN_STOCK_STATUSES:
            in_stock_colours.add(colour_label)

    return StockResult(
        product_url=product_url,
        product_id=product_id,
        name=name,
        currency=currency,
        price=float(price) if isinstance(price, (int, float)) else None,
        discount_price=float(discount_price) if isinstance(discount_price, (int, float)) else None,
        size_label=target_size_label,
        size_ids=tuple(size_ids),
        in_stock=bool(in_stock_colours),
        in_stock_colours=tuple(sorted(in_stock_colours)),
        stock_status_by_colour=stock_status_by_colour,
    )


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"version": 1, "products": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "products": {}}
        if "products" not in data or not isinstance(data.get("products"), dict):
            data["products"] = {}
        data.setdefault("version", 1)
        return data
    except Exception:
        return {"version": 1, "products": {}}


def save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def get_previous_in_stock(state: Dict[str, Any], product_url: str, size_label: str) -> Optional[bool]:
    prod = state.get("products", {}).get(product_url)
    if not isinstance(prod, dict):
        return None
    sizes = prod.get("sizes", {})
    if not isinstance(sizes, dict):
        return None
    size_state = sizes.get(size_label)
    if not isinstance(size_state, dict):
        return None
    if "in_stock" not in size_state:
        return None
    return bool(size_state.get("in_stock"))


def update_state_with_result(state: Dict[str, Any], result: StockResult) -> None:
    products = state.setdefault("products", {})
    prod_state = products.setdefault(result.product_url, {})
    prod_state["name"] = result.name
    prod_state["product_id"] = result.product_id
    prod_state["last_checked"] = utc_now_iso()

    sizes = prod_state.setdefault("sizes", {})
    size_state = sizes.setdefault(result.size_label, {})
    previous = bool(size_state.get("in_stock")) if "in_stock" in size_state else None

    size_state["in_stock"] = result.in_stock
    size_state["in_stock_colours"] = list(result.in_stock_colours)
    size_state["stock_status_by_colour"] = result.stock_status_by_colour
    size_state["size_ids"] = list(result.size_ids)
    size_state["last_checked"] = utc_now_iso()
    size_state.setdefault("notify_count", 0)
    size_state.setdefault("last_notified_at", None)

    if previous is None:
        size_state.setdefault("last_change", utc_now_iso())
    elif bool(previous) != bool(result.in_stock):
        size_state["last_change"] = utc_now_iso()
        size_state["notify_count"] = 0


def get_size_state(state: Dict[str, Any], product_url: str, size_label: str) -> Optional[Dict[str, Any]]:
    prod = state.get("products", {}).get(product_url)
    if not isinstance(prod, dict):
        return None
    sizes = prod.get("sizes", {})
    if not isinstance(sizes, dict):
        return None
    size_state = sizes.get(size_label)
    if not isinstance(size_state, dict):
        return None
    return size_state


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def compute_error_signature(errors: Sequence[Tuple[str, str]], *, max_items: int = 20) -> str:
    h = hashlib.sha1()
    for context, message in list(errors)[: max(1, max_items)]:
        h.update(str(context).encode("utf-8", errors="ignore"))
        h.update(b"\n")
        h.update(str(message).encode("utf-8", errors="ignore"))
        h.update(b"\n")
    return h.hexdigest()


def should_send_error_notification(
    *,
    state: Dict[str, Any],
    signature: str,
    repeat_interval_seconds: int,
) -> bool:
    meta = state.get("error_notify")
    if not isinstance(meta, dict):
        meta = {}

    last_sig = str(meta.get("last_signature") or "")
    if last_sig != signature:
        return True

    if repeat_interval_seconds <= 0:
        return True

    last_dt = parse_iso_datetime(meta.get("last_notified_at"))
    if last_dt is None:
        return True

    now = datetime.now(timezone.utc)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= repeat_interval_seconds


def record_error_notification_sent(state: Dict[str, Any], *, signature: str) -> None:
    meta = state.setdefault("error_notify", {})
    if not isinstance(meta, dict):
        meta = {}
        state["error_notify"] = meta
    meta["last_notified_at"] = utc_now_iso()
    meta["last_signature"] = signature


def build_error_notification_text(
    *,
    errors: Sequence[Tuple[str, str]],
    log_file: str,
) -> str:
    lines: List[str] = []
    lines.append("⚠️ Arc'teryx Stock Watch Error")
    lines.append(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Errors: {len(errors)}")
    lines.append("")
    lines.append("Top errors:")

    for context, message in list(errors)[:10]:
        msg = " ".join(str(message).split())
        if len(msg) > 300:
            msg = msg[:297] + "..."
        lines.append(f"- {context}: {msg}")

    lines.append("")
    lines.append("Possible causes: network issues, rate limiting/blocking (HTTP 403/429), or site changes.")
    if log_file:
        lines.append(f"Log: {log_file}")
    return "\n".join(lines).rstrip()


def should_send_repeat_notification(
    *,
    notify_count: int,
    max_notifications_per_item: int,
    last_notified_at: Any,
    repeat_interval_seconds: int,
) -> bool:
    if max_notifications_per_item <= 1:
        return False
    if notify_count < 1 or notify_count >= max_notifications_per_item:
        return False

    if repeat_interval_seconds <= 0:
        return True

    last_dt = parse_iso_datetime(last_notified_at)
    if last_dt is None:
        return True

    now = datetime.now(timezone.utc)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= repeat_interval_seconds


def record_notification_sent(state: Dict[str, Any], result: StockResult) -> None:
    size_state = get_size_state(state, result.product_url, result.size_label)
    if not size_state:
        return
    try:
        current = int(size_state.get("notify_count") or 0)
    except Exception:
        current = 0
    size_state["notify_count"] = current + 1
    size_state["last_notified_at"] = utc_now_iso()


def build_notify_note(state: Dict[str, Any], result: StockResult, *, max_notifications_per_item: int) -> str:
    if max_notifications_per_item <= 1:
        return ""
    size_state = get_size_state(state, result.product_url, result.size_label) or {}
    try:
        notify_count = int(size_state.get("notify_count") or 0)
    except Exception:
        notify_count = 0
    return f"Alert {notify_count + 1}/{max_notifications_per_item}"


def format_price(currency: str, value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if currency:
        return f"{currency} ${value:.0f}" if value.is_integer() else f"{currency} ${value:.2f}"
    return f"${value:.0f}" if value.is_integer() else f"${value:.2f}"


def parse_keywords(args: argparse.Namespace) -> List[str]:
    keywords: List[str] = []
    if args.keywords:
        for part in args.keywords.split(","):
            part = part.strip()
            if part:
                keywords.append(part)
    if args.keyword:
        for k in args.keyword:
            k = (k or "").strip()
            if k:
                keywords.append(k)
    if not keywords and args.default_keywords:
        keywords = list(DEFAULT_KEYWORDS)
    return keywords


@dataclass
class WatchSpec:
    name: str
    category_url: str = ""
    product_urls: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    sizes: List[str] = field(default_factory=list)
    max_products: int = 0
    no_category_prefilter: bool = False


@dataclass
class StockWatchConfig:
    data_dir: str
    state_file: str
    log_file: str
    log_level: str
    show_browser: bool
    render_wait_seconds: int
    scroll_times: int
    notify_on_first_run: bool
    max_products: int
    no_category_prefilter: bool
    max_notifications_per_item: int
    repeat_interval_seconds: int
    notify_on_errors: bool
    error_repeat_interval_seconds: int
    watches: List[WatchSpec]


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    if isinstance(value, str):
        parts: List[str] = []
        for part in value.split(","):
            part = part.strip()
            if part:
                parts.append(part)
        return parts
    s = str(value).strip()
    return [s] if s else []


def load_config_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config file must be a JSON object")
    return data


def build_config_from_file(data: Dict[str, Any]) -> StockWatchConfig:
    data_dir = str(data.get("data_dir") or DEFAULT_DATA_DIR)
    state_file = str(data.get("state_file") or os.path.join(data_dir, DEFAULT_STATE_FILENAME))

    log_file = str(data.get("log_file") or DEFAULT_LOG_FILE)
    log_level = str(data.get("log_level") or "INFO")

    show_browser = bool(data.get("show_browser") or False)
    render_wait_seconds = int(data.get("render_wait_seconds") or 10)
    scroll_times = int(data.get("scroll_times") or 3)

    if "notify_on_first_run" in data:
        notify_on_first_run = bool(data.get("notify_on_first_run"))
    else:
        notify_on_first_run = True
    max_products = int(data.get("max_products") or 0)
    no_category_prefilter = bool(data.get("no_category_prefilter") or False)

    repeat = data.get("repeat") if isinstance(data.get("repeat"), dict) else {}
    max_notifications_per_item = int((repeat or {}).get("max_notifications_per_item") or data.get("max_notifications_per_item") or 1)
    repeat_interval_seconds = int((repeat or {}).get("repeat_interval_seconds") or data.get("repeat_interval_seconds") or 0)

    error_notify = data.get("error_notify") if isinstance(data.get("error_notify"), dict) else {}
    notify_on_errors = bool((error_notify or {}).get("enabled", DEFAULT_ERROR_NOTIFY_ENABLED))
    error_repeat_interval_seconds = int(
        (error_notify or {}).get("repeat_interval_seconds", DEFAULT_ERROR_NOTIFY_REPEAT_INTERVAL_SECONDS)
    )

    watches_raw = data.get("watches")
    if watches_raw is None:
        watches_raw = [
            {
                "name": data.get("name") or "default",
                "category_url": data.get("category_url") or "",
                "product_urls": data.get("product_urls") or [],
                "keywords": data.get("keywords") or [],
                "sizes": data.get("sizes") or [],
            }
        ]
    if not isinstance(watches_raw, list):
        raise ValueError("Config field `watches` must be an array")

    watches: List[WatchSpec] = []
    for idx, w in enumerate(watches_raw):
        if not isinstance(w, dict):
            raise ValueError(f"watches[{idx}] must be an object")

        name = str(w.get("name") or f"watch-{idx+1}")
        category_url = str(w.get("category_url") or "")
        product_urls = [u.split("?")[0] for u in _as_str_list(w.get("product_urls")) if u]
        keywords = list(dict.fromkeys(_as_str_list(w.get("keywords"))))
        sizes = list(dict.fromkeys(_as_str_list(w.get("sizes")))) or [DEFAULT_SIZE]

        watch_max_products = int(w.get("max_products") or max_products or 0)
        watch_no_prefilter = bool(w.get("no_category_prefilter") if "no_category_prefilter" in w else no_category_prefilter)

        if not category_url and not product_urls:
            raise ValueError(f"watches[{idx}] must provide `category_url` or `product_urls`")

        watches.append(
            WatchSpec(
                name=name,
                category_url=category_url,
                product_urls=list(dict.fromkeys([u for u in product_urls if u])),
                keywords=[k for k in keywords if k],
                sizes=[s for s in sizes if s],
                max_products=watch_max_products,
                no_category_prefilter=watch_no_prefilter,
            )
        )

    return StockWatchConfig(
        data_dir=data_dir,
        state_file=state_file,
        log_file=log_file,
        log_level=log_level,
        show_browser=show_browser,
        render_wait_seconds=render_wait_seconds,
        scroll_times=scroll_times,
        notify_on_first_run=notify_on_first_run,
        max_products=max_products,
        no_category_prefilter=no_category_prefilter,
        max_notifications_per_item=max_notifications_per_item,
        repeat_interval_seconds=repeat_interval_seconds,
        notify_on_errors=notify_on_errors,
        error_repeat_interval_seconds=error_repeat_interval_seconds,
        watches=watches,
    )


def build_config_from_args(args: argparse.Namespace) -> StockWatchConfig:
    data_dir = args.data_dir
    state_file = args.state_file or os.path.join(data_dir, DEFAULT_STATE_FILENAME)
    log_file = args.log_file or DEFAULT_LOG_FILE
    log_level = args.log_level or "INFO"

    watch = WatchSpec(
        name="cli",
        category_url=args.category_url,
        product_urls=list(dict.fromkeys([u.split("?")[0] for u in (args.product_url or []) if u])),
        keywords=parse_keywords(args),
        sizes=[args.size],
        max_products=args.max_products or 0,
        no_category_prefilter=bool(args.no_category_prefilter),
    )

    return StockWatchConfig(
        data_dir=data_dir,
        state_file=state_file,
        log_file=log_file,
        log_level=log_level,
        show_browser=bool(args.show_browser),
        render_wait_seconds=int(args.render_wait_seconds),
        scroll_times=int(args.scroll_times),
        notify_on_first_run=bool(args.notify_on_first_run),
        max_products=int(args.max_products or 0),
        no_category_prefilter=bool(args.no_category_prefilter),
        max_notifications_per_item=int(args.max_notifications_per_item),
        repeat_interval_seconds=int(args.repeat_interval_seconds),
        notify_on_errors=DEFAULT_ERROR_NOTIFY_ENABLED,
        error_repeat_interval_seconds=DEFAULT_ERROR_NOTIFY_REPEAT_INTERVAL_SECONDS,
        watches=[watch],
    )


def run_stock_watch(cfg: StockWatchConfig, *, dry_run: bool = False) -> int:
    os.makedirs(cfg.data_dir, exist_ok=True)
    state_existed = os.path.exists(cfg.state_file)
    state = load_state(cfg.state_file)

    max_notifications_per_item = max(1, int(cfg.max_notifications_per_item))
    repeat_interval_seconds = max(0, int(cfg.repeat_interval_seconds))
    error_repeat_interval_seconds = max(0, int(cfg.error_repeat_interval_seconds))
    logger.info(
        "Config: "
        f"data_dir={cfg.data_dir} state_file={cfg.state_file} "
        f"max_notifications_per_item={max_notifications_per_item} repeat_interval_seconds={repeat_interval_seconds} "
        f"notify_on_errors={bool(cfg.notify_on_errors)} error_repeat_interval_seconds={error_repeat_interval_seconds}"
    )

    session = requests.Session()

    matched_results: List[StockResult] = []
    restock_events_by_watch: Dict[str, List[StockResult]] = {}
    errors: List[Tuple[str, str]] = []
    emitted_event_keys: Set[Tuple[str, str]] = set()

    for watch in cfg.watches:
        if watch.product_urls:
            product_urls = list(dict.fromkeys([u.split("?")[0] for u in watch.product_urls if u]))
            category_tiles: Optional[List[CategoryTile]] = None
        else:
            logger.info(f"[{watch.name}] Scraping product URLs from category page: {watch.category_url}")
            try:
                category_tiles = collect_product_tiles_from_category(
                    watch.category_url,
                    headless=not cfg.show_browser,
                    render_wait_seconds=cfg.render_wait_seconds,
                    scroll_times=cfg.scroll_times,
                )
            except Exception as e:
                context = f"[{watch.name}] category_url={watch.category_url}"
                errors.append((context, f"Category scrape failed: {e}"))
                logger.warning(f"{context} Category scrape failed: {e}", exc_info=True)
                continue

            if not category_tiles:
                context = f"[{watch.name}] category_url={watch.category_url}"
                errors.append((context, "No products found on category page (possible blocking or page structure change)"))

            if watch.keywords and not watch.no_category_prefilter:
                filtered = [t for t in category_tiles if tile_matches_keywords(t, watch.keywords)]
                logger.info(f"[{watch.name}] Category items: {len(category_tiles)}, keyword matches: {len(filtered)}")
                category_tiles = filtered
            else:
                logger.info(f"[{watch.name}] Category items: {len(category_tiles)} (no keyword prefilter)")

            product_urls = [t.product_url for t in category_tiles]

        if watch.max_products and watch.max_products > 0:
            product_urls = product_urls[: watch.max_products]

        logger.info(f"[{watch.name}] Products to check: {len(product_urls)}")
        logger.info(f"[{watch.name}] Keywords: {watch.keywords or '(none)'}")
        logger.info(f"[{watch.name}] Target sizes: {watch.sizes or '(none)'}")

        for url in product_urls:
            try:
                product = fetch_product_json(session, url)
                if not product:
                    context = f"[{watch.name}] {url}"
                    errors.append((context, "Unable to parse product JSON (missing __NEXT_DATA__ or product payload)"))
                    logger.warning(f"[{watch.name}] Skipped (unable to parse product JSON): {url}")
                    continue

                if not product_matches_keywords(product, watch.keywords):
                    continue

                for size_label in watch.sizes:
                    result = compute_stock_for_size(product, size_label)
                    matched_results.append(result)

                    previous_in_stock = get_previous_in_stock(state, result.product_url, result.size_label)
                    update_state_with_result(state, result)

                    if not result.in_stock:
                        continue

                    size_state = get_size_state(state, result.product_url, result.size_label) or {}
                    try:
                        notify_count = int(size_state.get("notify_count") or 0)
                    except Exception:
                        notify_count = 0

                    should_notify = False
                    if previous_in_stock is None:
                        should_notify = bool(cfg.notify_on_first_run) and notify_count < max_notifications_per_item
                    elif previous_in_stock is False:
                        should_notify = notify_count < max_notifications_per_item
                    else:
                        should_notify = should_send_repeat_notification(
                            notify_count=notify_count,
                            max_notifications_per_item=max_notifications_per_item,
                            last_notified_at=size_state.get("last_notified_at"),
                            repeat_interval_seconds=repeat_interval_seconds,
                        )

                    if should_notify:
                        key = (result.product_url, result.size_label)
                        if key not in emitted_event_keys:
                            emitted_event_keys.add(key)
                            restock_events_by_watch.setdefault(watch.name, []).append(result)

            except requests.exceptions.HTTPError as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                status_part = f"HTTP {status_code}" if status_code else "HTTP error"
                context = f"[{watch.name}] {url}"
                errors.append((context, f"{status_part}: {e}"))
                logger.warning(f"[{watch.name}] Failed: {url} ({status_part}: {e})")
                continue
            except requests.exceptions.RequestException as e:
                context = f"[{watch.name}] {url}"
                errors.append((context, f"Request error: {e}"))
                logger.warning(f"[{watch.name}] Failed: {url} (Request error: {e})")
                continue
            except Exception as e:
                context = f"[{watch.name}] {url}"
                errors.append((context, str(e)))
                logger.warning(f"[{watch.name}] Failed: {url} ({e})")
                continue

    state["updated_at"] = utc_now_iso()
    save_state(cfg.state_file, state)

    logger.info(f"Keyword-matched products: {len(matched_results)}")
    for r in matched_results:
        price_str = format_price(r.currency, r.discount_price or r.price)
        stock_str = "IN STOCK" if r.in_stock else "out of stock"
        colour_str = f" colours={list(r.in_stock_colours)}" if r.in_stock_colours else ""
        logger.info(f"- {r.name} size={r.size_label} {stock_str}{colour_str} price={price_str or 'N/A'} url={r.product_url}")

    if errors:
        logger.info(f"Errors: {len(errors)}")
        for context, msg in errors[:10]:
            logger.info(f"- {context}: {msg}")

    if errors and bool(cfg.notify_on_errors) and not dry_run:
        if not TELEGRAM_ENABLED:
            logger.info("Telegram notifier is not available; skipping error notification")
        else:
            try:
                notifier = TelegramNotifier()
                if not notifier.enabled:
                    logger.warning("Telegram is not configured; skipping error notification")
                else:
                    signature = compute_error_signature(errors)
                    if should_send_error_notification(
                        state=state,
                        signature=signature,
                        repeat_interval_seconds=error_repeat_interval_seconds,
                    ):
                        text = build_error_notification_text(errors=errors, log_file=cfg.log_file)
                        ok = notifier.send_message(text, disable_web_page_preview=True)
                        if ok:
                            record_error_notification_sent(state, signature=signature)
                            save_state(cfg.state_file, state)
                        else:
                            logger.warning("Telegram error notification failed to send")
            except Exception:
                logger.exception("Error notification send failed")

    if not restock_events_by_watch:
        logger.info("No restock events (out-of-stock -> in-stock)")
        return 0

    if dry_run:
        logger.info("dry-run: skipping Telegram send")
        return 0

    if not TELEGRAM_ENABLED:
        logger.info("Telegram notifier is not available (telegram_notifier.py import failed); logging only")
        return 0

    if not state_existed and not cfg.notify_on_first_run:
        logger.info("First run: baseline established; skipping first-seen in-stock alerts (notify_on_first_run=false)")
        return 0

    all_ok = True
    state_changed_after_notify = False
    for watch in cfg.watches:
        events = restock_events_by_watch.get(watch.name) or []
        if not events:
            continue

        logger.info(f"[{watch.name}] Sending restock alerts: {len(events)}")
        size_label = watch.sizes[0] if len(watch.sizes) == 1 else ""
        ok = send_stock_notification(
            [
                {
                    "name": r.name,
                    "link": r.product_url,
                    "size": r.size_label,
                    "colours": list(r.in_stock_colours),
                    "price": format_price(r.currency, r.discount_price or r.price),
                    "note": build_notify_note(state, r, max_notifications_per_item=max_notifications_per_item),
                }
                for r in events
            ],
            size_label=size_label,
            keywords=watch.keywords,
            category_url=watch.category_url if not watch.product_urls else "",
            title=f"🏔️ Arc'teryx Outlet Restock Alert: {watch.name}",
        )
        if ok:
            for r in events:
                record_notification_sent(state, r)
            state_changed_after_notify = True
        all_ok = all_ok and ok

    if state_changed_after_notify:
        save_state(cfg.state_file, state)

    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Arc'teryx Outlet keyword + size stock watcher")
    parser.add_argument("--config", type=str, default="", help="Path to JSON config file (enables multi-watch configuration)")
    parser.add_argument("--category-url", type=str, default=DEFAULT_CATEGORY_URL, help="Category page URL (default: men's footwear)")
    parser.add_argument("--product-url", action="append", default=[], help="Explicit product URL (repeatable); skips category scraping when provided")

    parser.add_argument("--size", type=str, default=DEFAULT_SIZE, help="Target size label (e.g. 8 / 8.5)")
    parser.add_argument("--keyword", action="append", help="Keyword filter (repeatable)")
    parser.add_argument("--keywords", type=str, default="", help="Comma-separated keywords (e.g. waterproof,gtx,gore-tex)")
    parser.add_argument(
        "--no-default-keywords",
        dest="default_keywords",
        action="store_false",
        help="Do not use default keywords (waterproof/gtx/gore-tex)",
    )
    parser.set_defaults(default_keywords=True)

    parser.add_argument("--data-dir", type=str, default=DEFAULT_DATA_DIR, help="Data directory (default: data/)")
    parser.add_argument("--state-file", type=str, default="", help="State file path (default: data/stock_watch_state.json)")
    parser.add_argument("--log-file", type=str, default="", help=f"Log file path (default: {DEFAULT_LOG_FILE})")
    parser.add_argument("--log-level", type=str, default="INFO", help="Log level (DEBUG/INFO/WARNING/ERROR)")

    parser.add_argument("--show-browser", action="store_true", help="Show browser window (debug category scraping)")
    parser.add_argument("--render-wait-seconds", type=int, default=10, help="Seconds to wait for category page rendering")
    parser.add_argument("--scroll-times", type=int, default=3, help="How many times to scroll the category page")
    parser.add_argument("--max-products", type=int, default=0, help="Max number of products to check (0 = no limit)")
    parser.add_argument(
        "--no-category-prefilter",
        action="store_true",
        help="Disable category-page keyword prefilter; fetch all product pages then match keywords (more requests, more coverage)",
    )

    parser.add_argument(
        "--notify-on-first-run",
        dest="notify_on_first_run",
        action="store_true",
        default=True,
        help="Send alerts when a product+size is first seen in stock (default: enabled)",
    )
    parser.add_argument(
        "--no-notify-on-first-run",
        dest="notify_on_first_run",
        action="store_false",
        help="Disable alerts for first-seen in-stock items",
    )
    parser.add_argument("--max-notifications-per-item", type=int, default=1, help="Max alerts per product+size per restock cycle")
    parser.add_argument("--repeat-interval-seconds", type=int, default=0, help="Minimum seconds between repeated alerts (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except sending Telegram")

    args = parser.parse_args()

    cfg = build_config_from_file(load_config_file(args.config)) if args.config else build_config_from_args(args)
    setup_logging(level=cfg.log_level, log_file=cfg.log_file)
    return run_stock_watch(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
