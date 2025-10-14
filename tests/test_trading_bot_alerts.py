from decimal import Decimal

import pytest

from trading_bot import TradingBot, TradingConfig


class DummyExchangeClient:
    def __init__(self):
        self.order_handler = None
        self.fetch_prices = (Decimal('100'), Decimal('101'))
        self.contract_id = "BTC-USD"
        self.tick_size = Decimal('0.1')

    def setup_order_update_handler(self, handler):
        self.order_handler = handler

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_contract_attributes(self):
        return self.contract_id, self.tick_size

    async def get_active_orders(self, contract_id):
        return []

    async def get_account_positions(self):
        return Decimal('0')

    async def fetch_bbo_prices(self, contract_id):
        return self.fetch_prices


@pytest.fixture
def dummy_exchange(monkeypatch):
    exchange = DummyExchangeClient()

    def fake_create_exchange(name, config):
        return exchange

    monkeypatch.setattr("trading_bot.ExchangeFactory.create_exchange", fake_create_exchange)
    return exchange


@pytest.fixture
def sample_config():
    return TradingConfig(
        ticker="BTC",
        contract_id="BTC-USD",
        quantity=Decimal('1'),
        take_profit=Decimal('0.5'),
        tick_size=Decimal('0.1'),
        direction="buy",
        max_orders=10,
        wait_time=10,
        exchange="dummy",
        grid_step=Decimal('1'),
        stop_price=Decimal('-1'),
        pause_price=Decimal('-1'),
        boost_mode=False,
    )


@pytest.fixture
def time_stub(monkeypatch):
    class TimeStub:
        def __init__(self, start=1_000_000):
            self.current = start

        def __call__(self):
            return self.current

        def advance(self, seconds):
            self.current += seconds

    stub = TimeStub()
    monkeypatch.setattr("trading_bot.time.time", stub)
    return stub


@pytest.mark.asyncio
async def test_order_utilization_alerts_trigger_once(dummy_exchange, sample_config, monkeypatch):
    bot = TradingBot(sample_config)

    messages = []
    async def fake_send_notification(message):
        messages.append(message)

    monkeypatch.setattr(bot, "send_notification", fake_send_notification)

    await bot._maybe_send_order_utilization_alert(5)
    assert len(messages) == 1
    assert "50%" in messages[0]
    assert messages[0].startswith("🚨 风险提醒")

    await bot._maybe_send_order_utilization_alert(5)
    assert len(messages) == 1

    await bot._maybe_send_order_utilization_alert(8)
    assert len(messages) == 2
    assert "80%" in messages[1]
    assert messages[1].startswith("🚨 风险提醒")

    await bot._maybe_send_order_utilization_alert(10)
    assert len(messages) == 3
    assert "100%" in messages[2]
    assert messages[2].startswith("🚨 风险提醒")


@pytest.mark.asyncio
async def test_loss_alerts_trigger_for_buy_position(dummy_exchange, sample_config, monkeypatch, time_stub):
    bot = TradingBot(sample_config)

    messages = []

    async def fake_send_notification(message):
        messages.append(message)

    monkeypatch.setattr(bot, "send_notification", fake_send_notification)

    bot._record_open_fill(Decimal('1'), Decimal('100'))

    dummy_exchange.fetch_prices = (Decimal('40'), Decimal('41'))
    bot.last_loss_check_time = 0

    await bot._check_position_loss()
    assert len(messages) == 1
    assert "60.0%" in messages[0]
    assert messages[0].startswith("🚨 亏损告警")

    dummy_exchange.fetch_prices = (Decimal('10'), Decimal('11'))
    time_stub.advance(bot.loss_check_interval + 1)

    await bot._check_position_loss()
    assert len(messages) == 2
    assert "90.0%" in messages[1]
    assert messages[1].startswith("🚨 亏损告警")

    dummy_exchange.fetch_prices = (Decimal('0'), Decimal('0.01'))
    time_stub.advance(bot.loss_check_interval + 1)

    await bot._check_position_loss()
    assert len(messages) == 3
    assert "100.0%" in messages[2]
    assert messages[2].startswith("🚨 亏损告警")


@pytest.mark.asyncio
async def test_run_exception_triggers_notification(dummy_exchange, sample_config, monkeypatch):
    bot = TradingBot(sample_config)

    messages = []

    async def fake_send_notification(message):
        messages.append(message)

    monkeypatch.setattr(bot, "send_notification", fake_send_notification)

    async def failing_connect():
        raise RuntimeError("connect boom")

    monkeypatch.setattr(dummy_exchange, "connect", failing_connect)

    with pytest.raises(RuntimeError):
        await bot.run()

    assert len(messages) == 1
    assert "程序异常" in messages[0]
    assert messages[0].startswith("🚨 程序异常")


@pytest.mark.asyncio
async def test_runtime_report_every_30_minutes(dummy_exchange, sample_config, monkeypatch, time_stub):
    bot = TradingBot(sample_config)

    messages = []

    async def fake_send_notification(message):
        messages.append(message)

    monkeypatch.setattr(bot, "send_notification", fake_send_notification)

    bot.active_close_orders = [
        {"id": "1", "price": Decimal("100"), "size": Decimal("2")},
        {"id": "2", "price": Decimal("101"), "size": Decimal("1")},
    ]
    bot.open_positions = [
        {"size": Decimal("1"), "price": Decimal("100"), "alerts": {Decimal('0.5'): False, Decimal('0.8'): False, Decimal('1.0'): False}}
    ]

    await bot._maybe_send_runtime_report(Decimal("3"), Decimal("3"))
    assert len(messages) == 1
    report = messages[0]
    assert report.startswith("[运行统计]")
    assert "活跃平仓订单数量" in report
    assert "累计交易次数" in report

    await bot._maybe_send_runtime_report(Decimal("3"), Decimal("3"))
    assert len(messages) == 1

    time_stub.advance(bot.report_interval + 1)
    await bot._maybe_send_runtime_report(Decimal("4"), Decimal("2"))
    assert len(messages) == 2
