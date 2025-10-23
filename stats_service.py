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

    # Fee statistics
    total_fee = Decimal("0")
    today_fee = Decimal("0")
    last_hour_fee = Decimal("0")
    current_hour_fee = Decimal("0")

    # Fee rate statistics (for verification)
    fee_rate_samples = []
    today_fee_rate_samples = []

    # Liquidity role statistics (Maker/Taker)
    total_maker_count = 0
    total_taker_count = 0
    today_maker_count = 0
    today_taker_count = 0

    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                status = (row.get("Status") or row.get("status") or "").upper()
                if status not in {"FILLED", "PARTIALLY_FILLED"}:
                    continue

                quantity = parse_decimal(row.get("Quantity") or row.get("quantity"))
                price = parse_decimal(row.get("Price") or row.get("price"))

                # Parse fee/fee_rate - distinguish between empty and 0
                fee_str = row.get("Fee") or row.get("fee")
                fee = parse_decimal(fee_str) if fee_str else None

                fee_rate_str = row.get("FeeRate") or row.get("fee_rate")
                fee_rate = parse_decimal(fee_rate_str) if fee_rate_str else None

                timestamp = parse_timestamp(row.get("Timestamp"), tz)
                if quantity is None or price is None or timestamp is None:
                    continue

                quote_amount = quantity * price
                elapsed = now_ts - timestamp

                # Volume statistics (always count)
                total += quote_amount
                if timestamp.date() == now_ts.date():
                    today_total += quote_amount

                if 0 <= elapsed.total_seconds() <= 3600:
                    current_hour_total += quote_amount
                if 3600 < elapsed.total_seconds() <= 7200:
                    last_hour_total += quote_amount

                # Fee statistics (only count if Fee field exists)
                if fee is not None:
                    total_fee += fee
                    if timestamp.date() == now_ts.date():
                        today_fee += fee

                    if 0 <= elapsed.total_seconds() <= 3600:
                        current_hour_fee += fee
                    if 3600 < elapsed.total_seconds() <= 7200:
                        last_hour_fee += fee

                # Fee rate samples (only count if FeeRate field exists)
                if fee_rate is not None and fee_rate > 0:
                    fee_rate_samples.append(fee_rate)
                    if timestamp.date() == now_ts.date():
                        today_fee_rate_samples.append(fee_rate)

                # Liquidity role statistics (only count if LiquidityRole field exists)
                liquidity_role = (row.get("LiquidityRole") or row.get("liquidity_role") or "").strip()
                if liquidity_role:
                    # Use case-insensitive comparison to support both 'Maker'/'Taker' and 'MAKER'/'TAKER'
                    liquidity_role_upper = liquidity_role.upper()
                    if liquidity_role_upper == "MAKER":
                        total_maker_count += 1
                        if timestamp.date() == now_ts.date():
                            today_maker_count += 1
                    elif liquidity_role_upper == "TAKER":
                        total_taker_count += 1
                        if timestamp.date() == now_ts.date():
                            today_taker_count += 1

                first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
                last_ts = timestamp if last_ts is None else max(last_ts, timestamp)
    except FileNotFoundError:
        return None

    if first_ts is None or last_ts is None:
        return None

    elapsed_days = max(1, (last_ts.date() - first_ts.date()).days + 1)
    duration_hours = max(1, math.ceil((last_ts - first_ts).total_seconds() / 3600))

    # Calculate wear rate (磨损率)
    wear_rate = (total_fee / total * Decimal(100)) if total > 0 else Decimal("0")
    today_wear_rate = (today_fee / today_total * Decimal(100)) if today_total > 0 else Decimal("0")

    # Calculate average fee rate from samples (for verification)
    avg_fee_rate = sum(fee_rate_samples) / len(fee_rate_samples) if fee_rate_samples else Decimal("0")
    today_avg_fee_rate = sum(today_fee_rate_samples) / len(today_fee_rate_samples) if today_fee_rate_samples else Decimal("0")

    # Calculate Maker/Taker ratios
    total_trades = total_maker_count + total_taker_count
    today_trades = today_maker_count + today_taker_count

    total_maker_ratio = (Decimal(total_maker_count) / Decimal(total_trades) * Decimal(100)) if total_trades > 0 else Decimal("0")
    total_taker_ratio = (Decimal(total_taker_count) / Decimal(total_trades) * Decimal(100)) if total_trades > 0 else Decimal("0")
    today_maker_ratio = (Decimal(today_maker_count) / Decimal(today_trades) * Decimal(100)) if today_trades > 0 else Decimal("0")
    today_taker_ratio = (Decimal(today_taker_count) / Decimal(today_trades) * Decimal(100)) if today_trades > 0 else Decimal("0")

    return {
        "total": total,
        "today": today_total,
        "avg_daily": total / Decimal(elapsed_days),
        "avg_hourly": total / Decimal(duration_hours),
        "last_hour": last_hour_total,
        "current_hour": current_hour_total,
        # Fee statistics
        "total_fee": total_fee,
        "today_fee": today_fee,
        "last_hour_fee": last_hour_fee,
        "current_hour_fee": current_hour_fee,
        "wear_rate": wear_rate,
        "today_wear_rate": today_wear_rate,
        "avg_fee_per_trade": total_fee / Decimal(elapsed_days) if elapsed_days > 0 else Decimal("0"),
        # Fee rate (for verification)
        "avg_fee_rate": avg_fee_rate,
        "today_avg_fee_rate": today_avg_fee_rate,
        # Liquidity role statistics
        "total_maker_count": total_maker_count,
        "total_taker_count": total_taker_count,
        "today_maker_count": today_maker_count,
        "today_taker_count": today_taker_count,
        "total_maker_ratio": total_maker_ratio,
        "total_taker_ratio": total_taker_ratio,
        "today_maker_ratio": today_maker_ratio,
        "today_taker_ratio": today_taker_ratio,
    }


def format_stats_message(per_exchange: Dict[str, Dict[str, Dict[str, Decimal]]], include_zero_today: bool = False) -> str:
    if not per_exchange:
        return "[统计服务] 交易量播报\n- 暂无成交记录"

    lines = ["[统计服务] 交易量与成本播报"]
    for exchange in sorted(per_exchange.keys()):
        tickers = per_exchange[exchange]
        if not tickers:
            continue

        section_lines = []
        for ticker in sorted(tickers.keys()):
            stats = tickers[ticker]
            if not include_zero_today and stats["today"] == 0:
                continue

            section_lines.append(f"【{exchange}-{ticker}】")

            # Volume statistics
            section_lines.append(f"💰 交易量统计:")
            section_lines.append(f"- 总交易量: {format_decimal(stats['total'])}")
            section_lines.append(f"- 今日交易量: {format_decimal(stats['today'])}")
            section_lines.append(f"- 日均交易量: {format_decimal(stats['avg_daily'])}")
            section_lines.append(f"- 小时交易量: {format_decimal(stats['avg_hourly'])}")
            section_lines.append(f"- 上1小时交易量: {format_decimal(stats['last_hour'])}")
            section_lines.append(f"- 本小时交易量: {format_decimal(stats['current_hour'])}")

            # Fee and wear rate statistics
            section_lines.append(f"")
            section_lines.append(f"📊 成本分析:")
            section_lines.append(f"- 总手续费: ${format_decimal(stats['total_fee'])}")
            section_lines.append(f"- 今日手续费: ${format_decimal(stats['today_fee'])}")
            section_lines.append(f"- 总磨损率: {format_wear_rate(stats['wear_rate'])}")
            section_lines.append(f"- 今日磨损率: {format_wear_rate(stats['today_wear_rate'])}")

            # Fee rate verification (if available)
            if stats.get('avg_fee_rate', Decimal('0')) > 0:
                section_lines.append(f"- 平均费率: {format_wear_rate(stats['avg_fee_rate'])} (校验用)")
                if stats.get('today_avg_fee_rate', Decimal('0')) > 0:
                    section_lines.append(f"- 今日平均费率: {format_wear_rate(stats['today_avg_fee_rate'])} (校验用)")

            section_lines.append(f"- 上1小时费用: ${format_decimal(stats['last_hour_fee'])}")
            section_lines.append(f"- 本小时费用: ${format_decimal(stats['current_hour_fee'])}")

            # Liquidity role statistics (Maker/Taker)
            total_trades = stats.get('total_maker_count', 0) + stats.get('total_taker_count', 0)
            today_trades = stats.get('today_maker_count', 0) + stats.get('today_taker_count', 0)

            if total_trades > 0 or today_trades > 0:
                section_lines.append(f"")
                section_lines.append(f"🎭 流动性角色统计:")

                if total_trades > 0:
                    section_lines.append(f"- 总订单: Maker {stats.get('total_maker_count', 0)} ({stats.get('total_maker_ratio', Decimal('0')):.1f}%) / Taker {stats.get('total_taker_count', 0)} ({stats.get('total_taker_ratio', Decimal('0')):.1f}%)")

                if today_trades > 0:
                    section_lines.append(f"- 今日订单: Maker {stats.get('today_maker_count', 0)} ({stats.get('today_maker_ratio', Decimal('0')):.1f}%) / Taker {stats.get('today_taker_count', 0)} ({stats.get('today_taker_ratio', Decimal('0')):.1f}%)")

            section_lines.append("")

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


def format_wear_rate(value: Decimal) -> str:
    """
    Format wear rate (磨损率) as percentage and basis points.
    Example: 0.035% -> "0.035% (万3.5)"
    """
    basis_points = value * 100  # Convert 0.035% to 3.5
    return f"{value:.3f}% (万{basis_points:.1f})"


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
