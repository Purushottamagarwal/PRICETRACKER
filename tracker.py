#!/usr/bin/env python3
"""Best-effort Flipkart listing price tracker with Discord webhook alerts."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

APP_DIR = Path(os.getenv("TRACKER_DATA_DIR", "./data"))
DB_PATH = APP_DIR / "tracker.sqlite3"
PRODUCTS_FILE = APP_DIR / "products.txt"
CHECK_INTERVAL = max(900, int(os.getenv("CHECK_INTERVAL_SECONDS", "900")))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
REQUEST_TIMEOUT = 25
USER_AGENT = os.getenv(
    "TRACKER_USER_AGENT",
    "Mozilla/5.0 (compatible; PersonalPriceTracker/1.0; +https://example.invalid/bot-info)"
)
MAX_PRODUCTS = 100
BLOCK_BACKOFF_SECONDS = 6 * 60 * 60

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("flipkart-tracker")
APP_DIR.mkdir(parents=True, exist_ok=True)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_price TEXT, last_mrp TEXT, last_discount TEXT, last_title TEXT,
            last_status TEXT, last_checked TEXT,
            next_allowed_check INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL, checked_at TEXT NOT NULL, status TEXT NOT NULL,
            price TEXT, mrp TEXT, discount TEXT, title TEXT, detail TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        """)
    if not PRODUCTS_FILE.exists():
        PRODUCTS_FILE.write_text(
            "# Optional: one public Flipkart product URL per line.\n", encoding="utf-8"
        )

def valid_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
        return p.scheme == "https" and (p.hostname or "").lower() in {
            "www.flipkart.com", "flipkart.com"
        }
    except Exception:
        return False

def label_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    slug = path.split("/")[0] if path else "Flipkart product"
    slug = re.sub(r"-p-[A-Z0-9]+$", "", slug, flags=re.I)
    return (re.sub(r"[-_]+", " ", slug).strip() or "Flipkart product").title()[:90]

def sync_products_file() -> None:
    # Railway users can manage products using the PRODUCT_URLS environment variable.
    # Accept either one URL per line or semicolon-separated URLs.
    entries = []
    if PRODUCTS_FILE.exists():
        entries.extend(PRODUCTS_FILE.read_text(encoding="utf-8").splitlines())
    env_urls = os.getenv("PRODUCT_URLS", "")
    if env_urls:
        entries.extend(env_urls.replace(";", "\n").splitlines())

    urls = []
    for line in entries:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not valid_url(line):
            log.warning("Ignoring invalid/non-Flipkart URL: %s", line[:180])
            continue
        urls.append(line)
    urls = list(dict.fromkeys(urls))[:MAX_PRODUCTS]
    with db() as conn:
        for url in urls:
            conn.execute("INSERT OR IGNORE INTO products(url,label,created_at) VALUES(?,?,?)",
                         (url, label_from_url(url), now_iso()))
        if urls:
            marks = ",".join("?" for _ in urls)
            conn.execute(f"UPDATE products SET enabled=1 WHERE url IN ({marks})", urls)

def parse_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            d = Decimal(str(value))
            return d if d > 0 else None
        except InvalidOperation:
            return None
    s = str(value).replace(",", "")
    match = re.search(r"(?:₹|Rs\.?\s*)\s*([0-9]+(?:\.[0-9]{1,2})?)", s, re.I)
    if not match:
        match = re.search(r"^\s*([0-9]+(?:\.[0-9]{1,2})?)\s*$", s)
    if not match:
        return None
    try:
        d = Decimal(match.group(1))
        return d if d > 0 else None
    except InvalidOperation:
        return None

def money(d: Decimal | None) -> str:
    if d is None:
        return "Unknown"
    return "₹{:,.2f}".format(d).rstrip("0").rstrip(".")

def pct(price: Decimal | None, mrp: Decimal | None) -> Decimal | None:
    if not price or not mrp or mrp <= price:
        return None
    return ((mrp - price) * Decimal(100) / mrp).quantize(Decimal("0.1"))

def find_price_in_text(text: str) -> Decimal | None:
    patterns = [
        r'"sellingPrice"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
        r'"salePrice"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
        r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
        r'"finalPrice"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            try:
                val = Decimal(m.group(1))
                if Decimal("1") <= val <= Decimal("100000000"):
                    return val
            except InvalidOperation:
                pass
    return None

def extract_product(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    price = mrp = None
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(node.string or node.get_text())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                stack.extend(item["@graph"])
            offers = item.get("offers")
            if isinstance(offers, list):
                stack.extend(offers)
            elif isinstance(offers, dict):
                stack.append(offers)
            price = price or parse_price(item.get("price"))
            if isinstance(offers, dict):
                price = price or parse_price(offers.get("price") or offers.get("lowPrice"))
                mrp = mrp or parse_price(offers.get("highPrice"))
            if item.get("priceCurrency") == "INR":
                price = price or parse_price(item.get("price") or item.get("lowPrice"))
                mrp = mrp or parse_price(item.get("highPrice"))
    if price is None:
        price = find_price_in_text(html)
    if mrp is None:
        candidates = []
        for el in soup.select("._3I9_wc, ._2p6lqe, ._30jeq3, ._25b18c"):
            p = parse_price(el.get_text(" ", strip=True))
            if p:
                candidates.append(p)
        if price and candidates:
            higher = [p for p in candidates if p > price]
            if higher:
                mrp = max(higher)
    return {"title": title, "price": price, "mrp": mrp}

def send_discord(message: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL is not configured.")
        return False
    try:
        r = requests.post(DISCORD_WEBHOOK_URL,
                          json={"content": message[:1900], "allowed_mentions": {"parse": []}},
                          timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            log.error("Discord webhook HTTP %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Discord notification failed: %s", exc)
        return False

def log_check(product_id: int, status: str, price=None, mrp=None, discount=None,
              title=None, detail=None) -> None:
    with db() as conn:
        conn.execute("""INSERT INTO checks(product_id,checked_at,status,price,mrp,discount,title,detail)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (product_id, now_iso(), status,
                      str(price) if price is not None else None,
                      str(mrp) if mrp is not None else None,
                      str(discount) if discount is not None else None,
                      title, (detail or "")[:500]))

def get_products():
    with db() as conn:
        return conn.execute("SELECT * FROM products WHERE enabled=1 ORDER BY id").fetchall()

def update_product(product_id: int, **fields) -> None:
    allowed = {"last_price", "last_mrp", "last_discount", "last_title",
               "last_status", "last_checked", "next_allowed_check"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        with db() as conn:
            conn.execute("UPDATE products SET " + ", ".join(f"{k}=?" for k in fields) +
                         " WHERE id=?", (*fields.values(), product_id))

def check_one(product) -> None:
    now = int(time.time())
    if int(product["next_allowed_check"] or 0) > now:
        log.info("Skipping product during access backoff: %s", product["url"])
        return
    headers = {"User-Agent": USER_AGENT,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-IN,en;q=0.9"}
    try:
        response = requests.get(product["url"], headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("Request failed: %s", exc)
        log_check(product["id"], "network_error", detail=str(exc))
        update_product(product["id"], last_status="network_error", last_checked=now_iso())
        return
    if response.status_code in (401, 403, 429, 503):
        detail = f"HTTP {response.status_code}; access restricted/rate limited. Backing off six hours."
        log.warning("%s URL=%s", detail, product["url"])
        log_check(product["id"], "blocked", detail=detail)
        update_product(product["id"], last_status="blocked", last_checked=now_iso(),
                       next_allowed_check=now + BLOCK_BACKOFF_SECONDS)
        return
    if response.status_code >= 400:
        detail = f"HTTP {response.status_code}"
        log.warning("%s URL=%s", detail, product["url"])
        log_check(product["id"], "http_error", detail=detail)
        update_product(product["id"], last_status="http_error", last_checked=now_iso())
        return
    html = response.text
    low = html.lower()
    if any(marker in low for marker in ("verify you are human", "access denied",
                                         "unusual traffic", "robot check", "temporarily blocked")):
        detail = "Challenge/block page detected. No bypass attempted; backing off six hours."
        log.warning("%s URL=%s", detail, product["url"])
        log_check(product["id"], "blocked", detail=detail)
        update_product(product["id"], last_status="blocked", last_checked=now_iso(),
                       next_allowed_check=now + BLOCK_BACKOFF_SECONDS)
        return
    parsed = extract_product(html)
    price, mrp = parsed["price"], parsed["mrp"]
    title = parsed["title"] or product["label"]
    if price is None:
        detail = "No reliable price field found; existing baseline retained."
        log.info("%s URL=%s", detail, product["url"])
        log_check(product["id"], "price_unavailable", title=title, detail=detail)
        update_product(product["id"], last_status="price_unavailable",
                       last_title=title, last_checked=now_iso())
        return
    discount = pct(price, mrp)
    old_price = Decimal(product["last_price"]) if product["last_price"] else None
    old_discount = Decimal(product["last_discount"]) if product["last_discount"] else None
    changes = []
    if old_price is not None:
        if price < old_price:
            changes.append(f"📉 **Price dropped:** {money(old_price)} → **{money(price)}**")
        if discount is not None and old_discount is not None and discount != old_discount:
            direction = "increased" if discount > old_discount else "decreased"
            changes.append(f"🏷️ **Listed discount {direction}:** {old_discount}% → **{discount}%**")
        elif discount is not None and old_discount is None and mrp:
            changes.append(f"🏷️ **Listed discount:** {discount}% off MRP")
    if changes:
        send_discord("🛍️ **Flipkart price update**\n"
                     f"**{title[:180]}**\n" + "\n".join(changes) +
                     f"\nCurrent listed price: **{money(price)}**" +
                     (f"\nDisplayed MRP: {money(mrp)}" if mrp else "") +
                     f"\n<{product['url']}>\n\n"
                     "_Listing price only; bank, coupon, exchange, and account-specific offers may differ._")
    elif old_price is None:
        log.info("Baseline saved for %s at %s; no first-check alert.", title, money(price))
    log_check(product["id"], "ok", price, mrp, discount, title, "Price parsed")
    update_product(product["id"], last_price=str(price),
                   last_mrp=str(mrp) if mrp else None,
                   last_discount=str(discount) if discount is not None else None,
                   last_title=title, last_status="ok", last_checked=now_iso(),
                   next_allowed_check=0)

def main() -> None:
    init_db()
    sync_products_file()
    log.info("Tracker started. Interval=%ss; DB=%s", CHECK_INTERVAL, DB_PATH.resolve())
    if not DISCORD_WEBHOOK_URL:
        log.warning("Set DISCORD_WEBHOOK_URL to enable Discord alerts.")
    while True:
        try:
            sync_products_file()
            products = get_products()
            log.info("Checking %d tracked product(s).", len(products))
            for product in products:
                check_one(product)
                time.sleep(3)
        except Exception:
            log.exception("Unexpected tracker error")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
