"""
Modular Trading Bot - Supports multiple exchanges
"""

import os
import time
import asyncio
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from exchanges import ExchangeFactory
from exchanges.base import OrderResult
from helpers import TradingLogger, TradingStats
from helpers.lark_bot import LarkBot
from helpers.telegram_bot import TelegramBot


@dataclass
class TradingConfig:
    """Configuration class for trading parameters."""
    ticker: str
    contract_id: str
    quantity: Decimal
    take_profit: Decimal
    tick_size: Decimal
    direction: str
    max_orders: int
    wait_time: int
    exchange: str
    grid_step: Decimal
    stop_price: Decimal
    pause_price: Decimal
    boost_mode: bool
    use_ioc_optimization: bool = False  # Enable IOC+market fallback optimization

    @property
    def close_order_side(self) -> str:
        """Get the close order side based on bot direction."""
        return 'buy' if self.direction == "sell" else 'sell'


@dataclass
class OrderMonitor:
    """Thread-safe order monitoring state."""
    order_id: Optional[str] = None
    filled: bool = False
    filled_price: Optional[Decimal] = None
    filled_qty: Decimal = 0.0

    def reset(self):
        """Reset the monitor state."""
        self.order_id = None
        self.filled = False
        self.filled_price = None
        self.filled_qty = 0.0


class TradingBot:
    """Modular Trading Bot - Main trading logic supporting multiple exchanges."""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.logger = TradingLogger(config.exchange, config.ticker, log_to_console=True)

        # Create exchange client
        try:
            self.exchange_client = ExchangeFactory.create_exchange(
                config.exchange,
                config
            )
        except ValueError as e:
            raise ValueError(f"Failed to create exchange client: {e}")

        # Trading state
        self.active_close_orders = []
        self.last_close_orders = 0
        self.last_open_order_time = 0
        self.last_log_time = 0
        self.current_order_status = None
        self.order_filled_event = asyncio.Event()
        self.order_canceled_event = asyncio.Event()
        self.shutdown_requested = False
        self.loop = None

        self.order_utilization_alerts = {
            0.5: False,
            0.8: False,
            1.0: False,
        }

        self.open_positions = []
        self.loss_alert_thresholds = [Decimal('0.5'), Decimal('0.8'), Decimal('1.0')]
        self.last_loss_check_time = 0
        self.loss_check_interval = 60
        self.cumulative_trade_count = 0
        self.cumulative_base_volume = Decimal('0')
        self.cumulative_quote_volume = Decimal('0')
        self.last_report_time = 0
        self.report_interval = 1800
        
        # Enhanced statistics tracking
        self.stats = TradingStats()

        # Register order callback
        self._setup_websocket_handlers()

    async def graceful_shutdown(self, reason: str = "Unknown"):
        """Perform graceful shutdown of the trading bot."""
        self.logger.log(f"Starting graceful shutdown: {reason}", "INFO")
        self.shutdown_requested = True

        try:
            # Disconnect from exchange
            await self.exchange_client.disconnect()
            self.logger.log("Graceful shutdown completed", "INFO")

        except Exception as e:
            self.logger.log(f"Error during graceful shutdown: {e}", "ERROR")

    def _setup_websocket_handlers(self):
        """Setup WebSocket handlers for order updates."""
        def order_update_handler(message):
            """Handle order updates from WebSocket."""
            try:
                # Check if this is for our contract
                if message.get('contract_id') != self.config.contract_id:
                    return

                order_id = message.get('order_id')
                status = message.get('status')
                side = message.get('side', '')
                order_type = message.get('order_type', '')
                filled_size = Decimal(message.get('filled_size'))
                try:
                    price = Decimal(str(message.get('price', '0')))
                except Exception:
                    price = Decimal('0')
                if order_type == "OPEN":
                    self.current_order_status = status

                if status == 'FILLED':
                    if order_type == "OPEN":
                        self._record_open_fill(filled_size, price)
                    else:
                        self._record_close_fill(filled_size, price)

                    if order_type == "OPEN":
                        self.order_filled_amount = filled_size
                        # Ensure thread-safe interaction with asyncio event loop
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_filled_event.set)
                        else:
                            # Fallback (should not happen after run() starts)
                            self.order_filled_event.set()

                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{message.get('size')} @ {message.get('price')}", "INFO")
                    self.logger.log_transaction(order_id, side, message.get('size'), message.get('price'), status)
                elif status == "CANCELED":
                    if order_type == "OPEN":
                        self.order_filled_amount = filled_size
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_canceled_event.set)
                        else:
                            self.order_canceled_event.set()

                        if self.order_filled_amount > 0:
                            self.logger.log_transaction(order_id, side, self.order_filled_amount, message.get('price'), status)
                            
                    # PATCH
                    if self.config.exchange == "extended":
                        self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                        f"{Decimal(message.get('size')) - filled_size} @ {message.get('price')}", "INFO")
                    else:
                        self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                        f"{message.get('size')} @ {message.get('price')}", "INFO")
                elif status == "PARTIALLY_FILLED":
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{filled_size} @ {message.get('price')}", "INFO")
                else:
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{message.get('size')} @ {message.get('price')}", "INFO")

            except Exception as e:
                self.logger.log(f"Error handling order update: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

        # Setup order update handler
        self.exchange_client.setup_order_update_handler(order_update_handler)

    def _calculate_wait_time(self) -> Decimal:
        """Calculate wait time between orders."""
        cool_down_time = self.config.wait_time

        if len(self.active_close_orders) < self.last_close_orders:
            self.last_close_orders = len(self.active_close_orders)
            return 0

        self.last_close_orders = len(self.active_close_orders)
        if len(self.active_close_orders) >= self.config.max_orders:
            return 1

        if len(self.active_close_orders) / self.config.max_orders >= 2/3:
            cool_down_time = 2 * self.config.wait_time
        elif len(self.active_close_orders) / self.config.max_orders >= 1/3:
            cool_down_time = self.config.wait_time
        elif len(self.active_close_orders) / self.config.max_orders >= 1/6:
            cool_down_time = self.config.wait_time / 2
        else:
            cool_down_time = self.config.wait_time / 4

        # if the program detects active_close_orders during startup, it is necessary to consider cooldown_time
        if self.last_open_order_time == 0 and len(self.active_close_orders) > 0:
            self.last_open_order_time = time.time()

        if time.time() - self.last_open_order_time > cool_down_time:
            return 0
        else:
            return 1

    async def _place_and_monitor_open_order(self) -> bool:
        """Place an order and monitor its execution."""
        try:
            # Reset state before placing order
            self.order_filled_event.clear()
            self.current_order_status = 'OPEN'
            self.order_filled_amount = 0.0

            # Place the order
            # 等待 WebSocket 事件同步，避免上一订单状态未更新导致重复下单
            await asyncio.sleep(0.2)

            order_result = await self.exchange_client.place_open_order(
                self.config.contract_id,
                self.config.quantity,
                self.config.direction
            )

            if not order_result.success:
                return False

            if order_result.status == 'FILLED':
                return await self._handle_order_result(order_result)
            elif not self.order_filled_event.is_set():
                try:
                    await asyncio.wait_for(self.order_filled_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass

            # Handle order result
            return await self._handle_order_result(order_result)

        except Exception as e:
            self.logger.log(f"Error placing order: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return False

    async def _get_mid_price(self) -> Decimal:
        """Get the current mid price from the order book."""
        try:
            best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
            return (best_bid + best_ask) / Decimal('2')
        except Exception as e:
            self.logger.log(f"Error getting mid price: {e}", "ERROR")
            # Fallback to using get_order_price
            return await self.exchange_client.get_order_price(self.config.close_order_side)

    async def _smart_close_with_ioc(self, quantity: Decimal, side: str) -> OrderResult:
        """
        Smart close: Try IOC limit order first, fall back to market order if needed.
        
        Args:
            quantity: The quantity to close
            side: The side of the close order ('buy' or 'sell')
            
        Returns:
            OrderResult with filled information
        """
        # Get current market price
        mid_price = await self._get_mid_price()
        
        # Calculate IOC price (allow small slippage tolerance)
        ioc_tolerance = Decimal('0.0001')  # 0.01% tolerance
        if side == 'sell':
            ioc_price = mid_price * (Decimal('1') - ioc_tolerance)
        else:  # buy
            ioc_price = mid_price * (Decimal('1') + ioc_tolerance)
        
        ioc_result = None
        remaining_quantity = quantity
        
        # Try IOC limit order first
        try:
            self.logger.log(f"[CLOSE_IOC] Attempting IOC order: {quantity} @ {ioc_price}", "INFO")
            
            # Record IOC attempt (protected)
            try:
                self.stats.record_ioc_attempt(quantity)
            except Exception:
                pass
            
            ioc_result = await self.exchange_client.place_ioc_order(
                self.config.contract_id,
                quantity,
                ioc_price,
                side
            )
            
            if ioc_result.success and ioc_result.filled_size:
                filled_size = ioc_result.filled_size
                remaining_quantity = quantity - filled_size
                
                if filled_size >= quantity:
                    # Fully filled via IOC!
                    self.logger.log(
                        f"[CLOSE_IOC] ✅ IOC fully filled: {filled_size} @ {ioc_result.price}", 
                        "INFO"
                    )
                    # Record IOC success (protected)
                    try:
                        self.stats.record_ioc_result(filled_size, quantity, False)
                    except Exception:
                        pass
                    return ioc_result
                elif filled_size > 0:
                    # Partially filled
                    self.logger.log(
                        f"[CLOSE_IOC] ⚠️ IOC partially filled: {filled_size}/{quantity}, "
                        f"remaining: {remaining_quantity}",
                        "INFO"
                    )
            else:
                # IOC didn't fill at all
                self.logger.log(f"[CLOSE_IOC] IOC not filled, will use market order", "INFO")
                
        except Exception as e:
            self.logger.log(f"[CLOSE_IOC] IOC order error: {e}, falling back to market order", "WARN")
        
        # Fall back to market order for remaining quantity
        if remaining_quantity > 0:
            self.logger.log(
                f"[CLOSE_MARKET] Placing market order for remaining: {remaining_quantity}",
                "INFO"
            )
            
            try:
                market_result = await self.exchange_client.place_market_order(
                    self.config.contract_id,
                    remaining_quantity,
                    side
                )
                
                if market_result.success:
                    self.logger.log(
                        f"[CLOSE_MARKET] ✅ Market order filled: {market_result.filled_size} @ {market_result.price}",
                        "INFO"
                    )
                    
                    # Record IOC result with market fallback (protected)
                    if ioc_result and ioc_result.success and ioc_result.filled_size > 0:
                        try:
                            self.stats.record_ioc_result(ioc_result.filled_size, quantity, True)
                        except Exception:
                            pass
                    
                    # Combine IOC and market results
                    if ioc_result and ioc_result.success and ioc_result.filled_size > 0:
                        # Both IOC and market filled
                        total_filled = ioc_result.filled_size + market_result.filled_size
                        # Weighted average price
                        avg_price = (
                            (ioc_result.price * ioc_result.filled_size + 
                             market_result.price * market_result.filled_size) / total_filled
                        )
                        
                        return OrderResult(
                            success=True,
                            order_id=market_result.order_id,
                            side=side,
                            size=total_filled,
                            price=avg_price,
                            status='FILLED',
                            filled_size=total_filled
                        )
                    else:
                        return market_result
                else:
                    self.logger.log(
                        f"[CLOSE_MARKET] ❌ Market order failed: {market_result.error_message}",
                        "ERROR"
                    )

                    # 兜底的 MARKET 订单失败，统一触发 TG 告警
                    ioc_filled = ioc_result.filled_size if (ioc_result and ioc_result.success) else 0
                    remaining = quantity - ioc_filled
                    alert_msg = (
                        f"⚠️ [{self.config.exchange.upper()}_{self.config.contract}] "
                        f"兜底平仓订单失败，请手动处理！\n\n"
                        f"IOC 成交: {ioc_filled}/{quantity}\n"
                        f"剩余数量: {remaining}\n"
                        f"失败原因: {market_result.error_message}\n\n"
                        f"当前可能有未平仓位，请检查并手动平仓！"
                    )
                    try:
                        await self.send_notification(alert_msg)
                    except Exception as e:
                        self.logger.log(f"Failed to send TG alert: {e}", "ERROR")

                    # Check if IOC had partial fill - don't lose that information!
                    if ioc_result and ioc_result.success and ioc_result.filled_size > 0:
                        self.logger.log(
                            f"[CLOSE_IOC] ⚠️ Returning partial fill from IOC: {ioc_result.filled_size}/{quantity}",
                            "WARN"
                        )
                        return OrderResult(
                            success=True,  # IOC partially succeeded
                            order_id=ioc_result.order_id,
                            side=side,
                            size=quantity,
                            price=ioc_result.price,
                            status='PARTIALLY_FILLED',
                            filled_size=ioc_result.filled_size
                        )
                    else:
                        # Complete failure - neither IOC nor market worked
                        return market_result
                    
            except Exception as e:
                self.logger.log(f"[CLOSE_MARKET] Market order error: {e}", "ERROR")
                
                # Check if IOC had partial fill - don't lose that information!
                if ioc_result and ioc_result.success and ioc_result.filled_size > 0:
                    self.logger.log(
                        f"[CLOSE_IOC] ⚠️ Returning partial fill from IOC after market error: {ioc_result.filled_size}/{quantity}",
                        "WARN"
                    )
                    return OrderResult(
                        success=True,  # IOC partially succeeded
                        order_id=ioc_result.order_id,
                        side=side,
                        size=quantity,
                        price=ioc_result.price,
                        status='PARTIALLY_FILLED',
                        filled_size=ioc_result.filled_size
                    )
                else:
                    # Complete failure
                    return OrderResult(success=False, error_message=str(e))
        
        # Should not reach here, but return ioc_result as fallback
        return ioc_result if ioc_result else OrderResult(success=False, error_message="No orders placed")

    async def _handle_order_result(self, order_result) -> bool:
        """Handle the result of an order placement."""
        order_id = order_result.order_id
        filled_price = order_result.price

        if self.order_filled_event.is_set() or order_result.status == 'FILLED':
            if self.config.boost_mode:
                # Use IOC optimization if enabled
                if self.config.use_ioc_optimization:
                    close_order_result = await self._smart_close_with_ioc(
                        self.config.quantity,
                        self.config.close_order_side
                    )
                else:
                    # Traditional market order
                    close_order_result = await self.exchange_client.place_market_order(
                        self.config.contract_id,
                        self.config.quantity,
                        self.config.close_order_side
                    )
            else:
                self.last_open_order_time = time.time()
                # Place close order
                close_side = self.config.close_order_side
                if close_side == 'sell':
                    close_price = filled_price * (1 + self.config.take_profit/100)
                else:
                    close_price = filled_price * (1 - self.config.take_profit/100)

                close_order_result = await self.exchange_client.place_close_order(
                    self.config.contract_id,
                    self.config.quantity,
                    close_price,
                    close_side
                )
                if self.config.exchange == "lighter":
                    await asyncio.sleep(1)

                if not close_order_result.success:
                    self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")
                    raise Exception(f"[CLOSE] Failed to place close order: {close_order_result.error_message}")

                return True

        else:
            new_order_price = await self.exchange_client.get_order_price(self.config.direction)

            def should_wait(direction: str, new_order_price: Decimal, order_result_price: Decimal) -> bool:
                if direction == "buy":
                    return new_order_price <= order_result_price
                elif direction == "sell":
                    return new_order_price >= order_result_price
                return False

            if self.config.exchange == "lighter":
                current_order_status = self.exchange_client.current_order.status
            else:
                order_info = await self.exchange_client.get_order_info(order_id)
                current_order_status = order_info.status

            while (
                should_wait(self.config.direction, new_order_price, order_result.price)
                and current_order_status == "OPEN"
            ):
                self.logger.log(f"[OPEN] [{order_id}] Waiting for order to be filled @ {order_result.price}", "INFO")
                await asyncio.sleep(5)
                if self.config.exchange == "lighter":
                    current_order_status = self.exchange_client.current_order.status
                else:
                    order_info = await self.exchange_client.get_order_info(order_id)
                    if order_info is not None:
                        current_order_status = order_info.status
                new_order_price = await self.exchange_client.get_order_price(self.config.direction)

            self.order_canceled_event.clear()
            # Cancel the order if it's still open
            self.logger.log(f"[OPEN] [{order_id}] Cancelling order and placing a new order", "INFO")
            if self.config.exchange == "lighter":
                cancel_result = await self.exchange_client.cancel_order(order_id)
                start_time = time.time()
                while (time.time() - start_time < 10 and self.exchange_client.current_order.status != 'CANCELED' and
                        self.exchange_client.current_order.status != 'FILLED'):
                    await asyncio.sleep(0.1)

                if self.exchange_client.current_order.status not in ['CANCELED', 'FILLED']:
                    raise Exception(f"[OPEN] Error cancelling order: {self.exchange_client.current_order.status}")
                else:
                    self.order_filled_amount = self.exchange_client.current_order.filled_size
            else:
                # 在取消前，先确认订单当前状态
                try:
                    order_info_before_cancel = await self.exchange_client.get_order_info(order_id)
                    if order_info_before_cancel is None:
                        self.logger.log(f"[OPEN] Failed to get order info for {order_id}, will attempt cancel anyway", "WARNING")
                        pre_cancel_status = "UNKNOWN"
                    else:
                        pre_cancel_status = order_info_before_cancel.status

                    # 如果订单已经成交，直接处理成交，跳过取消
                    if pre_cancel_status == 'FILLED':
                        self.order_filled_amount = order_info_before_cancel.filled_size
                        self.order_canceled_event.set()
                        self.logger.log(f"[OPEN] Order {order_id} already filled: {order_info_before_cancel.filled_size}, skipping cancel", "INFO")
                    # 如果订单已经取消，记录成交量并继续
                    elif pre_cancel_status == 'CANCELED':
                        self.order_filled_amount = order_info_before_cancel.filled_size
                        self.order_canceled_event.set()
                        self.logger.log(f"[OPEN] Order {order_id} already canceled, filled: {order_info_before_cancel.filled_size}", "INFO")
                    # 订单状态为 OPEN 或 UNKNOWN，尝试取消
                    else:
                        try:
                            cancel_result = await self.exchange_client.cancel_order(order_id)
                            if not cancel_result.success:
                                # 取消失败时，尝试查询订单状态
                                self.logger.log(f"[CLOSE] Failed to cancel order {order_id}: {cancel_result.error_message}", "WARNING")

                                # 尝试查询订单信息
                                try:
                                    order_info = await self.exchange_client.get_order_info(order_id)
                                    if order_info is not None:
                                        # 只有查询成功才设置事件和成交量
                                        self.order_filled_amount = order_info.filled_size
                                        self.order_canceled_event.set()
                                        self.logger.log(f"[OPEN] Order {order_id} status: {order_info.status}, filled: {order_info.filled_size}", "INFO")
                                    else:
                                        # 查询返回 None，不设置事件，让 timeout 机制处理
                                        self.logger.log(f"[CLOSE] Query returned None for {order_id}, will use timeout fallback", "WARNING")
                                except Exception as query_err:
                                    # 查询失败，不设置事件，让 timeout 机制处理
                                    self.logger.log(f"[CLOSE] Query failed for {order_id}: {query_err}, will use timeout fallback", "WARNING")
                            else:
                                # 取消成功
                                self.current_order_status = "CANCELED"

                        except Exception as e:
                            # 取消异常，不设置事件，让 timeout 机制兜底
                            self.logger.log(f"[CLOSE] Error canceling order {order_id}: {e}", "ERROR")

                except Exception as e:
                    # 查询状态失败，记录错误但仍尝试取消
                    self.logger.log(f"[OPEN] Error checking order status before cancel: {e}, will attempt cancel anyway", "WARNING")
                    try:
                        cancel_result = await self.exchange_client.cancel_order(order_id)
                        if not cancel_result.success:
                            self.logger.log(f"[CLOSE] Failed to cancel order {order_id}: {cancel_result.error_message}", "WARNING")
                        else:
                            self.current_order_status = "CANCELED"
                    except Exception as cancel_err:
                        self.logger.log(f"[CLOSE] Error canceling order {order_id}: {cancel_err}", "ERROR")

                if self.config.exchange == "backpack" or self.config.exchange == "extended":
                    self.order_filled_amount = cancel_result.filled_size
                else:
                    # Wait for cancel event or timeout
                    if not self.order_canceled_event.is_set():
                        try:
                            await asyncio.wait_for(self.order_canceled_event.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            order_info = await self.exchange_client.get_order_info(order_id)
                            self.order_filled_amount = order_info.filled_size

            # 如果有成交量，需要平仓（部分成交或完全成交）
            if self.order_filled_amount > 0:
                close_side = self.config.close_order_side

                # 记录状态信息用于调试
                self.logger.log(
                    f"[CLOSE] Need to close {self.order_filled_amount} from cancelled/filled order {order_id}",
                    "DEBUG"
                )

                if self.config.boost_mode:
                    # boost 模式：使用 IOC/MARKET 立即平仓
                    if self.config.use_ioc_optimization:
                        close_order_result = await self._smart_close_with_ioc(
                            self.order_filled_amount,
                            close_side
                        )
                    else:
                        close_order_result = await self.exchange_client.place_market_order(
                            self.config.contract_id,
                            self.order_filled_amount,
                            close_side
                        )
                else:
                    # 非 boost 模式：使用 LIMIT 订单
                    if close_side == 'sell':
                        close_price = filled_price * (1 + self.config.take_profit/100)
                    else:
                        close_price = filled_price * (1 - self.config.take_profit/100)

                    close_order_result = await self.exchange_client.place_close_order(
                        self.config.contract_id,
                        self.order_filled_amount,
                        close_price,
                        close_side
                    )
                    if self.config.exchange == "lighter":
                        await asyncio.sleep(1)

                self.last_open_order_time = time.time()
                if not close_order_result.success:
                    self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")

            return True

        return False

    async def _log_status_periodically(self):
        """Log status information periodically, including positions."""
        if time.time() - self.last_log_time > 60 or self.last_log_time == 0:
            print("--------------------------------")
            try:
                # Get active orders
                active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)

                # Filter close orders
                self.active_close_orders = []
                for order in active_orders:
                    if order.side == self.config.close_order_side:
                        self.active_close_orders.append({
                            'id': order.order_id,
                            'price': order.price,
                            'size': order.size
                        })

                # Get positions
                position_amt = await self.exchange_client.get_account_positions()

                # Calculate active closing amount
                active_close_amount = sum(
                    Decimal(order.get('size', 0))
                    for order in self.active_close_orders
                    if isinstance(order, dict)
                )

                self.logger.log(f"Current Position: {position_amt} | Active closing amount: {active_close_amount} | "
                                f"Order quantity: {len(self.active_close_orders)}")
                self.last_log_time = time.time()
                await self._maybe_send_order_utilization_alert(len(self.active_close_orders))
                await self._check_position_loss()
                await self._maybe_send_runtime_report(position_amt, active_close_amount)
                # Check for position mismatch
                if abs(position_amt - active_close_amount) > (2 * self.config.quantity):
                    error_message = f"\n\nERROR: [{self.config.exchange.upper()}_{self.config.ticker.upper()}] "
                    error_message += "Position mismatch detected\n"
                    error_message += "###### ERROR ###### ERROR ###### ERROR ###### ERROR #####\n"
                    error_message += "Please manually rebalance your position and take-profit orders\n"
                    error_message += "请手动平衡当前仓位和正在关闭的仓位\n"
                    error_message += f"current position: {position_amt} | active closing amount: {active_close_amount} | "f"Order quantity: {len(self.active_close_orders)}\n"
                    error_message += "###### ERROR ###### ERROR ###### ERROR ###### ERROR #####\n"
                    self.logger.log(error_message, "ERROR")

                    await self.send_notification(error_message.lstrip())

                    if not self.shutdown_requested:
                        self.shutdown_requested = True

                    mismatch_detected = True
                else:
                    mismatch_detected = False

                return mismatch_detected

            except Exception as e:
                self.logger.log(f"Error in periodic status check: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

            print("--------------------------------")

    async def _maybe_send_order_utilization_alert(self, active_close_count: int):
        if self.config.max_orders <= 0:
            return

        utilization = active_close_count / self.config.max_orders
        for threshold, sent in self.order_utilization_alerts.items():
            if not sent and utilization >= threshold:
                current_pct = round(utilization * 100, 1)
                message = (
                    f"🚨 风险提醒 | {self.config.exchange.upper()}_{self.config.ticker.upper()} 当前已有 "
                    f"{active_close_count}/{self.config.max_orders} (≈{current_pct:.1f}%) 平仓单，"
                    f"达到 {int(threshold * 100)}% 阈值，请注意潜在下跌风险。"
                )
                await self.send_notification(message)
                self.order_utilization_alerts[threshold] = True

    def _record_open_fill(self, size: Decimal, price: Decimal):
        if size <= 0:
            return

        self.cumulative_trade_count += 1
        self.cumulative_base_volume += size
        self.cumulative_quote_volume += size * price
        
        # Enhanced statistics (protected to not affect core logic)
        try:
            self.stats.record_trade(size, price)
        except Exception:
            pass  # Silently ignore stats errors
        
        alerts = {threshold: False for threshold in self.loss_alert_thresholds}
        self.open_positions.append({
            "size": size,
            "price": price,
            "alerts": alerts,
        })

    def _record_close_fill(self, size: Decimal, price: Decimal):
        if size <= 0:
            return

        self.cumulative_trade_count += 1
        self.cumulative_base_volume += size
        self.cumulative_quote_volume += size * price
        
        # Enhanced statistics (protected to not affect core logic)
        try:
            self.stats.record_trade(size, price)
        except Exception:
            pass  # Silently ignore stats errors

        remaining = size
        while remaining > 0 and self.open_positions:
            current = self.open_positions[0]
            if current["size"] <= remaining:
                remaining -= current["size"]
                self.open_positions.pop(0)
            else:
                current["size"] -= remaining
                remaining = Decimal('0')

    async def _check_position_loss(self):
        if not self.open_positions:
            return

        now = time.time()
        if now - self.last_loss_check_time < self.loss_check_interval:
            return

        try:
            best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
        except Exception as e:
            self.logger.log(f"Failed to fetch order book for loss check: {e}", "WARNING")
            return

        current_price = best_bid if self.config.direction == "buy" else best_ask
        if current_price <= 0:
            current_price = Decimal('0')

        self.last_loss_check_time = now

        for position in self.open_positions:
            entry_price = position["price"]
            if entry_price <= 0:
                continue

            if self.config.direction == "buy":
                loss_pct = max(Decimal('0'), (entry_price - current_price) / entry_price)
            else:
                loss_pct = max(Decimal('0'), (current_price - entry_price) / entry_price)

            for threshold in self.loss_alert_thresholds:
                if position["alerts"].get(threshold):
                    continue
                if loss_pct >= threshold:
                    loss_percent = loss_pct * Decimal('100')
                    message = (
                        f"🚨 亏损告警 | {self.config.exchange.upper()}_{self.config.ticker.upper()} 仓位亏损约 "
                        f"{loss_percent:.1f}% (入场价 {entry_price:.4f}, 当前价 {current_price:.4f}, 数量 {position['size']:.4f})。"
                    )
                    await self.send_notification(message)
                    position["alerts"][threshold] = True

    async def _maybe_send_runtime_report(self, position_amt: Decimal, active_close_amount: Decimal):
        now_ts = time.time()
        if self.last_report_time != 0 and now_ts - self.last_report_time < self.report_interval:
            return

        try:
            await self._send_enhanced_report(position_amt, active_close_amount)
        except Exception as e:
            # Fallback to simple report if enhanced report fails
            self.logger.log(f"Enhanced report failed, using fallback: {e}", "WARN")
            await self._send_simple_report(position_amt, active_close_amount)
        
        self.last_report_time = now_ts
    
    async def _send_simple_report(self, position_amt: Decimal, active_close_amount: Decimal):
        """Fallback simple report (original format)"""
        active_close_count = len(self.active_close_orders)
        remaining_capacity = max(self.config.max_orders - active_close_count, 0)
        lines = [
            f"[运行统计] {self.config.exchange.upper()}_{self.config.ticker.upper()}",
            f"- 当前持仓: {self._fmt_decimal(position_amt)}",
            f"- 活跃平仓订单数量: {active_close_count}",
            f"- 活跃平仓订单总量: {self._fmt_decimal(active_close_amount)}",
            f"- 剩余下单额度: {remaining_capacity}",
            f"- 当前持仓笔数: {len(self.open_positions)}",
            f"- 累计交易次数: {self.cumulative_trade_count}",
        ]
        await self.send_notification("\n".join(lines))
    
    async def _send_enhanced_report(self, position_amt: Decimal, active_close_amount: Decimal):
        """Enhanced report with detailed statistics (Boost mode optimized)"""
        active_close_count = len(self.active_close_orders)
        
        # Get market prices (protected)
        try:
            best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
            mid_price = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            spread_pct = (spread / mid_price * 100) if mid_price > 0 else Decimal('0')
            self.stats.record_price_sample(best_bid, best_ask)
        except:
            best_bid = best_ask = mid_price = spread = spread_pct = Decimal('0')

        # Build report
        mode_label = "Boost刷量" if self.config.boost_mode else "网格交易"
        report_lines = [
            f"📈 [{mode_label}报告] {self.config.exchange.upper()}_{self.config.ticker.upper()}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"⏰ 报告时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"🕐 运行时长: {self.stats.get_runtime_formatted()}",
            "",
            "【交易成果】💎",
            f"├─ 累计交易: {self.cumulative_trade_count}次",
            f"├─ 成交量(Base): {self._fmt_decimal(self.cumulative_base_volume, 4)}",
            f"├─ 成交量(Quote): ${self._fmt_decimal(self.cumulative_quote_volume, 2)}",
            f"├─ 平均频率: {self.stats.get_trades_per_hour():.1f}次/小时",
            f"└─ 平均单笔: {self._fmt_decimal(self.stats.get_avg_trade_size(), 4)}",
        ]
        
        # IOC statistics (if enabled and has data)
        if self.config.use_ioc_optimization and self.stats.ioc_attempt_count > 0:
            report_lines.extend([
                "",
                "【IOC优化】✨",
                f"├─ IOC尝试: {self.stats.ioc_attempt_count}次",
                f"├─ 完全成交: {self.stats.ioc_full_fill_count}次 ({self.stats.get_ioc_full_fill_rate():.1f}%)",
                f"├─ 部分成交: {self.stats.ioc_partial_fill_count}次",
                f"├─ 失败转Market: {self.stats.market_fallback_count}次",
                f"├─ IOC成功率: {self.stats.get_ioc_success_rate():.1f}%",
                f"└─ 平均成交率: {self.stats.get_ioc_avg_fill_rate():.1f}%",
            ])

        # Position check (anomaly detection) - 根据 boost 模式调整判断逻辑
        if self.config.boost_mode:
            # Boost 模式：应该立即平仓，持仓和平仓单都应该接近0
            position_status = "✅" if position_amt <= self.config.quantity * 2 else "⚠️ 异常"
            orders_status = "✅" if active_close_count == 0 else "⚠️ 异常"
            expected_state = "持仓和平仓单都应接近0"
        else:
            # 非 Boost 模式：会有挂单，持仓可能积累
            # 持仓不超过 max_orders * quantity 视为正常
            max_normal_position = self.config.quantity * self.config.max_orders
            position_status = "✅" if position_amt <= max_normal_position else "⚠️ 异常"
            # 活跃平仓单数量不超过 max_orders 视为正常
            orders_status = "✅" if active_close_count <= self.config.max_orders else "⚠️ 异常"
            expected_state = f"平仓单≤{self.config.max_orders}, 持仓≤{self._fmt_decimal(max_normal_position, 4)}"

        report_lines.extend([
            "",
            "【仓位检查】",
            f"├─ 当前持仓: {self._fmt_decimal(position_amt, 4)} {position_status}",
            f"├─ 活跃平仓单: {active_close_count}单 {orders_status}",
            f"├─ 预期状态: {expected_state}",
            f"└─ 总体状态: {'✅ 正常' if position_status == '✅' and orders_status == '✅' else '⚠️ 检测到异常，请关注'}",
        ])

        # Market info
        if mid_price > 0:
            report_lines.extend([
                "",
                "【市场行情】",
                f"├─ 最佳买价: ${self._fmt_decimal(best_bid, 2)}",
                f"├─ 最佳卖价: ${self._fmt_decimal(best_ask, 2)}",
                f"├─ 价差: ${self._fmt_decimal(spread, 2)} ({self._fmt_decimal(spread_pct, 3)}%)",
                f"└─ 中间价: ${self._fmt_decimal(mid_price, 2)}",
            ])

        # Note: Fees are now tracked in real-time from WebSocket fills
        # No need to query REST API before each report
        # The _query_actual_fees() method is kept as a backup for historical data

        # Cost analysis (with actual fee data from WebSocket fills)
        if self.stats.actual_total_fee > 0:
            wear_rate = self.stats.get_wear_rate(self.cumulative_quote_volume)
            avg_fee = self.stats.get_avg_fee_per_trade()

            report_lines.extend([
                "",
                "【成本分析】💰",
                f"├─ 实际手续费: ${self._fmt_decimal(self.stats.actual_total_fee, 2)}",
                f"├─ 磨损率: {self._fmt_decimal(wear_rate, 3)}% (万{int(wear_rate * 100)})",
                f"├─ 平均单笔: ${self._fmt_decimal(avg_fee, 4)}",
                f"└─ 数据来源: WebSocket实时更新",
            ])
        
        # Send report
        await self.send_notification("\n".join(report_lines))
    
    async def _meet_grid_step_condition(self) -> bool:
        if self.active_close_orders:
            picker = min if self.config.direction == "buy" else max
            next_close_order = picker(self.active_close_orders, key=lambda o: o["price"])
            next_close_price = next_close_order["price"]

            best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
            if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                raise ValueError("No bid/ask data available")

            if self.config.direction == "buy":
                new_order_close_price = best_ask * (1 + self.config.take_profit/100)
                if next_close_price / new_order_close_price > 1 + self.config.grid_step/100:
                    return True
                else:
                    return False
            elif self.config.direction == "sell":
                new_order_close_price = best_bid * (1 - self.config.take_profit/100)
                if new_order_close_price / next_close_price > 1 + self.config.grid_step/100:
                    return True
                else:
                    return False
            else:
                raise ValueError(f"Invalid direction: {self.config.direction}")
        else:
            return True

    async def _check_price_condition(self) -> bool:
        stop_trading = False
        pause_trading = False

        if self.config.pause_price == self.config.stop_price == -1:
            return stop_trading, pause_trading

        best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
        if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            raise ValueError("No bid/ask data available")

        if self.config.stop_price != -1:
            if self.config.direction == "buy":
                if best_ask >= self.config.stop_price:
                    stop_trading = True
            elif self.config.direction == "sell":
                if best_bid <= self.config.stop_price:
                    stop_trading = True

        if self.config.pause_price != -1:
            if self.config.direction == "buy":
                if best_ask >= self.config.pause_price:
                    pause_trading = True
            elif self.config.direction == "sell":
                if best_bid <= self.config.pause_price:
                    pause_trading = True

        return stop_trading, pause_trading

    @staticmethod
    def _fmt_decimal(value: Decimal, digits: int = 4) -> str:
        try:
            return f"{value:,.{digits}f}"
        except Exception:
            return str(value)

    async def send_notification(self, message: str):
        lark_token = os.getenv("LARK_TOKEN")
        if lark_token:
            async with LarkBot(lark_token) as lark_bot:
                await lark_bot.send_text(message)

        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if telegram_token and telegram_chat_id:
            with TelegramBot(telegram_token, telegram_chat_id) as tg_bot:
                tg_bot.send_text(message)

    async def run(self):
        """Main trading loop."""
        try:
            self.config.contract_id, self.config.tick_size = await self.exchange_client.get_contract_attributes()

            # Log current TradingConfig
            self.logger.log("=== Trading Configuration ===", "INFO")
            self.logger.log(f"Ticker: {self.config.ticker}", "INFO")
            self.logger.log(f"Contract ID: {self.config.contract_id}", "INFO")
            self.logger.log(f"Quantity: {self.config.quantity}", "INFO")
            self.logger.log(f"Take Profit: {self.config.take_profit}%", "INFO")
            self.logger.log(f"Direction: {self.config.direction}", "INFO")
            self.logger.log(f"Max Orders: {self.config.max_orders}", "INFO")
            self.logger.log(f"Wait Time: {self.config.wait_time}s", "INFO")
            self.logger.log(f"Exchange: {self.config.exchange}", "INFO")
            self.logger.log(f"Grid Step: {self.config.grid_step}%", "INFO")
            self.logger.log(f"Stop Price: {self.config.stop_price}", "INFO")
            self.logger.log(f"Pause Price: {self.config.pause_price}", "INFO")
            self.logger.log(f"Boost Mode: {self.config.boost_mode}", "INFO")
            self.logger.log("=============================", "INFO")

            # Capture the running event loop for thread-safe callbacks
            self.loop = asyncio.get_running_loop()

            # Pass stats to exchange client for real-time fee tracking from WebSocket
            if hasattr(self.exchange_client, 'set_stats'):
                self.exchange_client.set_stats(self.stats)
                self.logger.log("Stats object passed to exchange client for real-time fee tracking", "INFO")

            # Connect to exchange
            await self.exchange_client.connect()

            # wait for connection to establish
            await asyncio.sleep(5)

            # Main trading loop
            while not self.shutdown_requested:
                # Update active orders
                active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)

                # Filter close orders
                self.active_close_orders = []
                for order in active_orders:
                    if order.side == self.config.close_order_side:
                        self.active_close_orders.append({
                            'id': order.order_id,
                            'price': order.price,
                            'size': order.size
                        })

                # Periodic logging
                mismatch_detected = await self._log_status_periodically()

                stop_trading, pause_trading = await self._check_price_condition()
                if stop_trading:
                    msg = f"\n\nWARNING: [{self.config.exchange.upper()}_{self.config.ticker.upper()}] \n"
                    msg += "Stopped trading due to stop price triggered\n"
                    msg += "价格已经达到停止交易价格，脚本将停止交易\n"
                    await self.send_notification(msg.lstrip())
                    await self.graceful_shutdown(msg)
                    continue

                if pause_trading:
                    await asyncio.sleep(5)
                    continue

                if not mismatch_detected:
                    wait_time = self._calculate_wait_time()

                    if wait_time > 0:
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        meet_grid_step_condition = await self._meet_grid_step_condition()
                        if not meet_grid_step_condition:
                            await asyncio.sleep(1)
                            continue

                        await self._place_and_monitor_open_order()
                        self.last_close_orders += 1

        except KeyboardInterrupt:
            self.logger.log("Bot stopped by user")
            await self.graceful_shutdown("User interruption (Ctrl+C)")
        except Exception as e:
            self.logger.log(f"Critical error: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            error_message = (
                f"🚨 程序异常 | {self.config.exchange.upper()}_{self.config.ticker.upper()} "
                f"出现未捕获错误: {e}，程序将退出。"
            )
            try:
                await self.send_notification(error_message)
            except Exception as notify_err:
                self.logger.log(f"Failed to send exception notification: {notify_err}", "ERROR")
            await self.graceful_shutdown(f"Critical error: {e}")
            raise
        finally:
            # Ensure all connections are closed even if graceful shutdown fails
            try:
                await self.exchange_client.disconnect()
            except Exception as e:
                self.logger.log(f"Error disconnecting from exchange: {e}", "ERROR")
