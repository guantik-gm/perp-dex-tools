import lighter
import websockets
import asyncio
import json
import logging
import os
import sys
import time
import requests
import traceback
from decimal import Decimal
from typing import Optional, Tuple

from lighter import SignerClient, ApiClient, Configuration

from exchanges.base import OrderInfo, query_retry
from helpers.logger import log_trade_to_csv

class LighterProxy:
    def __init__(self, ticker: str, logger: logging.Logger, position_callback=None):
        # Lighter API configuration
        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX'))
        self.ticker = ticker
        self.logger = logger
        self.lighter_order_filled = False
        
        # Callback function to notify parent of position changes
        self.position_callback = position_callback
        
        # todo: 是否需要close 
        self.api_client = None
        self.lighter_client = None 
        self.lighter_client = self._initialize_lighter_client()
        self.lighter_market_index, self.base_amount_multiplier, self.price_multiplier, self.tick_size = self._get_lighter_market_config()
        self.logger.info(f"✅ Lighter market config - Market Index: {self.lighter_market_index}, "
                         f"Base Amount Multiplier: {self.base_amount_multiplier}, "
                         f"Price Multiplier: {self.price_multiplier}")
        self.stop_flag = False
        
        # Lighter order book state
        self.lighter_order_book = {"bids": {}, "asks": {}}
        self.lighter_best_bid = None
        self.lighter_best_ask = None
        self.lighter_order_book_ready = False
        self.lighter_order_book_offset = 0
        self.lighter_order_book_sequence_gap = False
        self.lighter_snapshot_loaded = False
        self.lighter_order_book_lock = asyncio.Lock()
        self.lighter_last_update_time = time.time()  # 数据新鲜度跟踪

        # Lighter WebSocket state
        self.lighter_order_result = None
        # 在trading_loop中异步初始化
        self.lighter_ws_task = None

        # Lighter order management
        self.lighter_order_status = None
        self.lighter_order_price = None
        self.lighter_order_side = None
        self.lighter_order_size = None
        self.lighter_order_start_time = None

        # Strategy state
        self.wait_start_time = None
    
    def _initialize_lighter_client(self):
        """Initialize the Lighter client."""
        if self.lighter_client is None:
            api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
            if not api_key_private_key:
                raise Exception("API_KEY_PRIVATE_KEY environment variable not set")

            self.lighter_client = SignerClient(
                url=self.lighter_base_url,
                private_key=api_key_private_key,
                account_index=self.account_index,
                api_key_index=self.api_key_index,
            )

            # Check client
            err = self.lighter_client.check_client()
            if err is not None:
                raise Exception(f"CheckClient error: {err}")

            self.logger.info("✅ Lighter client initialized successfully")
        return self.lighter_client

    def _get_lighter_market_config(self) -> Tuple[int, int, int, Decimal]:
        """Get Lighter market configuration."""
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        headers = {"accept": "application/json"}

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            if not response.text.strip():
                raise Exception("Empty response from Lighter API")

            data = response.json()

            if "order_books" not in data:
                raise Exception("Unexpected response format")

            for market in data["order_books"]:
                if market["symbol"] == self.ticker:
                    price_multiplier = pow(10, market["supported_price_decimals"])
                    return (market["market_id"], 
                           pow(10, market["supported_size_decimals"]), 
                           price_multiplier,
                           Decimal("1") / (Decimal("10") ** market["supported_price_decimals"])
                           )

            raise Exception(f"Ticker {self.ticker} not found")

        except Exception as e:
            self.logger.error(f"⚠️ Error getting market config: {e}")
            raise

    async def setup_ws_task(self):
        self.api_client = ApiClient(Configuration(host=self.lighter_base_url))
        self.lighter_ws_task = asyncio.create_task(self.handle_lighter_ws())
        self.logger.info("✅ Lighter WebSocket task started")
        await self.wait_for_lighter_order_book_ready()
            
    async def reset_lighter_order_book(self):
        """Reset Lighter order book state."""
        async with self.lighter_order_book_lock:
            self.lighter_order_book["bids"].clear()
            self.lighter_order_book["asks"].clear()
            self.lighter_order_book_offset = 0
            self.lighter_order_book_sequence_gap = False
            self.lighter_snapshot_loaded = False
            self.lighter_best_bid = None
            self.lighter_best_ask = None
            self.lighter_last_update_time = time.time()  # 重置时间戳

    def update_lighter_order_book(self, side: str, levels: list):
        """Update Lighter order book with new levels."""
        for level in levels:
            # Handle different data structures - could be list [price, size] or dict {"price": ..., "size": ...}
            if isinstance(level, list) and len(level) >= 2:
                price = Decimal(level[0])
                size = Decimal(level[1])
            elif isinstance(level, dict):
                price = Decimal(level.get("price", 0))
                size = Decimal(level.get("size", 0))
            else:
                self.logger.warning(f"⚠️ Unexpected level format: {level}")
                continue

            if size > 0:
                self.lighter_order_book[side][price] = size
            else:
                # Remove zero size orders
                self.lighter_order_book[side].pop(price, None)

    def validate_order_book_offset(self, new_offset: int) -> bool:
        """Validate order book offset sequence."""
        if new_offset <= self.lighter_order_book_offset:
            self.logger.warning(
                f"⚠️ Out-of-order update: new_offset={new_offset}, current_offset={self.lighter_order_book_offset}")
            return False
        return True

    def validate_order_book_integrity(self) -> bool:
        """Validate order book integrity."""
        # Check for negative prices or sizes
        for side in ["bids", "asks"]:
            for price, size in self.lighter_order_book[side].items():
                if price <= 0 or size <= 0:
                    self.logger.error(f"❌ Invalid order book data: {side} price={price}, size={size}")
                    return False
        return True

    def get_lighter_best_levels(self) -> Tuple[Tuple[Decimal, Decimal], Tuple[Decimal, Decimal]]:
        """Get best bid and ask levels from Lighter order book."""
        best_bid = None
        best_ask = None

        if self.lighter_order_book["bids"]:
            best_bid_price = max(self.lighter_order_book["bids"].keys())
            best_bid_size = self.lighter_order_book["bids"][best_bid_price]
            best_bid = (best_bid_price, best_bid_size)

        if self.lighter_order_book["asks"]:
            best_ask_price = min(self.lighter_order_book["asks"].keys())
            best_ask_size = self.lighter_order_book["asks"][best_ask_price]
            best_ask = (best_ask_price, best_ask_size)

        return best_bid, best_ask

    def get_order_book_levels(self, side: str, limit: int = 10):
        """
        获取指定方向的订单簿档位
        
        Args:
            side: 'bids' | 'asks'
            limit: 返回档位数量限制
            
        Returns:
            按价格排序的档位列表: [{'price': Decimal, 'size': Decimal}, ...]
        """
        if side not in ['bids', 'asks']:
            raise ValueError(f"Invalid side: {side}. Must be 'bids' or 'asks'")
            
        levels = self.lighter_order_book[side]
        
        if not levels:
            return []
        
        if side == 'bids':
            # 买单从高到低排序
            sorted_items = sorted(levels.items(), reverse=True)
        else:
            # 卖单从低到高排序
            sorted_items = sorted(levels.items())
        
        return [
            {'price': price, 'size': size} 
            for price, size in sorted_items[:limit]
        ]
    
    def get_order_book_depth_data(self, limit: int = 10) -> dict:
        """
        获取完整订单簿深度数据，与EdgeX格式保持一致
        
        Args:
            limit: 深度档位限制
            
        Returns:
            {
                'bids': [{'price': Decimal, 'size': Decimal}, ...],
                'asks': [{'price': Decimal, 'size': Decimal}, ...],
                'timestamp': int
            }
        """
        return {
            'bids': self.get_order_book_levels('bids', limit),
            'asks': self.get_order_book_levels('asks', limit),
            'timestamp': int(self.lighter_last_update_time * 1000)
        }

    def calculate_execution_price(self, side: str, quantity: Decimal) -> Decimal:
        """
        基于订单簿深度计算指定数量的taker执行成交量加权平均价格
        
        Args:
            side: 'buy' | 'sell' - 交易方向
            quantity: 交易数量
            
        Returns:
            Decimal: 成交量加权平均价格
            
        Raises:
            ValueError: 当订单簿流动性不足时
        """
        if side not in ['buy', 'sell']:
            raise ValueError(f"Invalid side: {side}. Must be 'buy' or 'sell'")
        
        if quantity <= 0:
            raise ValueError(f"Invalid quantity: {quantity}. Must be greater than 0")
        
        # 获取对应方向的订单簿数据
        if side == 'buy':
            # 买单需要消化卖方订单簿（asks）
            levels = self.get_order_book_levels('asks', limit=50)  # 获取更多档位确保流动性
        else:
            # 卖单需要消化买方订单簿（bids）
            levels = self.get_order_book_levels('bids', limit=50)
        
        if not levels:
            raise ValueError(f"No order book data available for side: {side}")
        
        # 吃单逻辑：按价格优先级逐档消化
        remaining_quantity = quantity
        total_cost = Decimal('0')
        total_filled = Decimal('0')
        
        for level in levels:
            if remaining_quantity <= 0:
                break
                
            level_price = level['price']
            level_size = level['size']
            
            # 计算这一档能成交的数量
            fill_quantity = min(remaining_quantity, level_size)
            
            # 累计成本和成交量
            total_cost += fill_quantity * level_price
            total_filled += fill_quantity
            remaining_quantity -= fill_quantity
            
            if self.logger:
                self.logger.debug(f"💱 吃单档位 - 价格: {level_price:.6f}, "
                                f"档位量: {level_size:.6f}, "
                                f"成交量: {fill_quantity:.6f}, "
                                f"剩余: {remaining_quantity:.6f}")
        
        # 检查是否有足够的流动性
        if remaining_quantity > 0:
            filled_ratio = (total_filled / quantity) * 100
            if self.logger:
                self.logger.warning(f"⚠️ 订单簿流动性不足 - 需要: {quantity:.6f}, "
                                  f"可成交: {total_filled:.6f} ({filled_ratio:.1f}%)")
            
            # 如果成交比例太低，抛出异常
            if filled_ratio < 80:  # 至少要能成交80%
                raise ValueError(f"Insufficient liquidity: only {filled_ratio:.1f}% can be filled")
        
        # 计算成交量加权平均价格
        if total_filled > 0:
            weighted_avg_price = total_cost / total_filled
            
            if self.logger:
                best_price = levels[0]['price'] if levels else Decimal('0')
                price_impact = abs(weighted_avg_price - best_price) / best_price * 100 if best_price > 0 else 0
                self.logger.info(f"🎯 {side.upper()}单执行价格计算完成 - "
                               f"数量: {total_filled:.6f}, "
                               f"加权均价: {weighted_avg_price:.6f}, "
                               f"最优价: {best_price:.6f}, "
                               f"价格冲击: {price_impact:.2f}%")
            
            # 返回精度为6位小数的价格
            return weighted_avg_price.quantize(Decimal('0.000001'))
        else:
            raise ValueError("No quantity could be filled")

    def get_lighter_mid_price(self) -> Decimal:
        """Get mid price from Lighter order book."""
        best_bid, best_ask = self.get_lighter_best_levels()

        if best_bid is None or best_ask is None:
            raise Exception("Cannot calculate mid price - missing order book data")

        mid_price = (best_bid[0] + best_ask[0]) / Decimal('2')
        return mid_price

    def get_lighter_order_price(self, is_ask: bool) -> Decimal:
        """Get order price from Lighter order book."""
        best_bid, best_ask = self.get_lighter_best_levels()

        if best_bid is None or best_ask is None:
            raise Exception("Cannot calculate order price - missing order book data")

        if is_ask:
            order_price = best_bid[0] + self.tick_size
        else:
            order_price = best_ask[0] - self.tick_size

        return order_price

    def calculate_adjusted_price(self, original_price: Decimal, side: str, adjustment_percent: Decimal) -> Decimal:
        """Calculate adjusted price for order modification."""
        adjustment = original_price * adjustment_percent

        if side.lower() == 'buy':
            # For buy orders, increase price to improve fill probability
            return original_price + adjustment
        else:
            # For sell orders, decrease price to improve fill probability
            return original_price - adjustment

    async def request_fresh_snapshot(self, ws):
        """Request fresh order book snapshot."""
        await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))

    def handle_lighter_order_result(self, order_data):
        """Handle Lighter order result from WebSocket."""
        try:
            order_data["avg_filled_price"] = (Decimal(order_data["filled_quote_amount"]) /
                                              Decimal(order_data["filled_base_amount"]))
            
            # Calculate position change
            if order_data["is_ask"]:
                order_data["side"] = "SHORT"
                order_type = "OPEN"
                position_change = -Decimal(order_data["filled_base_amount"])
            else:
                order_data["side"] = "LONG"
                order_type = "CLOSE"
                position_change = +Decimal(order_data["filled_base_amount"])

            # Notify parent of position change through callback
            if self.position_callback:
                self.position_callback(position_change, order_data["avg_filled_price"], Decimal(order_data["filled_base_amount"]))

            client_order_index = order_data["client_order_id"]

            self.logger.info(f"[WebSocket] [{client_order_index}] [{order_type}] [Lighter] [FILLED]: "
                             f"{order_data['filled_base_amount']} @ {order_data['avg_filled_price']}")

            # Log Lighter trade to CSV
            log_trade_to_csv(
                exchange='Lighter',
                ticker=self.ticker,
                side=order_data['side'],
                price=str(order_data['avg_filled_price']),
                quantity=str(order_data['filled_base_amount'])
            )

            # Mark execution as complete
            self.lighter_order_filled = True  # Mark order as filled
            # self.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")

    async def handle_lighter_ws(self):
        """Handle Lighter WebSocket connection and messages."""
        url = "wss://mainnet.zklighter.elliot.ai/stream"
        cleanup_counter = 0

        while not self.stop_flag:
            timeout_count = 0
            try:
                # Reset order book state before connecting
                await self.reset_lighter_order_book()

                async with websockets.connect(url) as ws:
                    # Subscribe to order book updates
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))

                    # Subscribe to account orders updates
                    account_orders_channel = f"account_orders/{self.lighter_market_index}/{self.account_index}"

                    # Get auth token for the subscription
                    try:
                        # Set auth token to expire in 10 minutes
                        ten_minutes_deadline = int(time.time() + 10 * 60)
                        auth_token, err = self.lighter_client.create_auth_token_with_expiry(ten_minutes_deadline)
                        if err is not None:
                            self.logger.warning(f"⚠️ Failed to create auth token for account orders subscription: {err}")
                        else:
                            auth_message = {
                                "type": "subscribe",
                                "channel": account_orders_channel,
                                "auth": auth_token
                            }
                            await ws.send(json.dumps(auth_message))
                            self.logger.info("✅ Subscribed to account orders with auth token (expires in 10 minutes)")
                    except Exception as e:
                        self.logger.warning(f"⚠️ Error creating auth token for account orders subscription: {e}")

                    while not self.stop_flag:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1)

                            try:
                                data = json.loads(msg)
                            except json.JSONDecodeError as e:
                                self.logger.warning(f"⚠️ JSON parsing error in Lighter websocket: {e}")
                                continue

                            # Reset timeout counter on successful message
                            timeout_count = 0

                            async with self.lighter_order_book_lock:
                                if data.get("type") == "subscribed/order_book":
                                    # Initial snapshot - clear and populate the order book
                                    self.lighter_order_book["bids"].clear()
                                    self.lighter_order_book["asks"].clear()

                                    # Handle the initial snapshot
                                    order_book = data.get("order_book", {})
                                    if order_book and "offset" in order_book:
                                        self.lighter_order_book_offset = order_book["offset"]
                                        self.logger.info(f"✅ Initial order book offset set to: {self.lighter_order_book_offset}")

                                    # Debug: Log the structure of bids and asks
                                    bids = order_book.get("bids", [])
                                    asks = order_book.get("asks", [])
                                    if bids:
                                        self.logger.debug(f"📊 Sample bid structure: {bids[0] if bids else 'None'}")
                                    if asks:
                                        self.logger.debug(f"📊 Sample ask structure: {asks[0] if asks else 'None'}")

                                    self.update_lighter_order_book("bids", bids)
                                    self.update_lighter_order_book("asks", asks)
                                    self.lighter_snapshot_loaded = True
                                    self.lighter_order_book_ready = True
                                    self.lighter_last_update_time = time.time()  # 更新时间戳

                                    self.logger.info(f"✅ Lighter order book snapshot loaded with "
                                                     f"{len(self.lighter_order_book['bids'])} bids and "
                                                     f"{len(self.lighter_order_book['asks'])} asks")

                                elif data.get("type") == "update/order_book" and self.lighter_snapshot_loaded:
                                    # Extract offset from the message
                                    order_book = data.get("order_book", {})
                                    if not order_book or "offset" not in order_book:
                                        self.logger.warning("⚠️ Order book update missing offset, skipping")
                                        continue

                                    new_offset = order_book["offset"]

                                    # Validate offset sequence
                                    if not self.validate_order_book_offset(new_offset):
                                        self.lighter_order_book_sequence_gap = True
                                        break

                                    # Update the order book with new data
                                    self.update_lighter_order_book("bids", order_book.get("bids", []))
                                    self.update_lighter_order_book("asks", order_book.get("asks", []))
                                    self.lighter_last_update_time = time.time()  # 更新时间戳

                                    # Validate order book integrity after update
                                    if not self.validate_order_book_integrity():
                                        self.logger.warning("🔄 Order book integrity check failed, requesting fresh snapshot...")
                                        break

                                    # Get the best bid and ask levels
                                    best_bid, best_ask = self.get_lighter_best_levels()

                                    # Update global variables
                                    if best_bid is not None:
                                        self.lighter_best_bid = best_bid[0]
                                    if best_ask is not None:
                                        self.lighter_best_ask = best_ask[0]

                                elif data.get("type") == "ping":
                                    # Respond to ping with pong
                                    await ws.send(json.dumps({"type": "pong"}))
                                elif data.get("type") == "update/account_orders":
                                    # Handle account orders updates
                                    orders = data.get("orders", {}).get(str(self.lighter_market_index), [])
                                    for order in orders:
                                        if order.get("status") == "filled":
                                            self.handle_lighter_order_result(order)
                                elif data.get("type") == "update/order_book" and not self.lighter_snapshot_loaded:
                                    # Ignore updates until we have the initial snapshot
                                    continue

                            # Periodic cleanup outside the lock
                            cleanup_counter += 1
                            if cleanup_counter >= 1000:
                                cleanup_counter = 0

                            # Handle sequence gap and integrity issues outside the lock
                            if self.lighter_order_book_sequence_gap:
                                try:
                                    await self.request_fresh_snapshot(ws)
                                    self.lighter_order_book_sequence_gap = False
                                except Exception as e:
                                    self.logger.error(f"⚠️ Failed to request fresh snapshot: {e}")
                                    break

                        except asyncio.TimeoutError:
                            timeout_count += 1
                            data_age = time.time() - self.lighter_last_update_time
                            
                            if timeout_count % 3 == 0:
                                self.logger.warning(f"⏰ No message from Lighter websocket for {timeout_count}s, "
                                                   f"order book数据已{data_age:.1f}s未更新")
                            
                            # 数据超过10秒未更新时主动重连
                            if data_age >= 3:
                                self.logger.warning(f"🔄 Order book数据过时({data_age:.1f}s)，主动重连以刷新数据")
                                self.lighter_order_book_ready = False
                                break  # 跳出内层循环，触发重连
                            continue
                        except websockets.exceptions.ConnectionClosed as e:
                            self.logger.warning(f"⚠️ Lighter websocket connection closed: {e}")
                            break
                        except websockets.exceptions.WebSocketException as e:
                            self.logger.warning(f"⚠️ Lighter websocket error: {e}")
                            break
                        except Exception as e:
                            self.logger.error(f"⚠️ Error in Lighter websocket: {e}")
                            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
                            break
            except Exception as e:
                self.logger.error(f"⚠️ Failed to connect to Lighter websocket: {e}")

            # Wait a bit before reconnecting
            await asyncio.sleep(2)

    async def wait_for_lighter_order_book_ready(self, timeout: int = 10):
        try:
            # Wait for initial Lighter order book data with timeout
            self.logger.info("⏳ Waiting for initial Lighter order book data...")
            timeout = 10  # seconds
            start_time = time.time()
            while not self.lighter_order_book_ready and not self.stop_flag:
                if time.time() - start_time > timeout:
                    self.logger.warning(f"⚠️ Timeout waiting for Lighter WebSocket order book data after {timeout}s")
                    break
                await asyncio.sleep(0.5)

            if self.lighter_order_book_ready:
                self.logger.info("✅ Lighter WebSocket order book data received")
            else:
                self.logger.warning("⚠️ Lighter WebSocket order book not ready")

        except Exception as e:
            self.logger.error(f"❌ Failed to setup Lighter websocket: {e}")
            sys.exit(1)

    async def place_lighter_market_order(self, lighter_side: str, quantity: Decimal, price: Decimal):
        if not self.lighter_client:
            self._initialize_lighter_client()

        best_bid, best_ask = self.get_lighter_best_levels()
        
        if best_bid is None or best_ask is None:
            self.logger.error(f"❌ Error placing Lighter order, best_bid: {best_bid} or best_ask: {best_ask} is None")
            return None

        # Determine order parameters
        if lighter_side.lower() == 'buy':
            order_type = "CLOSE"
            is_ask = False
            price = best_ask[0] * Decimal('1.002')
        else:
            order_type = "OPEN"
            is_ask = True
            price = best_bid[0] * Decimal('0.998')

        # Reset order state
        self.lighter_order_filled = False
        self.lighter_order_price = price
        self.lighter_order_side = lighter_side
        self.lighter_order_size = quantity

        try:
            client_order_index = int(time.time() * 1000)
            # Sign the order transaction
            tx_info, error = self.lighter_client.sign_create_order(
                market_index=self.lighter_market_index,
                client_order_index=client_order_index,
                base_amount=int(quantity * self.base_amount_multiplier),
                price=int(price * self.price_multiplier),
                is_ask=is_ask,
                order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=False,
                trigger_price=0,
            )
            if error is not None:
                raise Exception(f"Sign error: {error}")

            # Prepare the form data
            tx_hash = await self.lighter_client.send_tx(
                tx_type=self.lighter_client.TX_TYPE_CREATE_ORDER,
                tx_info=tx_info
            )

            self.logger.info(f"[{client_order_index}] [{order_type}] [Lighter] [OPEN]: {quantity}")

            await self.monitor_lighter_order(client_order_index)

            return tx_hash
        except Exception as e:
            self.logger.error(f"❌ Error placing Lighter order: {e}")
            return None

    async def monitor_lighter_order(self, client_order_index: int):
        """Monitor Lighter order and adjust price if needed."""

        start_time = time.time()
        while not self.lighter_order_filled and not self.stop_flag:
            # Check for timeout (30 seconds total)
            if time.time() - start_time > 30:
                self.logger.error(f"❌ Timeout waiting for Lighter order fill after {time.time() - start_time:.1f}s")
                self.logger.error(f"❌ Order state - Filled: {self.lighter_order_filled}")

                # Fallback: Mark as filled to continue trading
                self.logger.warning("⚠️ Using fallback - Fetching order info from account position")
                if self.position_callback:
                    order_info = await self.get_filled_order_info(client_order_index)
                    if order_info:
                        self.logger.info(f"get order info success: {order_info}")
                        self.position_callback(order_info.size, order_info.price, order_info.size)
                    else:
                        self.logger.warning(f"still failed to get order info, set -1 for lighter order and marking order as filled to continue trading")
                        self.position_callback(0, -1, -1)
                self.lighter_order_filled = True
                # self.waiting_for_lighter_fill = False
                # self.order_execution_complete = True
                break

            await asyncio.sleep(0.1)  # Check every 100ms

    async def modify_lighter_order(self, client_order_index: int, new_price: Decimal):
        """Modify current Lighter order with new price using client_order_index."""
        try:
            if client_order_index is None:
                self.logger.error("❌ Cannot modify order - no order ID available")
                return

            # Calculate new Lighter price
            lighter_price = int(new_price * self.price_multiplier)

            self.logger.info(f"🔧 Attempting to modify order - Market: {self.lighter_market_index}, "
                             f"Client Order Index: {client_order_index}, New Price: {lighter_price}")

            # Use the native SignerClient's modify_order method
            tx_info, tx_hash, error = await self.lighter_client.modify_order(
                market_index=self.lighter_market_index,
                order_index=client_order_index,  # Use client_order_index directly
                base_amount=int(self.lighter_order_size * self.base_amount_multiplier),
                price=lighter_price,
                trigger_price=0
            )

            if error is not None:
                self.logger.error(f"❌ Lighter order modification error: {error}")
                return

            self.lighter_order_price = new_price
            self.logger.info(f"🔄 Lighter order modified successfully: {self.lighter_order_side} "
                             f"{self.lighter_order_size} @ {new_price}")

        except Exception as e:
            self.logger.error(f"❌ Error modifying Lighter order: {e}")
            import traceback
            self.logger.error(f"❌ Full traceback: {traceback.format_exc()}")

    async def fetch_bbo_prices(self) -> Tuple[Decimal, Decimal]:
        """获取最优买卖价 - 适配SmartHedgeStrategy"""
        try:
            best_bid, best_ask = self.get_lighter_best_levels()
            
            if best_bid is None or best_ask is None:
                raise Exception("无法获取Lighter最优价格 - 订单簿数据缺失")
            
            return best_bid[0], best_ask[0]  # 返回价格部分，忽略数量
        except Exception as e:
            if hasattr(self, 'logger') and self.logger:
                self.logger.error(f"获取Lighter最优价格失败: {e}")
            raise

    @query_retry(reraise=True)
    async def get_ticker_position(self):
        """Get positions using official SDK."""
        # 接口文档：https://apidocs.lighter.xyz/reference/account-1
        # Use shared API client
        account_api = lighter.AccountApi(self.api_client)

        # Get account info
        account_data = await account_api.account(by="index", value=str(self.account_index))

        if not account_data or not account_data.accounts:
            self.logger.log("Failed to get positions", "ERROR")
            raise ValueError("Failed to get positions")
        position = None
        positions = account_data.accounts[0].positions
        for p in positions:
            if p.market_id == self.lighter_market_index:
                position = p
                break
        if position is None:
            self.logger.log(f"No position found for market {self.lighter_market_index}", "INFO")
        return position
    
    async def get_ticker_position_liquidation_price(self) -> Decimal:
        """获取指定合约的强平价"""
        position = await self.get_ticker_position()
        if position is None:
            raise ValueError("No position found for liquidation price calculation")
        return Decimal(position.liquidation_price)
    
    async def get_ticker_position_pnl(self) -> Decimal:
        position = await self.get_ticker_position()
        if position is None:
            raise ValueError("No position found for position PnL")
        # unrealized_pnl, realized_pnl
        return Decimal(position.realized_pnl)
    
    async def get_ticker_position_value(self) -> Decimal:
        position = await self.get_ticker_position()
        if position is None:
            raise ValueError("No position found for position value")
        # unrealized_pnl, realized_pnl
        return Decimal(position.position_value)
        
    async def get_filled_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information from Lighter using official SDK."""
        try:
            position = self.get_ticker_position()
            # Look for the specific order in account positions
            position_amt = abs(float(position.position_value))
            if position_amt > 0.001:  # Only include significant positions
                return OrderInfo(
                    order_id=order_id,
                    side="buy" if float(position.position) > 0 else "sell",
                    size=Decimal(str(position.position)),
                    price=Decimal(str(position.avg_entry_price)),
                    status="FILLED",  # Positions are filled orders
                    filled_size=Decimal(position.position),
                    remaining_size=Decimal('0')
                )

            return None

        except Exception as e:
            self.logger.log(f"Error getting order info: {e}", "ERROR")
            return None