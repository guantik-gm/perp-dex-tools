from datetime import datetime
from decimal import Decimal

import pytz

from stats_service import aggregate_quote_volume, format_stats_message


def write_orders(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("Timestamp,OrderID,Side,Quantity,Price,Status\n")
        for row in rows:
            handle.write(
                f"{row['timestamp']},{row['order_id']},{row['side']},{row['quantity']},{row['price']},{row['status']}\n"
            )


def test_aggregate_quote_volume_empty(tmp_path):
    tz = pytz.timezone("Asia/Shanghai")
    stats = aggregate_quote_volume(str(tmp_path), tz, now=tz.localize(datetime(2024, 1, 1, 12, 0, 0)))
    assert stats == {}


def test_aggregate_quote_volume_with_data(tmp_path):
    tz = pytz.timezone("Asia/Shanghai")
    rows = [
        {
            "timestamp": "2024-01-01 10:00:00",
            "order_id": "1",
            "side": "buy",
            "quantity": "2",
            "price": "100",
            "status": "FILLED",
        },
        {
            "timestamp": "2024-01-02 12:00:00",
            "order_id": "2",
            "side": "sell",
            "quantity": "1",
            "price": "50",
            "status": "FILLED",
        },
        {
            "timestamp": "2024-01-02 15:00:00",
            "order_id": "3",
            "side": "buy",
            "quantity": "1",
            "price": "40",
            "status": "CANCELED",
        },
    ]
    write_orders(tmp_path / "aster_BNB_orders.csv", rows)

    now = tz.localize(datetime(2024, 1, 2, 18, 0, 0))
    stats = aggregate_quote_volume(str(tmp_path), tz, now=now)

    assert "ASTER" in stats
    assert "BNB" in stats["ASTER"]
    entry = stats["ASTER"]["BNB"]
    assert entry["total"] == Decimal("250")
    assert entry["today"] == Decimal("50")
    assert entry["avg_daily"].quantize(Decimal("0.01")) == Decimal("125.00")
    assert entry["avg_hourly"].quantize(Decimal("0.01")) == Decimal("9.62")


def test_format_stats_message_layout():
    data = {
        "ASTER": {
            "BNB": {
                "total": Decimal("153094.11"),
                "today": Decimal("5478.51"),
                "avg_daily": Decimal("51031.37"),
                "avg_hourly": Decimal("4137.68"),
            }
        },
        "BACKPACK": {
            "BTC": {
                "total": Decimal("2000"),
                "today": Decimal("100"),
                "avg_daily": Decimal("1000"),
                "avg_hourly": Decimal("500"),
            }
        }
    }

    message = format_stats_message(data)
    expected = (
        "[统计服务] 交易量播报\n"
        "===ASTER===\n"
        "【BNB】\n"
        "- 总交易量: 153,094.11\n"
        "- 今日交易量: 5,478.51\n"
        "- 日均交易量: 51,031.37\n"
        "- 小时交易量: 4,137.68\n"
        "===BACKPACK===\n"
        "【BTC】\n"
        "- 总交易量: 2,000.00\n"
        "- 今日交易量: 100.00\n"
        "- 日均交易量: 1,000.00\n"
        "- 小时交易量: 500.00"
    )
    assert message == expected
