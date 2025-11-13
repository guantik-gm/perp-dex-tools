"""
EdgeX exchange client implementation.
"""

import os
import asyncio
import json
import time
import traceback
import websockets
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from edgex_sdk import Client, OrderSide, WebSocketManager, CancelOrderParams, GetOrderBookDepthParams, GetActiveOrderParams

from .base import BaseExchangeClient, OrderResult, OrderInfo, query_retry
from helpers.logger import TradingLogger


class EdgeXClient(BaseExchangeClient):
    """EdgeX exchange client implementation."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize EdgeX client."""
        super().__init__(config)

        # EdgeX credentials from environment
        self.account_id = os.getenv('EDGEX_ACCOUNT_ID')
        self.stark_private_key = os.getenv('EDGEX_STARK_PRIVATE_KEY')
        self.base_url = os.getenv('EDGEX_BASE_URL', 'https://pro.edgex.exchange')
        self.ws_url = os.getenv('EDGEX_WS_URL', 'wss://quote.edgex.exchange')

        if not self.account_id or not self.stark_private_key:
            raise ValueError("EDGEX_ACCOUNT_ID and EDGEX_STARK_PRIVATE_KEY must be set in environment variables")

        # Initialize EdgeX client using official SDK
        self.client = Client(
            base_url=self.base_url,
            account_id=int(self.account_id),
            stark_private_key=self.stark_private_key
        )

        # Initialize WebSocket manager using official SDK
        self.ws_manager = WebSocketManager(
            base_url=self.ws_url,
            account_id=int(self.account_id),
            stark_pri_key=self.stark_private_key
        )

        # Initialize logger
        self.logger = TradingLogger(exchange="edgex", ticker=self.config.ticker, log_to_console=False)

        self._order_update_handler = None

        # --- reconnection state ---
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_stop = asyncio.Event()
        self._ws_disconnected = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # --- order book state (WebSocket) ---
        self.order_book = {"bids": {}, "asks": {}}
        self.best_bid = None
        self.best_ask = None
        self.order_book_ready = False
        self.order_book_start_version = None
        self.order_book_end_version = None
        self.order_book_lock = asyncio.Lock()
        self.last_order_book_update_time = time.time()
        self._public_ws_task: Optional[asyncio.Task] = None
        self._public_ws_stop = asyncio.Event()
        self._public_ws_disconnected = asyncio.Event()

    def _validate_config(self) -> None:
        """Validate EdgeX configuration."""
        required_env_vars = ['EDGEX_ACCOUNT_ID', 'EDGEX_STARK_PRIVATE_KEY']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {missing_vars}")

    # ---------------------------
    # Connection / Reconnect
    # ---------------------------

    async def connect(self) -> None:
        """Connect private WS and keep it alive with auto-reconnect."""
        self._loop = asyncio.get_running_loop()

        # Hook disconnect/connect once (SDK calls these from threads)
        try:
            private_client = self.ws_manager.get_private_client()
            private_client.on_disconnect(
                lambda exc: self._loop.call_soon_threadsafe(self._ws_disconnected.set)
            )
            private_client.on_connect(
                lambda: self.logger.log("[WS] private connected", "INFO")
            )
        except Exception as e:
            self.logger.log(f"[WS] failed to set hooks: {e}", "ERROR")

        if not self._ws_task or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._run_private_ws())

        # Start public WebSocket for order book data
        if not self._public_ws_task or self._public_ws_task.done():
            self._public_ws_task = asyncio.create_task(self._run_public_ws())

        # give first connection a moment (optional)
        await asyncio.sleep(0.5)

    async def _run_private_ws(self):
        """Tiny reconnect loop with exponential backoff."""
        backoff = 1.0
        while not self._ws_stop.is_set():
            try:
                # connect
                self.ws_manager.connect_private()
                self.logger.log("[WS] connected", "INFO")
                backoff = 1.0

                # wait until either disconnect or stop
                self._ws_disconnected.clear()
                done, _ = await asyncio.wait(
                    {asyncio.create_task(self._ws_stop.wait()),
                    asyncio.create_task(self._ws_disconnected.wait()),},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._ws_stop.is_set():
                    break

                self.logger.log(
                    "[WS] disconnected; attempting to reconnect…", "WARNING"
                )
            except Exception as e:
                self.logger.log(f"[WS] connect error: {e}", "ERROR")
            finally:
                # ensure socket is closed before retry
                try:
                    self.ws_manager.disconnect_private()
                except Exception:
                    pass

            # backoff and retry
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

        # Final cleanup (on stop)
        try:
            self.ws_manager.disconnect_private()
        except Exception:
            pass

    async def _run_public_ws(self):
        """Manage public WebSocket connection for order book data with auto-reconnect."""
        # Wait for contract_id to be initialized
        while not self.config.contract_id and not self._public_ws_stop.is_set():
            await asyncio.sleep(0.5)

        if self._public_ws_stop.is_set():
            return

        self.logger.log(f"[Public WS] Starting with contract_id: {self.config.contract_id}", "INFO")

        backoff = 1.0
        while not self._public_ws_stop.is_set():
            try:
                # Connect to public WebSocket
                async with websockets.connect(self.ws_url) as ws:
                    self.logger.log("[Public WS] Connected to order book stream", "INFO")
                    backoff = 1.0

                    # Subscribe to order book depth
                    # Use 200 levels for better depth analysis
                    channel = f"depth.{self.config.contract_id}.200"
                    subscribe_msg = {
                        "type": "subscribe",
                        "channel": channel
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    self.logger.log(f"[Public WS] Subscribed to {channel}", "INFO")

                    # Message processing loop
                    while not self._public_ws_stop.is_set():
                        try:
                            # Set a timeout to periodically check stop flag
                            message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            await self._handle_order_book_message(message)
                        except asyncio.TimeoutError:
                            # Check for data freshness
                            data_age = time.time() - self.last_order_book_update_time
                            if data_age >= 10:
                                self.logger.log(f"[Public WS] Order book data stale ({data_age:.1f}s), reconnecting", "WARNING")
                                self.order_book_ready = False
                                break
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            self.logger.log("[Public WS] Connection closed, reconnecting", "WARNING")
                            self.order_book_ready = False
                            break

            except Exception as e:
                self.logger.log(f"[Public WS] Connection error: {e}", "ERROR")
                self.order_book_ready = False

            # Exponential backoff before reconnecting
            if not self._public_ws_stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def disconnect(self) -> None:
        """Disconnect from EdgeX."""
        try:
            self._ws_stop.set()
            self._public_ws_stop.set()

            if self._ws_task:
                await self._ws_task
            if self._public_ws_task:
                await self._public_ws_task
        except Exception:
            pass

        try:
            if hasattr(self, "client") and self.client:
                await self.client.close()
            if hasattr(self, "ws_manager"):
                self.ws_manager.disconnect_all()
        except Exception as e:
            self.logger.log(f"Error during EdgeX disconnect: {e}", "ERROR")

    # ---------------------------
    # Utility / Name
    # ---------------------------

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "edgex"

    # ---------------------------
    # Order Book Management
    # ---------------------------

    async def _handle_order_book_message(self, message: str):
        """Handle order book messages from public WebSocket."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            # Handle subscription confirmation
            if msg_type == "subscribed":
                channel = data.get("channel", "")
                self.logger.log(f"[Public WS] Subscription confirmed: {channel}", "INFO")
                return

            # Handle order book snapshot (initial full data)
            if msg_type == "depth" and data.get("depthType") == "SNAPSHOT":
                await self._handle_order_book_snapshot(data)

            # Handle order book updates (incremental changes)
            elif msg_type == "depth" and data.get("depthType") == "CHANGED":
                await self._handle_order_book_update(data)

        except json.JSONDecodeError as e:
            self.logger.log(f"[Public WS] Failed to parse message: {e}", "ERROR")
        except Exception as e:
            self.logger.log(f"[Public WS] Error handling message: {e}", "ERROR")

    async def _handle_order_book_snapshot(self, data: Dict[str, Any]):
        """Handle initial order book snapshot."""
        try:
            async with self.order_book_lock:
                # Clear existing order book
                self.order_book["bids"].clear()
                self.order_book["asks"].clear()

                # Extract version info
                self.order_book_start_version = data.get("startVersion")
                self.order_book_end_version = data.get("endVersion")

                # Process bids and asks
                bids = data.get("bids", [])
                asks = data.get("asks", [])

                for bid in bids:
                    if isinstance(bid, list) and len(bid) >= 2:
                        price = Decimal(str(bid[0]))
                        size = Decimal(str(bid[1]))
                        if size > 0:
                            self.order_book["bids"][price] = size

                for ask in asks:
                    if isinstance(ask, list) and len(ask) >= 2:
                        price = Decimal(str(ask[0]))
                        size = Decimal(str(ask[1]))
                        if size > 0:
                            self.order_book["asks"][price] = size

                # Update best bid/ask
                self._update_best_prices()

                # Mark order book as ready
                self.order_book_ready = True
                self.last_order_book_update_time = time.time()

                self.logger.log(
                    f"[Public WS] Order book snapshot loaded: {len(self.order_book['bids'])} bids, "
                    f"{len(self.order_book['asks'])} asks, version: {self.order_book_end_version}",
                    "INFO"
                )

        except Exception as e:
            self.logger.log(f"[Public WS] Error processing snapshot: {e}", "ERROR")
            self.order_book_ready = False

    async def _handle_order_book_update(self, data: Dict[str, Any]):
        """Handle incremental order book updates."""
        try:
            async with self.order_book_lock:
                # Validate version sequence
                new_start_version = data.get("startVersion")
                new_end_version = data.get("endVersion")

                if self.order_book_end_version is not None and new_start_version != self.order_book_end_version:
                    self.logger.log(
                        f"[Public WS] Version gap detected: expected {self.order_book_end_version}, "
                        f"got {new_start_version}. Waiting for snapshot...",
                        "WARNING"
                    )
                    self.order_book_ready = False
                    return

                # Process bid updates
                bids = data.get("bids", [])
                for bid in bids:
                    if isinstance(bid, list) and len(bid) >= 2:
                        price = Decimal(str(bid[0]))
                        size = Decimal(str(bid[1]))
                        if size > 0:
                            self.order_book["bids"][price] = size
                        else:
                            # Remove price level if size is 0
                            self.order_book["bids"].pop(price, None)

                # Process ask updates
                asks = data.get("asks", [])
                for ask in asks:
                    if isinstance(ask, list) and len(ask) >= 2:
                        price = Decimal(str(ask[0]))
                        size = Decimal(str(ask[1]))
                        if size > 0:
                            self.order_book["asks"][price] = size
                        else:
                            # Remove price level if size is 0
                            self.order_book["asks"].pop(price, None)

                # Update version tracking
                self.order_book_end_version = new_end_version

                # Update best bid/ask
                self._update_best_prices()

                # Update timestamp
                self.last_order_book_update_time = time.time()

        except Exception as e:
            self.logger.log(f"[Public WS] Error processing update: {e}", "ERROR")

    def _update_best_prices(self):
        """Update best bid and ask prices from order book."""
        try:
            if self.order_book["bids"]:
                self.best_bid = max(self.order_book["bids"].keys())
            else:
                self.best_bid = None

            if self.order_book["asks"]:
                self.best_ask = min(self.order_book["asks"].keys())
            else:
                self.best_ask = None
        except Exception as e:
            self.logger.log(f"Error updating best prices: {e}", "ERROR")

    def get_order_book_levels(self, side: str, limit: int = 50) -> List[Dict[str, Decimal]]:
        """
        Get order book levels for a specific side.

        Args:
            side: 'bids' or 'asks'
            limit: Maximum number of levels to return

        Returns:
            List of {price, size} dictionaries, sorted by best price first
        """
        if not self.order_book_ready:
            return []

        try:
            if side not in ['bids', 'asks']:
                self.logger.log(f"Invalid side: {side}", "ERROR")
                return []

            # Get price levels sorted by best price first
            prices = sorted(
                self.order_book[side].keys(),
                reverse=(side == 'bids')  # Descending for bids, ascending for asks
            )

            # Build result list
            levels = []
            for price in prices[:limit]:
                levels.append({
                    'price': price,
                    'size': self.order_book[side][price]
                })

            return levels

        except Exception as e:
            self.logger.log(f"Error getting order book levels: {e}", "ERROR")
            return []

    def get_best_prices(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Get best bid and ask prices from WebSocket order book."""
        if not self.order_book_ready:
            return None, None
        return self.best_bid, self.best_ask

    def calculate_execution_price(self, side: str, quantity: Decimal) -> Optional[Decimal]:
        """
        Calculate the volume-weighted average execution price for a taker order.

        Args:
            side: 'buy' or 'sell'
            quantity: Order quantity

        Returns:
            Weighted average execution price, or None if insufficient liquidity
        """
        if not self.order_book_ready:
            self.logger.log("Order book not ready for execution price calculation", "WARNING")
            return None

        try:
            # Determine which side of the order book to use
            # For buy orders, we take from asks; for sell orders, we take from bids
            book_side = 'asks' if side == 'buy' else 'bids'
            levels = self.get_order_book_levels(book_side, limit=100)

            if not levels:
                self.logger.log(f"No liquidity available for {side} order", "WARNING")
                return None

            remaining_quantity = quantity
            total_cost = Decimal('0')
            total_filled = Decimal('0')

            # Walk through the order book levels
            for level in levels:
                if remaining_quantity <= 0:
                    break

                level_price = level['price']
                level_size = level['size']

                # Calculate how much we can fill at this level
                fill_quantity = min(remaining_quantity, level_size)

                # Update totals
                total_cost += fill_quantity * level_price
                total_filled += fill_quantity
                remaining_quantity -= fill_quantity

            # Check if we have sufficient liquidity
            if remaining_quantity > 0:
                self.logger.log(
                    f"Insufficient liquidity: requested {quantity}, available {total_filled}",
                    "WARNING"
                )
                # Return None or partial execution price based on preference
                # Here we return the partial execution price
                if total_filled == 0:
                    return None

            # Calculate volume-weighted average price
            weighted_avg_price = total_cost / total_filled
            return weighted_avg_price.quantize(Decimal('0.000001'))

        except Exception as e:
            self.logger.log(f"Error calculating execution price: {e}", "ERROR")
            return None

    def get_mid_price_from_orderbook(self) -> Optional[Decimal]:
        """Get mid price from WebSocket order book."""
        if not self.order_book_ready or self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal('2')

    # ---------------------------
    # WS Handlers
    # ---------------------------

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        self._order_update_handler = handler

        def order_update_handler(message):
            """Handle order updates from WebSocket."""
            try:
                # Parse the message structure
                if isinstance(message, str):
                    message = json.loads(message)

                # Check if this is a trade-event with ORDER_UPDATE
                content = message.get("content", {})
                event = content.get("event", "")
                if event == "ORDER_UPDATE":
                    # Extract order data from the nested structure
                    data = content.get('data', {})
                    orders = data.get('order', [])

                    if orders and len(orders) > 0:
                        order = orders[0]  # Get the first order
                        if order.get('contractId') != self.config.contract_id:
                            return

                        order_id = order.get('id')
                        status = order.get('status')
                        side = order.get('side', '').lower()
                        filled_size = order.get('cumMatchSize')

                        if side == self.config.close_order_side:
                            order_type = "CLOSE"
                        else:
                            order_type = "OPEN"

                        # edgex returns TWO filled events for the same order; take the first one
                        if status == "FILLED" and len(data.get('collateral', [])):
                            return

                        # ignore canceled close orders
                        if status == "CANCELED" and order_type == "CLOSE":
                            return

                        # edgex returns partially filled events as "OPEN" orders
                        if status == "OPEN" and Decimal(filled_size) > 0:
                            status = "PARTIALLY_FILLED"

                        if status in ['OPEN', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED']:
                            if self._order_update_handler:
                                self._order_update_handler({
                                    'order_id': order_id,
                                    'side': side,
                                    'order_type': order_type,
                                    'status': status,
                                    'size': order.get('size'),
                                    'price': order.get('price'),
                                    'contract_id': order.get('contractId'),
                                    'filled_size': filled_size
                                })

            except Exception as e:
                self.logger.log(f"Error handling order update: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

        try:
            private_client = self.ws_manager.get_private_client()
            private_client.on_message("trade-event", order_update_handler)
        except Exception as e:
            self.logger.log(f"Could not add trade-event handler: {e}", "ERROR")

    # ---------------------------
    # REST-ish helpers
    # ---------------------------

    @query_retry(default_return=(0, 0))
    async def fetch_bbo_prices(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        depth_params = GetOrderBookDepthParams(contract_id=contract_id, limit=15)
        order_book = await self.client.quote.get_order_book_depth(depth_params)
        order_book_data = order_book['data']

        # Get the first (and should be only) order book entry
        order_book_entry = order_book_data[0]

        # Extract bids and asks from the entry
        bids = order_book_entry.get('bids', [])
        asks = order_book_entry.get('asks', [])

        # Best bid is the highest price someone is willing to buy at
        best_bid = Decimal(bids[0]['price']) if bids and len(bids) > 0 else 0
        # Best ask is the lowest price someone is willing to sell at
        best_ask = Decimal(asks[0]['price']) if asks and len(asks) > 0 else 0
        return best_bid, best_ask

    async def get_order_price(self, direction: str) -> Decimal:
        """Get the price of an order with EdgeX using official SDK."""
        best_bid, best_ask = await self.fetch_bbo_prices(self.config.contract_id)
        if best_bid <= 0 or best_ask <= 0:
            self.logger.log("Invalid bid/ask prices", "ERROR")
            raise ValueError("Invalid bid/ask prices")


        if direction == 'buy':
            # For buy orders, place slightly below best ask to ensure execution
            order_price = best_ask - self.config.tick_size
        else:
            # For sell orders, place slightly above best bid to ensure execution
            order_price = best_bid + self.config.tick_size
        return self.round_to_tick(order_price)

    async def place_open_order(self, contract_id: str, quantity: Decimal, direction: str) -> OrderResult:
        """Place an open order with EdgeX using official SDK with retry logic for POST_ONLY rejections."""
        max_retries = 100
        retry_count = 0

        last_order_id = None
        last_order_price = None
        # current for BTC
        order_price_diff_rate = 0.0001
        while retry_count < max_retries:
            try:
                best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

                if best_bid <= 0 or best_ask <= 0:
                    return OrderResult(success=False, error_message='Invalid bid/ask prices')

                if direction == 'buy':
                    # For buy orders, place slightly below best ask to ensure execution
                    order_price = best_ask - self.config.tick_size
                    side = OrderSide.BUY
                else:
                    # For sell orders, place slightly above best bid to ensure execution
                    order_price = best_bid + self.config.tick_size
                    side = OrderSide.SELL
                # 价差偏差过大直接以cancel的状态返回上层处理
                if last_order_price is not None and abs(self.round_to_tick(order_price) - last_order_price) / last_order_price > order_price_diff_rate:
                    msg = f"Current retry order price has more than {order_price_diff_rate} diff with last order price cancel order execute, last order {last_order_id} status should be [CANCELED]"
                    self.logger.log(msg, "INFO")
                    return OrderResult(success=True, order_id=last_order_id, order_price=last_order_price)
                    
                self.logger.log(f"Placing open order: side={side.value}, size={quantity}, price={self.round_to_tick(order_price)}", "INFO")

                # Place the order using official SDK (post-only to ensure maker order)
                order_result = await self.client.create_limit_order(
                    contract_id=contract_id,
                    size=str(quantity),
                    price=str(self.round_to_tick(order_price)),
                    side=side,
                    post_only=True
                )
                self.logger.log(f"Order placement result: {order_result}", "INFO")
                
                if not order_result or 'data' not in order_result:
                    return OrderResult(success=False, error_message='Failed to place order')

                # Extract order ID from response
                order_id = order_result['data'].get('orderId')
                if not order_id:
                    return OrderResult(success=False, error_message='No order ID in response')

                # Check order status after a short delay to see if it was rejected
                await asyncio.sleep(0.05)
                order_info = await self.get_order_info(order_id)
                self.logger.log(f"Order info after placement: {order_info}", "INFO")
                
                last_order_id = order_id
                last_order_price = self.round_to_tick(order_price)

                if order_info:
                    if order_info.status == 'CANCELED':
                        if retry_count < max_retries - 1:
                            retry_count += 1
                            continue
                        else:
                            return OrderResult(success=False, error_message=f'Order rejected after {max_retries} attempts')
                    elif order_info.status in ['OPEN', 'PARTIALLY_FILLED', 'FILLED']:
                        # Order successfully placed
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side.value,
                            size=quantity,
                            price=order_price,
                            status=order_info.status
                        )
                    else:
                        return OrderResult(success=False, error_message=f'Unexpected order status: {order_info.status}')
                else:
                    # Assume order is successful if we can't get info
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        side=side.value,
                        size=quantity,
                        price=order_price,
                        status='OPEN'
                    )

            except Exception as e:
                if retry_count < max_retries - 1:
                    retry_count += 1
                    await asyncio.sleep(0.1)  # Wait before retry
                    continue
                else:
                    return OrderResult(success=False, error_message=str(e))

        return OrderResult(success=False, error_message='Max retries exceeded')

    async def place_close_order(self, contract_id: str, quantity: Decimal, price: Decimal, side: str) -> OrderResult:
        """Place a close order with EdgeX using official SDK with retry logic for POST_ONLY rejections."""
        max_retries = 15
        retry_count = 0

        while retry_count < max_retries:
            try:
                best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

                if best_bid <= 0 or best_ask <= 0:
                    return OrderResult(success=False, error_message='Invalid bid/ask prices')

                # Convert side string to OrderSide enum
                order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL

                # Adjust order price based on market conditions and side
                adjusted_price = price

                if side.lower() == 'sell':
                    # For sell orders, ensure price is above best bid to be a maker order
                    if price <= best_bid:
                        adjusted_price = best_bid + self.config.tick_size
                elif side.lower() == 'buy':
                    # For buy orders, ensure price is below best ask to be a maker order
                    if price >= best_ask:
                        adjusted_price = best_ask - self.config.tick_size

                adjusted_price = self.round_to_tick(adjusted_price)
                # Place the order using official SDK (post-only to avoid taker fees)
                order_result = await self.client.create_limit_order(
                    contract_id=contract_id,
                    size=str(quantity),
                    price=str(adjusted_price),
                    side=order_side,
                    post_only=True
                )

                if not order_result or 'data' not in order_result:
                    return OrderResult(success=False, error_message='Failed to place order')

                # Extract order ID from response
                order_id = order_result['data'].get('orderId')
                if not order_id:
                    return OrderResult(success=False, error_message='No order ID in response')

                # Check order status after a short delay to see if it was rejected
                await asyncio.sleep(0.01)
                order_info = await self.get_order_info(order_id)

                if order_info:
                    if order_info.status == 'CANCELED':
                        if retry_count < max_retries - 1:
                            retry_count += 1
                            continue
                        else:
                            return OrderResult(success=False, error_message=f'Close order rejected after {max_retries} attempts')
                    elif order_info.status in ['OPEN', 'PARTIALLY_FILLED', 'FILLED']:
                        # Order successfully placed
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side,
                            size=quantity,
                            price=adjusted_price,
                            status=order_info.status
                        )
                    else:
                        return OrderResult(success=False, error_message=f'Unexpected close order status: {order_info.status}')
                else:
                    # Assume order is successful if we can't get info
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        side=side,
                        size=quantity,
                        price=adjusted_price
                    )

            except Exception as e:
                if retry_count < max_retries - 1:
                    retry_count += 1
                    await asyncio.sleep(0.1)  # Wait before retry
                    continue
                else:
                    return OrderResult(success=False, error_message=str(e))

        return OrderResult(success=False, error_message='Max retries exceeded for close order')

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order with EdgeX using official SDK."""
        try:
            # cancel之前先查询订单状态，避免重复取消
            order_info = await self.get_order_info(order_id)
            filled_size = Decimal(order_info.filled_size)
            price = Decimal(order_info.price)
            side = order_info.side
            if order_info and order_info.status in ['CANCELED', 'FILLED']:
                self.logger.log(f"Order {order_id} already {order_info.status}, no need to cancel", "INFO")
                return OrderResult(success=False, status=order_info.status, filled_size=filled_size, price=price, side=side, error_message=f'Order already {order_info.status}')
            # Create cancel parameters using official SDK
            cancel_params = CancelOrderParams(order_id=order_id)

            # Cancel the order using official SDK
            cancel_result = await self.client.cancel_order(cancel_params)

            if not cancel_result or 'data' not in cancel_result:
                return OrderResult(success=False, status=order_info.status, filled_size=filled_size, price=price, side=side, error_message='Failed to cancel order')

            return OrderResult(success=True, status=order_info.status, filled_size=filled_size, price=price, side=side)

        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    @query_retry()
    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information from EdgeX using official SDK."""
        # Use the newly created get_order_by_id method
        order_result = await self.client.order.get_order_by_id(order_id_list=[order_id])

        if not order_result or 'data' not in order_result:
            return None

        # The API returns a list of orders, get the first (and should be only) one
        order_list = order_result['data']
        if order_list and len(order_list) > 0:
            order_data = order_list[0]
            return OrderInfo(
                order_id=order_data.get('id', ''),
                side=order_data.get('side', '').lower(),
                size=Decimal(order_data.get('size', 0)),
                price=Decimal(order_data.get('price', 0)),
                status=order_data.get('status', ''),
                filled_size=Decimal(order_data.get('cumMatchSize', 0)),
                remaining_size=Decimal(order_data.get('size', 0)) - Decimal(order_data.get('cumMatchSize', 0))
            )

        return None

    @query_retry(default_return=[])
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract using official SDK."""
        # Get active orders using official SDK
        params = GetActiveOrderParams(size="200", offset_data="", filter_contract_id_list=[contract_id])
        active_orders = await self.client.get_active_orders(params)

        if not active_orders or 'data' not in active_orders:
            return []

        # Filter orders for the specific contract and ensure they are dictionaries
        # The API returns orders under 'dataList' key, not 'orderList'
        order_list = active_orders['data'].get('dataList', [])
        contract_orders = []

        for order in order_list:
            if isinstance(order, dict) and order.get('contractId') == contract_id:
                contract_orders.append(OrderInfo(
                    order_id=order.get('id', ''),
                    side=order.get('side', '').lower(),
                    size=Decimal(order.get('size', 0)),
                    price=Decimal(order.get('price', 0)),
                    status=order.get('status', ''),
                    filled_size=Decimal(order.get('cumMatchSize', 0)),
                    remaining_size=Decimal(order.get('size', 0)) - Decimal(order.get('cumMatchSize', 0))
                ))

        return contract_orders

    @query_retry(default_return=0)
    async def get_account_positions(self) -> Decimal:
        """Get account positions using official SDK."""
        positions_data = await self.client.get_account_positions()
        if not positions_data or 'data' not in positions_data:
            self.logger.log("No positions or failed to get positions", "WARNING")
            position_amt = 0
        else:
            # The API returns positions under data.positionList
            positions = positions_data.get('data', {}).get('positionList', [])
            if positions:
                # Find position for current contract
                position = None
                for p in positions:
                    if isinstance(p, dict) and p.get('contractId') == self.config.contract_id:
                        position = p
                        break

                if position:
                    position_amt = abs(Decimal(position.get('openSize', 0)))
                else:
                    position_amt = 0
            else:
                position_amt = 0
        return position_amt

    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """Get contract ID for a ticker."""
        ticker = self.config.ticker
        if len(ticker) == 0:
            self.logger.log("Ticker is empty", "ERROR")
            raise ValueError("Ticker is empty")

        response = await self.client.get_metadata()
        data = response.get('data', {})
        if not data:
            self.logger.log("Failed to get metadata", "ERROR")
            raise ValueError("Failed to get metadata")

        contract_list = data.get('contractList', [])
        if not contract_list:
            self.logger.log("Failed to get contract list", "ERROR")
            raise ValueError("Failed to get contract list")

        current_contract = None
        for c in contract_list:
            if c.get('contractName') == ticker+'USD':
                current_contract = c
                break

        if not current_contract:
            self.logger.log("Failed to get contract ID for ticker", "ERROR")
            raise ValueError("Failed to get contract ID for ticker")

        self.config.contract_id = current_contract.get('contractId')
        min_quantity = Decimal(current_contract.get('minOrderSize'))
        if self.config.quantity < min_quantity:
            self.logger.log(f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}", "ERROR")
            raise ValueError(f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}")

        self.config.tick_size = Decimal(current_contract.get('tickSize'))

        return self.config.contract_id, self.config.tick_size

    @query_retry(default_return=0)
    async def get_ticker_position(self) -> Decimal:
        """Get account positions using official SDK."""
        # 接口文档地址: https://edgex-1.gitbook.io/edgeX-documentation/api/private-api/account-api#get-account-asset
        position = None
        positions_data = await self.client.get_account_positions()
        if not positions_data or 'data' not in positions_data:
            self.logger.log("No positions or failed to get positions", "WARNING")
        else:
            positions = positions_data.get('data', {}).get('positionAssetList', [])
            if positions:
                for p in positions:
                    if isinstance(p, dict) and p.get('contractId') == self.config.contract_id:
                        position = p
                        break
        return position

    async def get_ticker_position_liquidation_price(self) -> Decimal:
        """获取指定合约的强平价"""
        position = await self.get_ticker_position()
        if position is None:
            raise ValueError("No position found for liquidation price calculation")
        return Decimal(position["liquidatePrice"])
    
    async def get_ticker_position_pnl(self) -> Decimal:
        position = await self.get_ticker_position()
        if position is None:
            raise ValueError("No position found for position PnL")
        # unrealizePnl, termRealizePnl
        return Decimal(position["totalRealizePnl"])
    
    async def get_ticker_position_value(self) -> Decimal:
        position = await self.get_ticker_position()
        if position is None:
            raise ValueError("No position found for position value")
        # unrealizePnl, termRealizePnl
        return Decimal(position["positionValue"])

    @query_retry(default_return=Decimal('0'))
    async def get_funding_rate(self, contract_id: str) -> Decimal:
        """获取资金费率 - 对冲模式专用，自动转换为1小时费率"""
        try:
            url = f"{self.base_url}/api/v1/public/funding/getLatestFundingRate?contractId={contract_id}"
            
            import requests
            response = requests.get(url, timeout=10)
            
            if response.status_code != 200:
                self.logger.log(f"⚠️ HTTP请求失败: {response.status_code}", "WARNING")
                return Decimal('0')
                
            response_data = response.json()

            if response_data.get('code') == 'SUCCESS' and response_data.get('data'):
                data_list = response_data['data']
                if len(data_list) > 0:
                    funding_data = data_list[0]
                    funding_rate = Decimal(str(funding_data.get('fundingRate', '0')))
                    
                    funding_interval = Decimal(funding_data.get('fundingRateIntervalMin', 240)) / 60
                    funding_rate_1h = funding_rate / funding_interval

                    self.logger.log(f"📊 EdgeX资金费率: {funding_rate:.6f} ({funding_interval}h) → {funding_rate_1h:.6f} (1h)", "INFO")
                    return funding_rate_1h
                else:
                    self.logger.log(f"⚠️ 无资金费率数据: {contract_id}", "WARNING")
                    return Decimal('0')
            else:
                self.logger.log(f"⚠️ 获取资金费率失败: {response_data.get('msg', 'Unknown error')}", "WARNING")
                return Decimal('0')

        except Exception as e:
            self.logger.log(f"❌ 资金费率获取异常: {e}", "ERROR")
            return Decimal('0')

    @query_retry(default_return=(Decimal('0'), Decimal('0')))
    async def get_mid_price(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)
            if best_bid > 0 and best_ask > 0:
                mid_price = (best_bid + best_ask) / Decimal('2')
                spread = best_ask - best_bid
                return mid_price, spread
            return Decimal('0'), Decimal('0')
        except Exception as e:
            self.logger.log(f"❌ 获取中间价格失败: {e}", "ERROR")
            return Decimal('0'), Decimal('0')

    @query_retry(default_return={})
    async def get_account_balances(self) -> Dict[str, Decimal]:
        try:
            account_data = await self.client.get_account_asset()
            if not account_data or 'data' not in account_data:
                return {}

            balances = {}
            data = account_data['data']
            
            # 根据真实API结构，余额信息在 collateralAssetModelList 中
            collateral_assets = data.get('collateralAssetModelList', [])
            for asset in collateral_assets:
                # coinId "1000" 对应 USDC
                coin_id = asset.get('coinId', '')
                if coin_id == '1000':  # USDC
                    available_amount = Decimal(str(asset.get('availableAmount', '0')))
                    total_equity = Decimal(str(asset.get('totalEquity', '0')))
                    
                    balances['USDC'] = {
                        'available': available_amount,
                        'total': total_equity
                    }
                    break
            
            # 如果没有找到USDC，设置默认值
            if 'USDC' not in balances:
                balances['USDC'] = {
                    'available': Decimal('0'),
                    'total': Decimal('0')
                }

            self.logger.log(f"📊 账户余额获取成功: USDC可用={balances['USDC']['available']:.6f}, 总权益={balances['USDC']['total']:.6f}", "INFO")
            return balances

        except Exception as e:
            self.logger.log(f"❌ 获取账户余额失败: {e}", "ERROR")
            return {}
        
    @query_retry(default_return={'bids': [], 'asks': [], 'timestamp': 0})
    async def get_order_book_depth(self, contract_id: str, limit: int = 15) -> Dict:
        """
        获取完整订单簿深度数据
        
        Args:
            contract_id: 合约ID
            limit: 深度档位限制（最大15）
            
        Returns:
            {
                'bids': [{'price': Decimal, 'size': Decimal}, ...],
                'asks': [{'price': Decimal, 'size': Decimal}, ...],
                'timestamp': int
            }
        """
        import time
        
        depth_params = GetOrderBookDepthParams(contract_id=contract_id, limit=min(limit, 15))
        order_book = await self.client.quote.get_order_book_depth(depth_params)
        order_book_data = order_book['data']
        
        if not order_book_data:
            return {'bids': [], 'asks': [], 'timestamp': int(time.time() * 1000)}
            
        order_book_entry = order_book_data[0]
        
        # 转换为统一格式
        bids_data = [
            {'price': Decimal(bid['price']), 'size': Decimal(bid['size'])} 
            for bid in order_book_entry.get('bids', [])
        ]
        asks_data = [
            {'price': Decimal(ask['price']), 'size': Decimal(ask['size'])} 
            for ask in order_book_entry.get('asks', [])
        ]
        
        return {
            'bids': bids_data,
            'asks': asks_data,
            'timestamp': int(time.time() * 1000)
        }