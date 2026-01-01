# Arc'teryx Outlet Stock Watch (Shoes / Sizes)

This repo monitors Arc'teryx Outlet products and sends a Telegram alert when a target size goes from **out of stock → in stock**. By default, it also alerts when a product+size is first seen and is already in stock (e.g. first run or newly listed products). The primary entrypoint is `watch_stock.py`.

## What it does

- Scrapes a category page (e.g. men's footwear) to collect product URLs (via Selenium).
- Fetches each product page and parses the embedded `__NEXT_DATA__` JSON (via `requests`).
- Checks variant stock status for your target size(s).
- Sends Telegram alerts on restock events (and optionally repeats).

## Quick start (recommended)

```bash
./setup.sh
cp stock_watch_config.example.json stock_watch_config.json

# Telegram (optional but recommended)
export TELEGRAM_BOT_TOKEN="123456789:replace-me"
export TELEGRAM_CHAT_ID="123456789"   # or TELEGRAM_CHAT_IDS="id1,id2"

./run.sh
```

`run.sh` behavior:
- If you pass `--config ...`, it forwards args to `watch_stock.py`.
- Else, if `stock_watch_config.json` exists, it runs `watch_stock.py --config stock_watch_config.json`.
- Else, it runs `watch_stock.py` with CLI defaults.

## Requirements

- Python 3.7+
- If you use `category_url`: Google Chrome / Chromium installed (Selenium will manage the driver automatically on recent Selenium versions).

On a Linux server (Ubuntu example), you typically need Chrome/Chromium and common libraries. If Chrome fails to start, install the missing deps indicated in the error log.

## Telegram configuration

Environment variables:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID` (single) or `TELEGRAM_CHAT_IDS` (comma-separated)

The notifier is implemented in `telegram_notifier.py`.

## JSON config guide (`stock_watch_config.json`)

Copy the example and edit it:

```bash
cp stock_watch_config.example.json stock_watch_config.json
```

### Top-level fields

- `data_dir`: where state is stored (default `data`)
- `state_file`: persisted baseline + history (default `data/stock_watch_state.json`)
- `log_file`: log path (default `logs/watch_stock.log`)
- `log_level`: `DEBUG|INFO|WARNING|ERROR`
- `show_browser`: set `true` to run a visible browser (debugging category scraping)
- `render_wait_seconds`: seconds to wait after opening category page
- `scroll_times`: how many times to scroll the category page (lazy-load)
- `notify_on_first_run`: `true` by default (alert when a product+size is first seen in stock; set `false` to only alert on out-of-stock → in-stock transitions after baseline)
- `max_products`: global cap (0 = no limit)
- `no_category_prefilter`: if `false`, keywords are pre-filtered using category tile text to reduce product-page requests
- `repeat.max_notifications_per_item`: max alerts per product+size per “restock cycle” (min 1)
- `repeat.repeat_interval_seconds`: minimum seconds between repeated alerts (0 = no limit)
- `error_notify.enabled`: send a Telegram alert when scraping fails (network errors, blocking, parsing failures)
- `error_notify.repeat_interval_seconds`: throttle repeated error alerts across runs (0 = no limit)

### `watches[]`

You can define multiple independent watches. Each watch supports:

- `name`: label for logs/alerts
- `category_url`: category page URL to scrape (requires Selenium + Chrome)
- `product_urls`: optional explicit product URLs; if provided, category scraping is skipped
- `keywords`: list of keywords to match (case-insensitive)
- `sizes`: list of size labels to monitor (e.g. `["8","8.5"]` or `["M","L"]`)
- `max_products`: per-watch cap (0 = no limit)
- `no_category_prefilter`: override `no_category_prefilter` for this watch

### Example config

```json
{
  "version": 1,
  "data_dir": "data",
  "state_file": "data/stock_watch_state.json",
  "log_file": "logs/watch_stock.log",
  "log_level": "INFO",
  "show_browser": false,
  "render_wait_seconds": 10,
  "scroll_times": 3,
  "notify_on_first_run": true,
  "repeat": {
    "max_notifications_per_item": 3,
    "repeat_interval_seconds": 1800
  },
  "error_notify": {
    "enabled": true,
    "repeat_interval_seconds": 3600
  },
  "watches": [
    {
      "name": "shoes-gtx",
      "category_url": "https://outlet.arcteryx.com/ca/en/c/mens/footwear",
      "keywords": ["waterproof", "gtx", "gore-tex"],
      "sizes": ["8", "8.5"],
      "max_products": 0,
      "no_category_prefilter": false
    }
  ]
}
```

## CLI usage (no config file)

```bash
python3 watch_stock.py \
  --category-url "https://outlet.arcteryx.com/ca/en/c/mens/footwear" \
  --size 8 \
  --keyword waterproof --keyword gtx --keyword gore-tex
```

Useful flags:
- `--dry-run`: do everything except sending Telegram
- `--no-notify-on-first-run`: disable alerts for first-seen in-stock items
- `--no-category-prefilter`: fetch all product pages and match keywords there (more requests, more coverage)
- `--show-browser`: debug category scraping with a visible browser

## Output files

- `data/stock_watch_state.json`: baseline + per-product state
- `logs/watch_stock.log`: logs

## Scheduling on Linux (cron)

```bash
crontab -e
```

Run every 30 minutes:

```bash
*/30 * * * * cd $HOME/arcteryx-monitor && ./run.sh >> logs/watch_stock.log 2>&1
```

## Troubleshooting

- Selenium/Chrome fails to start: install Chrome/Chromium and the missing system libraries; check `logs/watch_stock.log`.
- No products found on category page: increase `render_wait_seconds`, increase `scroll_times`, or run with `--show-browser` to inspect the page.
- Too many requests: keep a reasonable schedule (e.g. 30–60 minutes) and avoid aggressive scraping.

## Optional / legacy

- `monitor_unified.py` can also run catalog-change monitoring, but the repo is optimized for `watch_stock.py`.
- Other scripts in the repo are legacy/deprecated and not maintained.
