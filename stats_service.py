import argparse
import csv
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
    parser.add_argument("--include-zero-today", action="store_true", help="Include tokens whose today volume is zero")
    return parser.parse_args()


def list_order_files(logs_dir: str) -> Iterable[str]:
    for entry in sorted(os.scandir(logs_dir), key=lambda e: e.name):
        if entry.is_file() and entry.name.endswith("_orders.csv"):
            yield entry.path


def iter_order_rows(logs_dir: str) -> Iterable[Tuple[str, Dict[str, str]]]:
    for path in list_order_files(logs_dir):
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


def aggregate_quote_volume(logs_dir: str, tz: pytz.BaseTzInfo, now: Optional[datetime] = None) -> Dict[str, Dict[str, Dict[str, Decimal]]]:
    now_ts = now or datetime.now(tz)
    aggregate: Dict[str, Dict[str, Dict[str, Decimal]]] = {}

    for path in list_order_files(logs_dir):
        stats = _aggregate_single_file(path, tz, now_ts)
        if stats is None:
            continue
        exchange, ticker = _parse_filename(os.path.basename(path))
        exchange_map = aggregate.setdefault(exchange, {})
        exchange_map[ticker] = stats

    return aggregate


def _aggregate_single_file(path: str, tz: pytz.BaseTzInfo, now_ts: datetime) -> Optional[Dict[str, Decimal]]:
    total = Decimal("0")
    today_total = Decimal("0")
    last_hour_total = Decimal("0")
    current_hour_total = Decimal("0")
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None

    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                status = (row.get("Status") or row.get("status") or "").upper()
                if status not in {"FILLED", "PARTIALLY_FILLED"}:
                    continue

                quantity = parse_decimal(row.get("Quantity") or row.get("quantity"))
                price = parse_decimal(row.get("Price") or row.get("price"))
                timestamp = parse_timestamp(row.get("Timestamp"), tz)
                if quantity is None or price is None or timestamp is None:
                    continue

                quote_amount = quantity * price
                elapsed = now_ts - timestamp
                total += quote_amount
                if timestamp.date() == now_ts.date():
                    today_total += quote_amount

                if 0 <= elapsed.total_seconds() <= 3600:
                    current_hour_total += quote_amount
                if 3600 < elapsed.total_seconds() <= 7200:
                    last_hour_total += quote_amount

                first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
                last_ts = timestamp if last_ts is None else max(last_ts, timestamp)
    except FileNotFoundError:
        return None

    if first_ts is None or last_ts is None:
        return None

    elapsed_days = max(1, (last_ts.date() - first_ts.date()).days + 1)
    duration_hours = max(1, math.ceil((last_ts - first_ts).total_seconds() / 3600))

    return {
        "total": total,
        "today": today_total,
        "avg_daily": total / Decimal(elapsed_days),
        "avg_hourly": total / Decimal(duration_hours),
        "last_hour": last_hour_total,
        "current_hour": current_hour_total,
    }


def format_stats_message(per_exchange: Dict[str, Dict[str, Dict[str, Decimal]]], include_zero_today: bool = False) -> str:
    if not per_exchange:
        return "[统计服务] 交易量播报\n- 暂无成交记录"

    lines = ["[统计服务] 交易量播报"]
    for exchange in sorted(per_exchange.keys()):
        tickers = per_exchange[exchange]
        if not tickers:
            continue

        section_lines = [f"==={exchange}==="]
        for ticker in sorted(tickers.keys()):
            stats = tickers[ticker]
            if not include_zero_today and stats["today"] == 0:
                continue

            section_lines.append(f"【{ticker}】")
            section_lines.append(f"- 总交易量: {format_decimal(stats['total'])}")
            section_lines.append(f"- 今日交易量: {format_decimal(stats['today'])}")
            section_lines.append(f"- 日均交易量: {format_decimal(stats['avg_daily'])}")
            section_lines.append(f"- 小时交易量: {format_decimal(stats['avg_hourly'])}")
            section_lines.append(f"- 上1小时交易量: {format_decimal(stats['last_hour'])}")
            section_lines.append(f"- 本小时交易量: {format_decimal(stats['current_hour'])}")

        if len(section_lines) > 1:
            lines.extend(section_lines)
    return "\n".join(lines)


def _parse_filename(filename: str) -> Tuple[str, str]:
    base = filename[:-len("_orders.csv")] if filename.endswith("_orders.csv") else filename
    parts = base.split("_")
    if len(parts) >= 2:
        exchange = parts[0].upper()
        ticker = parts[1].upper()
    else:
        exchange = base.upper()
        ticker = "-"
    return exchange, ticker


def format_decimal(value: Decimal) -> str:
    return f"{value:,.2f}"


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
            bot.send_text(format_stats_message(stats, include_zero_today=args.include_zero_today))
            if args.once:
                break
            time.sleep(interval)


if __name__ == "__main__":
    main()
