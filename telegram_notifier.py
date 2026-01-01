#!/usr/bin/env python3
"""
Arc'teryx Outlet Telegram notifier.

Sends alert messages via the Telegram Bot API.

Required environment variables:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID (or TELEGRAM_CHAT_IDS for multiple chat IDs, comma-separated)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LEN = 4096


def _split_csv(value: str) -> List[str]:
    parts: List[str] = []
    for raw in (value or "").replace("\n", ",").split(","):
        item = raw.strip()
        if item:
            parts.append(item)
    return parts


def _chunk_text(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LEN) -> List[str]:
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at < 0:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at < 0:
            split_at = max_len

        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks


class TelegramNotifier:
    def __init__(
        self,
        *,
        token: Optional[str] = None,
        chat_ids: Optional[Sequence[str]] = None,
        timeout: int = 10,
    ) -> None:
        self.token = (token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        env_chat_ids = _split_csv(os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID") or "")
        self.chat_ids = list(chat_ids or env_chat_ids)
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_ids)

    def send_message(self, text: str, *, disable_web_page_preview: bool = True) -> bool:
        if not self.enabled:
            logger.warning("Telegram is not configured; skipping send (requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID/TELEGRAM_CHAT_IDS)")
            return False

        ok = True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for chat_id in self.chat_ids:
            for chunk in _chunk_text(text):
                payload = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": disable_web_page_preview,
                }
                try:
                    resp = requests.post(url, data=payload, timeout=self.timeout)
                    if resp.status_code != 200:
                        ok = False
                        logger.error(f"Telegram send failed: chat_id={chat_id} status={resp.status_code} body={resp.text[:500]}")
                except Exception as e:
                    ok = False
                    logger.error(f"Telegram send error: chat_id={chat_id} error={e}")
        return ok


def _now_local_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_change_notification(changes: Dict[str, Any]) -> bool:
    added = changes.get("added") or []
    removed = changes.get("removed") or []
    price_changes = changes.get("price_changes") or []

    parts: List[str] = []
    if added:
        parts.append(f"🆕 New items {len(added)}")
    if price_changes:
        parts.append(f"💰 Price changes {len(price_changes)}")
    if removed:
        parts.append(f"📦 Removed {len(removed)}")
    if not parts:
        logger.info("No changes; skipping Telegram notification")
        return False

    lines: List[str] = []
    lines.append("🏔️ Arc'teryx Outlet Update")
    lines.append(f"Time: {_now_local_str()}")
    lines.append("Summary: " + " | ".join(parts))

    if added:
        lines.append("")
        lines.append("🆕 New items:")
        for p in added[:10]:
            name = str(p.get("name") or "N/A")
            price = str(p.get("price") or "N/A")
            link = str(p.get("link") or "")
            lines.append(f"- {name} ({price})")
            if link:
                lines.append(f"  {link}")

    if price_changes:
        lines.append("")
        lines.append("💰 Price changes:")
        for c in price_changes[:10]:
            product = c.get("product") or {}
            name = str(product.get("name") or "N/A")
            old_price = str(c.get("old_price") or "N/A")
            new_price = str(c.get("new_price") or "N/A")
            link = str(product.get("link") or "")
            lines.append(f"- {name}: {old_price} → {new_price}")
            if link:
                lines.append(f"  {link}")

    if removed:
        lines.append("")
        lines.append("📦 Removed items:")
        for p in removed[:10]:
            name = str(p.get("name") or "N/A")
            lines.append(f"- {name}")

    notifier = TelegramNotifier()
    return notifier.send_message("\n".join(lines), disable_web_page_preview=True)


def send_stock_notification(
    items: List[dict],
    *,
    size_label: str = "",
    keywords: Optional[List[str]] = None,
    category_url: str = "",
    title: str = "🏔️ Arc'teryx Outlet Restock Alert",
) -> bool:
    if not items:
        logger.info("No stock events; skipping Telegram notification")
        return False

    keywords = keywords or []

    lines: List[str] = []
    lines.append(title)
    lines.append(f"Time: {_now_local_str()}")
    if size_label:
        lines.append(f"Size: {size_label}")
    if keywords:
        lines.append("Keywords: " + ", ".join(keywords))
    if category_url:
        lines.append(f"Category: {category_url}")
    lines.append("")

    for idx, item in enumerate(items, 1):
        name = str(item.get("name") or "N/A")
        link = str(item.get("link") or "")
        size = str(item.get("size") or size_label or "")
        price = str(item.get("price") or "N/A")
        note = str(item.get("note") or "").strip()
        colours = item.get("colours") or []
        colours_str = ", ".join([str(c) for c in colours if c]) if isinstance(colours, list) else str(colours)

        lines.append(f"{idx}. {name}")
        if size:
            lines.append(f"   size: {size}")
        if price:
            lines.append(f"   price: {price}")
        if note:
            lines.append(f"   note: {note}")
        if colours_str:
            lines.append(f"   colours: {colours_str}")
        if link:
            lines.append(f"   {link}")
        lines.append("")

    notifier = TelegramNotifier()
    return notifier.send_message("\n".join(lines).rstrip(), disable_web_page_preview=True)
