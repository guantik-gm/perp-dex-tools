import argparse
import csv
import glob
import math
import os
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Optional, Tuple

import pytz
from dotenv import load_dotenv

from helpers.telegram_bot import TelegramBot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate trade volumes from CSV logs and send Telegram reports")
    parser.add_argument("--logs-dir", default="logs", help="Directory containing *_orders.csv files")
    parser.add_argument("--interval", type=int, default=1800, help="Interval in seconds between reports")
    parser.add_argument("--env-file", default=".env", help="Environment file providing TELEGRAM credentials")
    parser.add_argument("--timezone", default=None, help="Timezone name, defaults to TIMEZONE env or Asia/Shanghai")
    parser.add_argument("--once", action="store_true", help="Send a single report and exit")
    return parser.parse_args()


def iter_order_rows(logs_dir: str) -> Iterable[Tuple[str, Dict[str, str]]]:
    pattern = os.path.join(logs_dir, "*_orders.csv")
    for path in glob.glob(pattern):
        try:
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    yield path, row
        except FileNotFoundError:
            continue


def parse_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_timestamp(value: Optional[str], tz: pytz.BaseTzInfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return tz.localize(naive)
    except ValueError:
        return None


def aggregate_quote_volume(logs_dir: str, tz: pytz.BaseTzInfo, now: Optional[datetime] = None) -> Dict[str, Decimal]:
    total = Decimal("0")
    today_total = Decimal("0")
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    now_ts = now or datetime.now(tz)

    for _, row in iter_order_rows(logs_dir):
        status = (row.get("Status") or row.get("status") or "").upper()
        if status not in {"FILLED", "PARTIALLY_FILLED"}:
            continue

        quantity = parse_decimal(row.get("Quantity") or row.get("quantity"))
        price = parse_decimal(row.get("Price") or row.get("price"))
        timestamp = parse_timestamp(row.get("Timestamp"), tz)
        if quantity is None or price is None or timestamp is None:
            continue

        quote_amount = quantity * price
        total += quote_amount
        if timestamp.date() == now_ts.date():
            today_total += quote_amount

        first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
        last_ts = timestamp if last_ts is None else max(last_ts, timestamp)

    if first_ts is None or last_ts is None:
        zero = Decimal("0")
        return {
            "total": zero,
            "today": zero,
            "avg_daily": zero,
            "avg_hourly": zero,
        }

    elapsed_days = max(1, (last_ts.date() - first_ts.date()).days + 1)
    duration_hours = max(1, math.ceil((last_ts - first_ts).total_seconds() / 3600))

    return {
        "total": total,
        "today": today_total,
        "avg_daily": total / Decimal(elapsed_days),
        "avg_hourly": total / Decimal(duration_hours),
    }


def format_stats_message(stats: Dict[str, Decimal]) -> str:
    return "\n".join([
        "[统计服务] 交易量播报",
        f"- 累计成交量(计价): {stats['total']:.2f}",
        f"- 今日成交量: {stats['today']:.2f}",
        f"- 平均每日成交量: {stats['avg_daily']:.2f}",
        f"- 平均每小时成交量: {stats['avg_hourly']:.2f}",
    ])


def resolve_timezone(name: Optional[str]) -> pytz.BaseTzInfo:
    zone = name or os.getenv("TIMEZONE") or "Asia/Shanghai"
    return pytz.timezone(zone)


def main() -> None:
    args = parse_args()
    load_dotenv(args.env_file)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    tz = resolve_timezone(args.timezone)

    interval = max(1, args.interval)

    with TelegramBot(token, chat_id) as bot:
        while True:
            stats = aggregate_quote_volume(args.logs_dir, tz)
            bot.send_text(format_stats_message(stats))
            if args.once:
                break
            time.sleep(interval)


if __name__ == "__main__":
    main()
