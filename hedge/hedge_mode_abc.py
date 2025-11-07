from dataclasses import dataclass
import pytz
from datetime import datetime
from abc import ABC, abstractmethod
import asyncio
import signal
import logging
import os
import sys
import time
import argparse
import traceback
import csv
from decimal import Decimal
from typing import List, Tuple

import sys
import os

from hedge.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult
from hedge.lighter_proxy import LighterProxy
from hedge.hedge_monitor import HedgeMonitor
from helpers.logger import log_trade_to_csv
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class HedgeOrderResult:
    """订单执行结果状态定义"""
    SUCCESS = "success"          # 订单成功执行
    FAILED = "failed"           # 订单执行失败
    RETRY_STRATEGY = "retry_strategy"  # 需要重新进行策略判断


class Config:
    """Simple config class to wrap dictionary for primary client."""

    def __init__(self, config_dict):
        for key, value in config_dict.items():
            setattr(self, key, value)

@dataclass
class CurrentOrderHandler:
    current_primary_side: str = None
    current_primary_price: Decimal = None
    current_primary_quantity: Decimal = None
    current_lighter_side: str = None
    current_lighter_price: Decimal = None
    current_lighter_quantity: Decimal = None

class HedgeBotAbc(ABC):
    """Trading bot that places post-only orders on primary and hedges with market orders on Lighter."""

    def __init__(self, ticker: str, order_quantity: Decimal, fill_timeout: int = 5, iterations: int = 20):
        self.ticker = ticker
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.iterations = iterations
        self.primary_position = Decimal('0')
        self.lighter_position = Decimal('0')
        self.current_order = {}

        # 策略数据共享字典
        self.triggered_strategies_data = {}

        # Primary state
        self.primary_client = None
        self.primary_contract_id = None
        self.primary_tick_size = None
        self.primary_order_status = None

        # Initialize CSV file with headers if it doesn't exist
        self._initialize_log_file()
        self._initialize_logger()
        self._initialize_primary_client()

        self.lighter = LighterProxy(self.ticker, self.logger, position_callback=self._update_lighter_position)

        # Initialize HedgeMonitor
        self.monitor = HedgeMonitor(
            ticker=self.ticker,
            order_quantity=self.order_quantity,
            logger=self.logger,
            primary_exchange_name=self.primary_exchange_name(),
            hedge_bot_order_handler=self.get_current_order_handler,
            hedge_bot_fee_rate_handler=self.primary_fee_rate
        )

        self.waiting_for_lighter_fill = False
        # State management
        self.stop_flag = False
        self.order_counter = 0

        # Order execution tracking
        self.order_execution_complete = False
        
        # 开平仓策略接口 - 支持策略数组
        self.hedge_strategies = []
        self.strategy_check_time = 1

        self.current_primary_side = None
        self.current_primary_price = None
        self.current_primary_quantity = None
        self.current_lighter_side = None        
        self.current_lighter_price = None
        self.current_lighter_quantity = None

    def get_primary_position(self):
        return self.primary_position
    
    def get_lighter_position(self):
        return self.lighter_position
    
    def get_current_order_handler(self):
        return CurrentOrderHandler(
            current_primary_side=self.current_primary_side,
            current_primary_price=self.current_primary_price,
            current_primary_quantity=self.current_primary_quantity,
            current_lighter_side=self.current_lighter_side,
            current_lighter_price=self.current_lighter_price,
            current_lighter_quantity=self.current_lighter_quantity
        )

    def add_strategy(self, strategy):
        """添加策略到策略数组"""
        self.hedge_strategies.append(strategy)
        self.logger.info(f"✅ 添加策略: {strategy.name} (开仓优先级: {strategy.open_priority}, 平仓优先级: {strategy.close_priority})")
    
    def get_sorted_strategies(self, operation_type='open'):
        """获取按优先级排序的策略列表"""
        if operation_type == 'open':
            return sorted(self.hedge_strategies, key=lambda s: s.open_priority, reverse=True)
        else:  # close
            return sorted(self.hedge_strategies, key=lambda s: s.close_priority, reverse=True)
    
    async def wait_open(self):
        """等待开仓条件满足 - 在hedge_mode_abc中实现，检查所有策略"""
        self.logger.info("🔍 开始等待开仓条件满足...")
        
        while not self.stop_flag:
            try:
                # 按优先级顺序检查所有策略
                strategies = self.get_sorted_strategies('open')
                passed_strategies = []
                
                self.logger.info("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~") 
                for strategy in strategies:
                    try:
                        self.logger.info(f"[{strategy.name}] 开始检测开仓策略")
                        await strategy.can_open(self)
                        self.logger.info(f"[{strategy.name}] {strategy.reason}")
                        if strategy.result == HedgeStrategyResult.TRIGGER:
                            self.logger.info(f"[{strategy.name}] ✅ 开仓策略触发")
                            passed_strategies.append(strategy)  # 返回被触发的策略对象
                            return passed_strategies
                        elif strategy.result == HedgeStrategyResult.REJECT:
                            self.logger.info(f"[{strategy.name}] 当前策略开仓条件不满足，强制退出并等待下轮循环")
                            passed_strategies.clear()
                            break
                        elif strategy.result == HedgeStrategyResult.PASS:
                            self.logger.info(f"[{strategy.name}] ✅ 开仓策略通过")
                            passed_strategies.append(strategy)
                    except Exception as e:
                        self.logger.error(f"[{strategy.name}] ❌ 策略开仓检查异常: {e}")
                        continue
                # 要么都没有触发为0，只要不为0就肯定都pass
                if len(passed_strategies) == 0:
                    self.logger.info(f"🔄 无开仓策略触发，等待 {self.strategy_check_time} 秒后重新检查...")
                    await asyncio.sleep(self.strategy_check_time)
                else:
                    return passed_strategies
                
            except Exception as e:
                self.logger.error(f"❌ 开仓策略链检查异常: {e}")
                await asyncio.sleep(self.strategy_check_time)  # 异常时等待策略检查时间
        
        return None  # 如果stop_flag被设置，返回None
    
    async def wait_close(self):
        """等待平仓条件满足 - 在hedge_mode_abc中实现，检查所有策略"""
        self.logger.info("🔍 开始等待平仓条件满足...")
        
        while not self.stop_flag:
            try:
                # 按优先级顺序检查所有策略
                strategies = self.get_sorted_strategies('close')
                passed_strategies = []
                
                self.logger.info("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~") 
                for strategy in strategies:
                    try:
                        self.logger.info(f"[{strategy.name}] 开始检测平仓策略")
                        await strategy.can_close(self)
                        self.logger.info(f"[{strategy.name}] {strategy.reason}")
                        if strategy.result == HedgeStrategyResult.TRIGGER:
                            self.logger.info(f"[{strategy.name}] ✅ 平仓策略触发")
                            passed_strategies.append(strategy)  # 返回被触发的策略对象
                            return passed_strategies
                        elif strategy.result == HedgeStrategyResult.REJECT:
                            self.logger.info(f"[{strategy.name}] 当前策略平仓条件不满足，强制退出并等待下轮循环")
                            passed_strategies.clear()
                            break
                        elif strategy.result == HedgeStrategyResult.PASS:
                            self.logger.info(f"[{strategy.name}] ✅ 平仓策略通过")
                            passed_strategies.append(strategy)
                    except Exception as e:
                        self.logger.error(f"[{strategy.name}] ❌ 策略平仓检查异常: {e}")
                        continue
                if len(passed_strategies) == 0:
                    self.logger.info(f"🔄 无平仓策略触发，等待 {self.strategy_check_time} 秒后重新检查...")
                    await asyncio.sleep(self.strategy_check_time)
                else:
                    return passed_strategies

            except Exception as e:
                self.logger.error(f"❌ 平仓策略链检查异常: {e}")
                await asyncio.sleep(self.strategy_check_time)  # 异常时等待策略检查时间

        return None  # 如果stop_flag被设置，返回None
        
    @abstractmethod
    def primary_exchange_name(self):
        """Return the name of the primary exchange."""
        pass

    @abstractmethod
    def primary_client_vars(self):
        pass

    @abstractmethod
    def primary_client_init(self):
        pass

    def primary_logger_level(self):
        pass
    
    def primary_fee_rate(self) -> Decimal:
        """Return the taker fee rate for the primary exchange as a Decimal."""
        return Decimal('0.0001')  # Default to 0.01%, override in subclass if different

    def _initialize_log_file(self):
        # Initialize logging to file
        os.makedirs("logs", exist_ok=True)
        self.log_filename = f"logs/{self.primary_exchange_name()}_{self.ticker}_hedge_mode_log.txt"
        self.original_stdout = sys.stdout

    def _initialize_logger(self):
        # Setup logger
        self.logger = logging.getLogger(f"hedge_bot_{self.ticker}")
        self.logger.setLevel(logging.DEBUG)  # Set to DEBUG to show all our detailed logs

        # Clear any existing handlers to avoid duplicates
        self.logger.handlers.clear()

        # Disable verbose logging from external libraries
        logging.getLogger('urllib3').setLevel(logging.CRITICAL)
        logging.getLogger('requests').setLevel(logging.CRITICAL)
        logging.getLogger('websockets').setLevel(logging.CRITICAL)
        logging.getLogger('pysdk').setLevel(logging.CRITICAL)
        # todo: primary log level
        logging.getLogger('lighter').setLevel(logging.CRITICAL)
        logging.getLogger('lighter.signer_client').setLevel(logging.CRITICAL)

        # Disable root logger propagation to prevent external logs
        logging.getLogger().setLevel(logging.CRITICAL)

        # Create timezone-aware formatter
        import pytz
        import os
        
        class TimeZoneFormatter(logging.Formatter):
            def __init__(self, fmt=None, datefmt=None, tz=None):
                super().__init__(fmt=fmt, datefmt=datefmt)
                self.tz = tz

            def formatTime(self, record, datefmt=None):
                dt = datetime.fromtimestamp(record.created, tz=self.tz)
                if datefmt:
                    return dt.strftime(datefmt)
                return dt.isoformat()

        # Get timezone from environment or default to Asia/Shanghai
        timezone = pytz.timezone(os.getenv('TIMEZONE', 'Asia/Shanghai'))

        # Create file handler
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(logging.DEBUG)  # File captures all debug info

        # Create console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)  # Console shows important info only

        # Create professional formatters with timestamp and module info
        file_formatter = TimeZoneFormatter(
            '%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            tz=timezone
        )
        
        console_formatter = TimeZoneFormatter(
            '%(asctime)s.%(msecs)03d - %(levelname)s - %(name)s - [%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            tz=timezone
        )

        file_handler.setFormatter(file_formatter)
        console_handler.setFormatter(console_formatter)

        # Add handlers to logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        # Prevent propagation to root logger to avoid duplicate messages and external logs
        self.logger.propagate = False

    def _initialize_primary_client(self):
        for key, value in self.primary_client_vars().items():
            setattr(self, key, value)
        self.primary_client_init()

    async def _init_primary_contract_info(self) -> Tuple[str, Decimal]:
        """Get Primary contract ID and tick size."""
        if not self.primary_client:
            raise Exception(f"{self.primary_exchange_name()} client not initialized")

        contract_id, tick_size = await self.primary_client.get_contract_attributes()

        if self.order_quantity < self.primary_client.config.quantity:
            raise ValueError(
                f"Order quantity is less than min quantity: {self.order_quantity} < {self.primary_client.config.quantity}")
        self.primary_contract_id = contract_id
        self.primary_tick_size = tick_size

    async def _setup_primary_websocket(self):
        """Setup Primary websocket for order updates and order book data."""
        if not self.primary_client:
            raise Exception(f"{self.primary_exchange_name()} client not initialized")

        def order_update_handler(order_data):
            """Handle order updates from Primary WebSocket."""
            if order_data.get('contract_id') != self.primary_contract_id:
                return
            try:
                order_id = order_data.get('order_id')
                status = order_data.get('status')
                side = order_data.get('side', '').lower()
                filled_size = Decimal(order_data.get('filled_size', '0'))
                size = Decimal(order_data.get('size', '0'))
                price = order_data.get('price', '0')

                if side == 'buy':
                    order_type = "OPEN"
                else:
                    order_type = "CLOSE"

                if status == 'CANCELED' and filled_size > 0:
                    status = 'FILLED'

                # Handle the order update
                if status == 'FILLED' and self.primary_order_status != 'FILLED':
                    if side == 'buy':
                        self.primary_position += filled_size
                    else:
                        self.primary_position -= filled_size
                    self.logger.info(f"[WebSocket] [{order_id}] [{order_type}] [{self.primary_exchange_name()}] [{status}]: {filled_size} @ {price}")
                    self.primary_order_status = status

                    # Log Primary trade to CSV
                    log_trade_to_csv(
                        exchange=self.primary_exchange_name(),
                        ticker=self.ticker,
                        side=side,
                        price=str(price),
                        quantity=str(filled_size)
                    )

                    self.handle_primary_order_update({
                        'order_id': order_id,
                        'side': side,
                        'status': status,
                        'size': size,
                        'price': price,
                        'contract_id': self.primary_contract_id,
                        'filled_size': filled_size
                    })
                elif self.primary_order_status != 'FILLED':
                    if status == 'OPEN':
                        self.logger.info(f"[WebSocket] [{order_id}] [{order_type}] [{self.primary_exchange_name()}] [{status}]: {size} @ {price}")
                    else:
                        self.logger.info(f"[WebSocket] [{order_id}] [{order_type}] [{self.primary_exchange_name()}] [{status}]: {filled_size} @ {price}")
                    self.primary_order_status = status

            except Exception as e:
                self.logger.error(f"[WebSocket] Error handling {self.primary_exchange_name()} order update: {e}")

        try:
            # Setup order update handler
            self.primary_client.setup_order_update_handler(order_update_handler)
            self.logger.info(f"✅ {self.primary_exchange_name()} WebSocket order update handler set up")

            # Connect to Primary WebSocket
            await self.primary_client.connect()
            self.logger.info(f"✅ {self.primary_exchange_name()} WebSocket connection established")

        except Exception as e:
            self.logger.error(f"Could not setup {self.primary_exchange_name()} WebSocket handlers: {e}")
            sys.exit(1)

    def _update_lighter_position(self, position_change: Decimal, filled_price: Decimal, filled_quantity: Decimal):
        """Handle Lighter position change callback."""
        self.lighter_position += position_change
        self.logger.info(f"📊 Lighter position updated: {position_change:+} → {self.lighter_position}, filled price: {filled_price}, filled quantity: {filled_quantity}")
        self.current_lighter_price = filled_price
        self.current_lighter_quantity = filled_quantity

    def _set_stop_flag(self, stop: bool):
        self.stop_flag = stop
        self.lighter.stop_flag = stop
        self.monitor.set_stop_flag(stop)

    def shutdown(self, signum=None, frame=None):
        """Graceful shutdown handler."""
        self._set_stop_flag(True)
        self.logger.info("\n🛑 Stopping...")

        # Close WebSocket connections
        if self.primary_client:
            try:
                # Note: disconnect() is async, but shutdown() is sync
                # We'll let the cleanup happen naturally
                self.logger.info(f"🔌 {self.primary_exchange_name()} WebSocket will be disconnected")
            except Exception as e:
                self.logger.error(f"Error disconnecting {self.primary_exchange_name()} WebSocket: {e}")

        # Cancel Lighter WebSocket task
        if self.lighter.lighter_ws_task and not self.lighter.lighter_ws_task.done():
            try:
                self.lighter.lighter_ws_task.cancel()
                self.logger.info("🔌 Lighter WebSocket task cancelled")
            except Exception as e:
                self.logger.error(f"Error cancelling Lighter WebSocket task: {e}")

        # Close logging handlers properly
        for handler in self.logger.handlers[:]:
            try:
                handler.close()
                self.logger.removeHandler(handler)
            except Exception:
                pass

    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    async def fetch_primary_bbo_prices(self) -> Tuple[Decimal, Decimal]:
        """Fetch best bid/ask prices from Primary using REST API."""
        if not self.primary_client:
            raise Exception(f"{self.primary_exchange_name()} client not initialized")

        best_bid, best_ask = await self.primary_client.fetch_bbo_prices(self.primary_contract_id)

        return best_bid, best_ask

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Round price to tick size."""
        if self.primary_tick_size is None:
            return price
        return (price / self.primary_tick_size).quantize(Decimal('1')) * self.primary_tick_size

    async def place_bbo_order(self, side: str, quantity: Decimal):
        self.logger.info(f"📈 Fetching market prices for {side} order placement...")
        # Place the order using Primary client
        order_result = await self.primary_client.place_open_order(
            contract_id=self.primary_contract_id,
            quantity=quantity,
            direction=side.lower()
        )

        if order_result.success:
            self.logger.info(f"📋 Order placed successfully - ID: {order_result.order_id}, Price: {order_result.price}")
            return order_result.order_id, order_result.price
        else:
            self.logger.error(f"❌ Failed to place {side} order: {order_result.error_message}")
            raise Exception(f"Failed to place order: {order_result.error_message}")

    async def place_primary_post_only_order(self, side: str, quantity: Decimal, triggered_strategies: List[HedgeStrategy]):
        """Place a post-only order on Primary."""
        if not self.primary_client:
            raise Exception(f"{self.primary_exchange_name()} client not initialized")

        self.primary_order_status = None
        # 注意：不在此处重置计数器，保持跨调用的累积检测
        # 这里没有检查stop_flag有可能在ctl+c之后触发，需要在外层进行控制
        self.logger.info(f"[OPEN] [{self.primary_exchange_name()}] [{side}] Placing {self.primary_exchange_name()} POST-ONLY order")
        order_id, order_price = await self.place_bbo_order(side, quantity)

        start_time = time.time()
        last_log_time = 0
        log_interval = 5  # Log status every 5 seconds
        
        while not self.stop_flag:
            current_time = time.time()
            elapsed_time = current_time - start_time
            
            # Log status every 5 seconds
            if current_time - last_log_time >= log_interval:
                self.logger.info(f"⏳ Waiting for order fill - Status: {self.primary_order_status}, Elapsed: {elapsed_time:.1f}s")
                last_log_time = current_time
           
            # POST-ONLY的价差过大取消、当前非最优价格取消
            if self.primary_order_status == 'CANCELED':
                self.logger.info(f"🔄 Order was canceled, placing new order")
                
                self.primary_order_status = 'NEW'
                order_id, order_price = await self.place_bbo_order(side, quantity)
                start_time = time.time()
                self.logger.info(f"📝 New order placed - ID: {order_id}, Price: {order_price}")
                await asyncio.sleep(0.5)
            elif self.primary_order_status in ['NEW', 'OPEN', 'PENDING', 'CANCELING', 'PARTIALLY_FILLED']:
                await asyncio.sleep(0.5)
                
                # Check if we need to cancel and replace the order
                should_cancel = False
                best_bid, best_ask = await self.fetch_primary_bbo_prices()
                
                # Log price comparison details
                if side == 'buy':
                    self.logger.info(f"📊 Price check - Buy order: {order_price}, Best bid: {best_bid}, Best ask: {best_ask}")
                    if order_price < best_bid:
                        should_cancel = True
                        self.logger.info(f"💡 Buy order price {order_price} < best bid {best_bid}, should cancel")
                    else:
                        self.logger.info(f"✅ Buy order price {order_price} >= best bid {best_bid}, keeping order")
                else:
                    self.logger.info(f"📊 Price check - Sell order: {order_price}, Best bid: {best_bid}, Best ask: {best_ask}")
                    if order_price > best_ask:
                        should_cancel = True
                        self.logger.info(f"💡 Sell order price {order_price} > best ask {best_ask}, should cancel")
                    else:
                        self.logger.info(f"✅ Sell order price {order_price} <= best ask {best_ask}, keeping order")
                
                # Check if 15 seconds have passed
                # 只有超时15s后才需要重新走一遍策略，15s内可以根据最优化重新下单
                triggered_strategies_need_to_replace_order = triggered_strategies and any([strategy.order_place_timeout_retry() for strategy in triggered_strategies]) and elapsed_time > 15
                # 价格不利时立即取消重新下单，保证成功率
                if should_cancel or elapsed_time > 15:
                    # 触发策略无需重试且无需取消，继续等待
                    if not triggered_strategies_need_to_replace_order and not should_cancel:
                        self.logger.info(f"⏰ 15s timeout reached, but order {order_id} is at favorable price (bid: {best_bid}, ask: {best_ask}), continuing to wait")
                        start_time = time.time()  # Reset timer
                    else:
                        # 触发策略需要全部重试，或者仅primary order内部should_cancel重试（不返回直接接续循环）
                        cancel_warning  = f"should cancel order due to unfavorable price" if should_cancel else "15s timeout reached"
                        self.logger.info(f"⏰ {cancel_warning}, canceling order...")
                    
                        cancel_result = await self.primary_client.cancel_order(order_id)
                        self.logger.info(f"Order {order_id} canceled result: {cancel_result}")
                        if not cancel_result.success:
                            self.logger.error(f"❌ Error canceling {self.primary_exchange_name()} order: {cancel_result.error_message}")
                            # 取消失败时如果订单状态已经是CANCELED或FILLED，则更新状态，理论上不会再出现卡单状态
                            if cancel_result.status in ['CANCELED', 'FILLED']:
                                if cancel_result.status == 'FILLED' or cancel_result.filled_size > 0:
                                    self.logger.info(f"订单取消失败，但订单已全部或部分成交: {cancel_result.filled_size}, 重置 {self.primary_exchange_name()} 订单状态为 FILLED")
                                    order_data = {'side': cancel_result.side, 'price': cancel_result.price, 'filled_size': cancel_result.filled_size}
                                    # todo: 这里有很多重复代码可以优化, 还要调用log_trade_to_csv，参考order_update_handler
                                    # 这里如果处理了，后续websocket延迟更新还会再执行一次，通过self.primary_order_status判断是否已经被ws更新，同理ws中也通过self.primary_order_status判断是否已经被rest主动更新
                                    if self.primary_order_status != "FILLED":
                                        if cancel_result.side == "buy":
                                            self.primary_position += cancel_result.filled_size
                                        else:
                                            self.primary_position -= cancel_result.filled_size
                                        self.primary_order_status = 'FILLED'
                                        self.handle_primary_order_update(order_data)
                                    else:
                                        self.logger.info(f"primary order status 可能通过WebSocket回调已经更新为 FILLED, 跳过primary position更新，primary_position: {self.primary_position}")
                                    return HedgeOrderResult.SUCCESS
                                else:
                                    # 只有cancel+filled_size为0是真正的取消状态需要重试
                                    # should_cancel说明当前primary下单价格将会改变，重新计算策略条件（比如价差策略）
                                    if triggered_strategies_need_to_replace_order or should_cancel:
                                        self.logger.info(f"📋 订单已真正取消，当前策略需要重新进行策略判断")
                                        return HedgeOrderResult.RETRY_STRATEGY
                                    else:
                                        # 只需内部重试，更新状态等待while即可
                                        self.primary_order_status = cancel_result.status
                        else:
                            # 取消成功，需要重新进行策略判断
                            # 取消成功的场景下，有可能时间差的原因，ws又返回了FILLED的状态，实际已经成交，这种情况下需要返回开仓成功
                            if self.primary_order_status == "FILLED":
                                return HedgeOrderResult.SUCCESS
                            # 真实场景下，取消成功后也有可能是部分成交了
                            if cancel_result.status == 'FILLED' or cancel_result.filled_size > 0:
                                    self.logger.info(f"订单取消成功，但订单已全部或部分成交: {cancel_result.filled_size}, 重置 {self.primary_exchange_name()} 订单状态为 FILLED")
                                    order_data = {'side': cancel_result.side, 'price': cancel_result.price, 'filled_size': cancel_result.filled_size}
                                    if self.primary_order_status != "FILLED":
                                        if cancel_result.side == "buy":
                                            self.primary_position += cancel_result.filled_size
                                        else:
                                            self.primary_position -= cancel_result.filled_size
                                        self.primary_order_status = 'FILLED'
                                        self.handle_primary_order_update(order_data)
                                    else:
                                        self.logger.info(f"primary order status 可能通过WebSocket回调已经更新为 FILLED, 跳过primary position更新，primary_position: {self.primary_position}")
                                    return HedgeOrderResult.SUCCESS
                            # should_cancel说明当前primary下单价格将会改变，重新计算策略条件（比如价差策略）
                            if triggered_strategies_need_to_replace_order or should_cancel:
                                self.logger.info(f"📋 订单取消成功，当前策略需要重新进行策略判断")
                                return HedgeOrderResult.RETRY_STRATEGY
                            else:
                                self.logger.info("canceled order due to unfavorable price")
                                # 下轮循环重新下单
                                self.primary_order_status = cancel_result.status
            elif self.primary_order_status == 'FILLED':
                self.logger.info(f"✅ Order {order_id} filled successfully after {elapsed_time:.1f}s")
                # 这里不用调用，ws回调会自动更新
                # self.handle_primary_order_update(order_data)
                return HedgeOrderResult.SUCCESS
            else:
                if self.primary_order_status is not None:
                    self.logger.error(f"❌ Unknown {self.primary_exchange_name()} order status: {self.primary_order_status}")
                    return HedgeOrderResult.FAILED
                else:
                    # primary order status 为None的情况，可能是ws没有及时更新
                    # 或者client中place_open_order的POST-ONLY单子价差过大直接取消
                    # 这里可以尝试fetch一下状态
                    self.logger.info(f"⏳ No order status update yet, order status is: {self.primary_order_status}, websocket stream didnt update, try to fetch order status through REST API")
                    order_info = await self.primary_client.get_order_info(order_id)
                    if order_info is not None:
                        order_data = {'side': order_info.side, 'price': Decimal(order_info.price), 'filled_size': Decimal(order_info.filled_size)}
                        self.logger.info(f"get order info from REST API: {order_info}")
                        self.primary_order_status = order_info.status
                        # 手动fetch order status的场景需要主动调用 handle_primary_order_update
                        # 这里的状态有很多中，不需要再设置一次，下轮循环处理自动根据状态处理
                        if order_info.status == "FILLED" or order_info.filled_size > 0:
                            # 只有FILLED状态需要调用更新
                            self.logger.info(f"订单已全部或部分成交: {order_info.filled_size}, 重置 {self.primary_exchange_name()} 订单状态为 FILLED, 订单信息: {order_info}")
                            if self.primary_order_status != "FILLED":
                                if cancel_result.side == "buy":
                                    self.primary_position += cancel_result.filled_size
                                else:
                                    self.primary_position -= cancel_result.filled_size
                                self.primary_order_status = 'FILLED'
                                self.handle_primary_order_update(order_data)
                            else:
                                self.logger.info(f"primary order status 可能通过WebSocket回调已经更新为 FILLED, 跳过primary position更新，primary_position: {self.primary_position}")
                            # return也可以不需要，下轮循环直接走FILLED分支
                            return HedgeOrderResult.SUCCESS
                    else:
                        self.logger.info(f"Still cannt fetch order info from REST API, continue to wait...")
                    await asyncio.sleep(0.5)
        
        # 如果因为stop_flag退出循环，返回失败状态
        return HedgeOrderResult.FAILED

    def handle_primary_order_update(self, order_data):
        """Handle Primary order updates from WebSocket."""
        # filled的状态才能调用
        side = order_data.get('side', '').lower()
        filled_size = Decimal(order_data.get('filled_size', '0'))
        price = Decimal(order_data.get('price', '0'))

        if side == 'buy':
            lighter_side = 'sell'
        else:
            lighter_side = 'buy'

        # Store order details for immediate execution
        self.current_lighter_side = lighter_side
        self.current_primary_side = side
        self.current_primary_quantity = filled_size
        self.current_primary_price = price

        self.waiting_for_lighter_fill = True

    def _reset_order_state(self):
        """重置订单执行状态"""
        self.order_execution_complete = False
        self.waiting_for_lighter_fill = False

    async def _wait_for_lighter_execution(self, start_time: float) -> bool:
        """等待对冲订单执行完成，返回是否成功继续"""
        while not self.order_execution_complete and not self.stop_flag:
            # Check if Primary order filled and we need to place Lighter order
            if self.waiting_for_lighter_fill:
                result = await self.lighter.place_lighter_market_order(
                    self.current_lighter_side,
                    self.current_primary_quantity,
                    self.current_primary_price
                )
                # lighter订单失败重试机制
                self.order_execution_complete = result is not None
                # break

            await asyncio.sleep(0.01)
            if time.time() - start_time > 180:
                self.logger.error("❌ Timeout waiting for trade completion")
                return False
        return not self.stop_flag

    async def _execute_hedge_position(self, side: str, quantity: Decimal, triggered_strategies: List[HedgeStrategy]) -> tuple:
        """执行完整的对冲订单流程，返回(成功状态, 是否需要重试策略)"""
        self._reset_order_state()
        
        try:
            result = await self.place_primary_post_only_order(side, quantity, triggered_strategies)
            
            if result == HedgeOrderResult.RETRY_STRATEGY:
                # 需要重新进行策略判断
                self.logger.info(f"🔄 对冲订单执行需要重新执行策略判断")
                return False, True
            elif result == HedgeOrderResult.FAILED:
                # 彻底失败
                self.logger.error(f"❌ 对冲订单执行彻底失败")
                return False, False
            # OrderResult.SUCCESS 继续执行后续流程
            
        except Exception as e:
            self.logger.error(f"⚠️ Error in trading loop: {e}")
            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
            
            # 发送错误通知
            await self.monitor.send_error_notification(e, f"尝试执行{side}订单时发生错误，正在准备重试")
            
            return False, True
        
        # 执行对冲部分
        operation_start = time.time()  # 每次操作独立计时
        success = await self._wait_for_lighter_execution(operation_start)
        return success, False  # 返回成功状态，不需要重试策略


    def _determine_close_side_and_quantity(self) -> tuple:
        """确定平仓方向和数量，返回(side, quantity)或(None, None)表示不需要平仓"""
        if self.primary_position == 0:
            return None, None
        elif self.primary_position > 0:
            return 'sell', abs(self.primary_position)
        else:
            return 'buy', abs(self.primary_position)
    
    async def trading_loop(self):
        """Main trading loop implementing the new strategy."""
        self.logger.info(f"🚀 Starting hedge bot for {self.ticker}")

        # wait for all websockets and orderbook to be ready
        await asyncio.sleep(5)

        iterations = 0
        while iterations < self.iterations and not self.stop_flag:
            iterations += 1
            self.logger.info("-----------------------------------------------")
            self.logger.info(f"🔄 Trading loop iteration {iterations}")
            self.logger.info("-----------------------------------------------")

            # 执行前前确认lighter的order_book已经就绪，否则lighter无法开仓
            while not self.lighter.lighter_order_book_ready:
                self.logger.error(f"lighter's order book not ready, wait for order book data to continue")
                await asyncio.sleep(10)
            
            self.logger.info(f"[STEP 1] {self.primary_exchange_name()} position: {self.primary_position} | Lighter position: {self.lighter_position}")

            if abs(self.primary_position + self.lighter_position) > self.order_quantity * 2:
                self.logger.error(f"❌ Position diff is too large: {self.primary_position + self.lighter_position}")
                break

            open_side = 'buy'  # 默认值
            triggered_open_strategies = None
            # Step 1: 执行开仓策略链，获取触发的策略
            triggered_open_strategies = await self.wait_open()
            if triggered_open_strategies:
                change_side_strategies = [strategy for strategy in triggered_open_strategies if hasattr(strategy, "open_side") and strategy.open_side is not None]
                open_side = change_side_strategies[0].open_side if len(change_side_strategies) > 0 else open_side
            else:
                self.logger.warning("⚠️ 没有策略触发开仓，使用默认方向")
            
            # Step 1: 开仓（添加重试逻辑）
            max_retries = 1000
            success = False
            for retry_count in range(max_retries):
                if self.stop_flag:
                    self.logger.warning("收到退出信号，退出开仓流程")
                    break
                
                success, need_retry_strategy = await self._execute_hedge_position(open_side, self.order_quantity, triggered_open_strategies)
                
                if success:
                    break  # 成功，继续后续流程
                elif need_retry_strategy and retry_count < max_retries - 1:
                    self.logger.info(f"🔄 订单超时，重新检查开仓策略条件 (重试 {retry_count + 1}/{max_retries})")
                    # 重新获取开仓策略
                    triggered_open_strategies = await self.wait_open()
                    if triggered_open_strategies:
                        change_side_strategies = [strategy for strategy in triggered_open_strategies if hasattr(strategy, "open_side") and strategy.open_side is not None]
                        open_side = change_side_strategies[0].open_side if len(change_side_strategies) > 0 else open_side
                    else:
                        self.logger.warning("⚠️ 重试时没有策略触发开仓")
                        break
                else:
                    # 彻底失败或超过重试次数
                    self.logger.error("❌ 开仓执行失败，退出交易循环")
                    break
            
            # 如果最终未成功，退出
            if not success:
                break
            
            for strategy in triggered_open_strategies:
                strategy.after_open_hedge_position(self)

            # 开仓后发送通知并启动监控
            try:
                if triggered_open_strategies:
                    # 传递触发的策略给monitor
                    await self.monitor.send_position_open_notification(f"{iterations}/{self.iterations}", open_side, triggered_open_strategies)
                    
                    # 启动状态监控任务
                    self.monitor.start_status_monitor(
                        lambda: self.get_primary_position,
                        lambda: self.get_lighter_position, 
                        triggered_open_strategies,
                        self.primary_client,
                        self.lighter
                    )
                
            except Exception as e:
                self.logger.error(f"Failed to send open notification: {e}")

            if self.stop_flag:
                break
            
            close_side = 'sell' if open_side == 'buy' else 'buy'
            # Step 2: 执行平仓策略链，等待平仓条件满足
            triggered_close_strategies = await self.wait_close()
            

            # Step 2: 第一次平仓（添加重试逻辑）
            self.logger.info(f"[STEP 2] {self.primary_exchange_name()} position: {self.primary_position} | Lighter position: {self.lighter_position}")
            success = False
            for retry_count in range(max_retries):
                if self.stop_flag:
                    self.logger.warning("收到退出信号，退出平仓流程")
                    break
                
                success, need_retry_strategy = await self._execute_hedge_position(close_side, self.order_quantity, triggered_close_strategies)
                
                if success:
                    break  # 成功，继续后续流程
                elif need_retry_strategy and retry_count < max_retries - 1:
                    self.logger.info(f"🔄 平仓订单超时，重新检查平仓策略条件 (重试 {retry_count + 1}/{max_retries})")
                    # 重新获取平仓策略
                    triggered_close_strategies = await self.wait_close()
                    if not triggered_close_strategies:
                        self.logger.warning("⚠️ 重试时没有策略触发平仓")
                        break
                else:
                    # 彻底失败或超过重试次数
                    self.logger.error("❌ 平仓执行失败，退出交易循环")
                    break
            
            # 如果最终未成功，退出
            if not success:
                break
            
            for strategy in triggered_close_strategies:
                strategy.after_close_hedge_position(self)

            # Step 3: 剩余平仓(无需重试策略)
            self.logger.info(f"[STEP 3] {self.primary_exchange_name()} position: {self.primary_position} | Lighter position: {self.lighter_position}")
            final_close_side, final_close_quantity = self._determine_close_side_and_quantity()
            if final_close_side:
                success = False
                for retry_count in range(max_retries):
                    if self.stop_flag:
                        self.logger.warning("收到退出信号，退出平仓流程")
                        break

                    success, need_retry_strategy = await self._execute_hedge_position(final_close_side, final_close_quantity, triggered_close_strategies)

                    if success:
                        break  # 成功，继续后续流程
                    elif need_retry_strategy and retry_count < max_retries - 1:
                        self.logger.info(f"🔄 平仓订单超时，略过检查平仓策略条件 (重试 {retry_count + 1}/{max_retries})，等待时间 1s")
                        await asyncio.sleep(1)
                        # 重新获取平仓策略
                        # triggered_close_strategies = await self.wait_close()
                        # if not triggered_close_strategies:
                            # self.logger.warning("⚠️ 重试时没有策略触发平仓")
                            # break
                    else:
                        # 彻底失败或超过重试次数
                        self.logger.error("❌ 平仓执行失败，退出交易循环")
                        break
            
            if not success:
                break

            # 平仓完成后发送通知并停止监控
            try:
                if triggered_close_strategies:
                    # 传递触发的策略给monitor
                    await self.monitor.send_position_close_notification(
                        f"{iterations}/{self.iterations}",
                        close_side,
                        triggered_close_strategies,
                        primary_client=self.primary_client, 
                        lighter_proxy=self.lighter
                    )
                
                # 停止状态监控任务
                # self.monitor.stop_status_monitor()
                
            except Exception as e:
                self.logger.error(f"Failed to send close notification: {e}")

    async def run(self):
        """Run the hedge bot."""
        self.setup_signal_handlers()

        # 发送系统启动通知
        await self.monitor.send_startup_notification(self.iterations)

        try:
            await asyncio.gather(
                self._init_primary_contract_info(),
                self._setup_primary_websocket(),
                self.lighter.setup_ws_task()
            )
            await self.trading_loop()
        except KeyboardInterrupt:
            self.logger.info("\n🛑 Received interrupt signal...")
        except Exception as e:
            self.logger.error(f"❌ Error running hedge bot: {e}")
            self.logger.error(f"❌ Full traceback: {traceback.format_exc()}")
            
            # 发送系统错误通知
            await self.monitor.send_error_notification(e, "对冲系统运行时发生错误")
                
        finally:
            self.logger.info("🔄 Cleaning up...")
            
            # 停止状态监控任务
            self.monitor.stop_status_monitor()
            
            # 发送系统停止通知
            await self.monitor.send_shutdown_notification(self.primary_position, self.lighter_position)
                
            self.shutdown()


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Trading bot for Primary and Lighter')
    parser.add_argument('--exchange', type=str,
                        help='Exchange')
    parser.add_argument('--ticker', type=str, default='BTC',
                        help='Ticker symbol (default: BTC)')
    parser.add_argument('--size', type=str,
                        help='Number of tokens to buy/sell per order')
    parser.add_argument('--iter', type=int,
                        help='Number of iterations to run')
    parser.add_argument('--fill-timeout', type=int, default=5,
                        help='Timeout in seconds for maker order fills (default: 5)')

    return parser.parse_args()