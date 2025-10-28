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
from typing import Tuple

import sys
import os

from hedge.lighter_proxy import LighterProxy
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Config:
    """Simple config class to wrap dictionary for primary client."""

    def __init__(self, config_dict):
        for key, value in config_dict.items():
            setattr(self, key, value)


def log_trade_to_csv(exchange: str, ticker: str, side: str, price: str, quantity: str):
    """Log trade details to CSV file."""
    timestamp = datetime.now(pytz.UTC).isoformat()
    filename = f"logs/{exchange}_{ticker}_hedge_mode_trades.csv"
    """Initialize CSV file with headers if it doesn't exist."""
    if not os.path.exists(filename):
        with open(filename, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['exchange', 'timestamp', 'side', 'price', 'quantity'])

    with open(filename, 'a', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            exchange,
            timestamp,
            side,
            price,
            quantity
        ])


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
        self.logger.info(
            f"Contract info loaded - {self.primary_exchange_name()}: {self.primary_contract_id}, "f"Lighter: {self.lighter.lighter_market_index}")

        self.waiting_for_lighter_fill = False
        # State management
        self.stop_flag = False
        self.order_counter = 0

        # Order execution tracking
        self.order_execution_complete = False
        
        # 开平仓策略接口
        self.hedge_position_open_strategy = None
        self.hedge_position_close_strategy = None

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

    def _initialize_log_file(self):
        # Initialize logging to file
        os.makedirs("logs", exist_ok=True)
        self.log_filename = f"logs/{self.primary_exchange_name()}_{self.ticker}_hedge_mode_log.txt"
        self.original_stdout = sys.stdout

    def _initialize_logger(self):
        # Setup logger
        self.logger = logging.getLogger(f"hedge_bot_{self.ticker}")
        self.logger.setLevel(logging.INFO)

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

        # Create file handler
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(logging.INFO)

        # Create console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # Create different formatters for file and console
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')

        file_handler.setFormatter(file_formatter)
        console_handler.setFormatter(console_formatter)

        # Add handlers to logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        # Prevent propagation to root logger to avoid duplicate messages and external logs
        self.logger.propagate = False

        # Ensure our logger only shows our messages
        self.logger.setLevel(logging.INFO)

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

    # todo: 不同的primary可能ws有不同的结构
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
                    self.logger.info(f"[{order_id}] [{order_type}] [{self.primary_exchange_name()}] [{status}]: {filled_size} @ {price}")
                    self.primary_order_status = status

                    # Log Primary trade to CSV
                    log_trade_to_csv(
                        exchange=self.primary_exchange_name(),
                        ticker=self.ticker,
                        side=side,
                        price=str(price),
                        quantity=str(filled_size)
                    )
                    self.logger.info(
                        f"📊 Trade logged to CSV: {self.primary_exchange_name()} {side} {str(filled_size)} @ {price}")

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
                        self.logger.info(f"[{order_id}] [{order_type}] [{self.primary_exchange_name()}] [{status}]: {size} @ {price}")
                    else:
                        self.logger.info(f"[{order_id}] [{order_type}] [{self.primary_exchange_name()}] [{status}]: {filled_size} @ {price}")
                    self.primary_order_status = status

            except Exception as e:
                self.logger.error(f"Error handling {self.primary_exchange_name()} order update: {e}")

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

    def _update_lighter_position(self, position_change: Decimal):
        """Handle Lighter position change callback."""
        self.lighter_position += position_change
        self.logger.info(f"📊 Lighter position updated: {position_change:+} → {self.lighter_position}")

    def _set_stop_flag(self, stop: bool):
        self.stop_flag = stop
        self.lighter.stop_flag = stop

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
        # Place the order using Primary client
        order_result = await self.primary_client.place_open_order(
            contract_id=self.primary_contract_id,
            quantity=quantity,
            direction=side.lower()
        )

        if order_result.success:
            return order_result.order_id, order_result.price
        else:
            raise Exception(f"Failed to place order: {order_result.error_message}")

    async def place_primary_post_only_order(self, side: str, quantity: Decimal):
        """Place a post-only order on Primary."""
        if not self.primary_client:
            raise Exception(f"{self.primary_exchange_name()} client not initialized")

        self.primary_order_status = None
        self.logger.info(f"[OPEN] [{self.primary_exchange_name()}] [{side}] Placing {self.primary_exchange_name()} POST-ONLY order")
        order_id, order_price = await self.place_bbo_order(side, quantity)

        start_time = time.time()
        while not self.stop_flag:
            if self.primary_order_status == 'CANCELED':
                self.primary_order_status = 'NEW'
                order_id, order_price = await self.place_bbo_order(side, quantity)
                start_time = time.time()
                await asyncio.sleep(0.5)
            elif self.primary_order_status in ['NEW', 'OPEN', 'PENDING', 'CANCELING', 'PARTIALLY_FILLED']:
                await asyncio.sleep(0.5)
                # Check if we need to cancel and replace the order
                should_cancel = False
                best_bid, best_ask = await self.fetch_primary_bbo_prices()
                if side == 'buy':
                    if order_price < best_bid:
                        should_cancel = True
                else:
                    if order_price > best_ask:
                        should_cancel = True
                if time.time() - start_time > 10:
                    if should_cancel:
                        try:
                            # Cancel the order using Primary client
                            cancel_result = await self.primary_client.cancel_order(order_id)
                            if not cancel_result.success:
                                self.logger.error(f"❌ Error canceling {self.primary_exchange_name()} order: {cancel_result.error_message}")
                        except Exception as e:
                            self.logger.error(f"❌ Error canceling {self.primary_exchange_name()} order: {e}")
                    else:
                        self.logger.info(f"Order {order_id} is at best bid/ask, waiting for fill")
                        start_time = time.time()
            elif self.primary_order_status == 'FILLED':
                break
            else:
                if self.primary_order_status is not None:
                    self.logger.error(f"❌ Unknown {self.primary_exchange_name()} order status: {self.primary_order_status}")
                    break
                else:
                    await asyncio.sleep(0.5)

    def handle_primary_order_update(self, order_data):
        """Handle Primary order updates from WebSocket."""
        side = order_data.get('side', '').lower()
        filled_size = Decimal(order_data.get('filled_size', '0'))
        price = Decimal(order_data.get('price', '0'))

        if side == 'buy':
            lighter_side = 'sell'
        else:
            lighter_side = 'buy'

        # Store order details for immediate execution
        self.current_lighter_side = lighter_side
        self.current_lighter_quantity = filled_size
        self.current_lighter_price = price

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
                await self.lighter.place_lighter_market_order(
                    self.current_lighter_side,
                    self.current_lighter_quantity,
                    self.current_lighter_price
                )
                break

            await asyncio.sleep(0.01)
            if time.time() - start_time > 180:
                self.logger.error("❌ Timeout waiting for trade completion")
                return False
        return not self.stop_flag

    async def _execute_hedge_position(self, side: str, quantity: Decimal) -> bool:
        """执行完整的对冲订单流程，返回是否成功"""
        self._reset_order_state()
        
        try:
            await self.place_primary_post_only_order(side, quantity)
        except Exception as e:
            self.logger.error(f"⚠️ Error in trading loop: {e}")
            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
            return False
        
        operation_start = time.time()  # 每次操作独立计时
        return await self._wait_for_lighter_execution(operation_start)

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

            self.logger.info(f"[STEP 1] {self.primary_exchange_name()} position: {self.primary_position} | Lighter position: {self.lighter_position}")

            if abs(self.primary_position + self.lighter_position) > 0.2:
                self.logger.error(f"❌ Position diff is too large: {self.primary_position + self.lighter_position}")
                break

            if self.hedge_position_open_strategy:
                self.hedge_position_open_strategy.execute(self)
            # Step 1: 开仓
            if not await self._execute_hedge_position('buy', self.order_quantity):
                break

            if self.stop_flag:
                break

            if self.hedge_position_close_strategy:
                self.hedge_position_close_strategy.execute(self)
            # Step 2: 第一次平仓
            self.logger.info(f"[STEP 2] {self.primary_exchange_name()} position: {self.primary_position} | Lighter position: {self.lighter_position}")
            if not await self._execute_hedge_position('sell', self.order_quantity):
                break

            # Step 3: 剩余平仓
            self.logger.info(f"[STEP 3] {self.primary_exchange_name()} position: {self.primary_position} | Lighter position: {self.lighter_position}")
            close_side, close_quantity = self._determine_close_side_and_quantity()
            if close_side:
                if not await self._execute_hedge_position(close_side, close_quantity):
                    break

    async def run(self):
        """Run the hedge bot."""
        self.setup_signal_handlers()

        try:
            await asyncio.gather(
                await self._init_primary_contract_info(),
                await self._setup_primary_websocket(),
                await self.lighter.setup_ws_task()
            )
            await self.trading_loop()
        except KeyboardInterrupt:
            self.logger.info("\n🛑 Received interrupt signal...")
        finally:
            self.logger.info("🔄 Cleaning up...")
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