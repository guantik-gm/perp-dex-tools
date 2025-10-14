from datetime import datetime
from decimal import Decimal

import pytz

from stats_service import aggregate_quote_volume


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
    assert stats["total"] == Decimal("0")
    assert stats["today"] == Decimal("0")
    assert stats["avg_daily"] == Decimal("0")
    assert stats["avg_hourly"] == Decimal("0")


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
    write_orders(tmp_path / "dummy_orders.csv", rows)

    now = tz.localize(datetime(2024, 1, 2, 18, 0, 0))
    stats = aggregate_quote_volume(str(tmp_path), tz, now=now)

    assert stats["total"] == Decimal("250")
    assert stats["today"] == Decimal("50")
    assert stats["avg_daily"].quantize(Decimal("0.01")) == Decimal("125.00")
    assert stats["avg_hourly"].quantize(Decimal("0.01")) == Decimal("9.62")
