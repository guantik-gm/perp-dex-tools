#!/usr/bin/env python3
"""
Test script for GRVT WebSocket order monitoring.
This script connects to GRVT WebSocket and prints any order updates received.
"""

import asyncio
import os
import sys
import traceback
from datetime import datetime
from typing import Dict, Any

from pysdk.grvt_ccxt_env import GrvtEnv, GrvtWSEndpointType
from pysdk.grvt_ccxt_logging_selector import logger
from pysdk.grvt_ccxt_ws import GrvtCcxtWS


class OrderUpdateMonitor:
    """Monitor for GRVT order updates via WebSocket."""

    def __init__(self, ticker: str = "BTC"):
        """Initialize the monitor with a ticker symbol."""
        self.ticker = ticker
        self.test_api = None
        self.instrument = f"{ticker}_USDT_Perp"

    def order_update_handler(self, message: Dict[str, Any]) -> None:
        """Handle order updates from WebSocket."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Parse the message structure
        if 'feed' in message:
            data = message.get('feed', {})
            leg = data.get('legs', [])[0]

            if isinstance(data, dict):
                try:
                    order_state = data.get('state', {})
                    # Extract order data
                    order_id = data.get('order_id', '')
                    status = order_state.get('status', '')
                    side = 'buy' if leg.get('is_buying_asset') else 'sell'
                    size = leg.get('size', '0')
                    price = leg.get('limit_price', '0')
                    filled_size = order_state.get('traded_size')[0]
                    contract_id = leg.get('instrument', '')

                    if order_id and status:
                        print(f"\n[{timestamp}] YOUR ORDER UPDATE:")
                        print(f"  Order ID: {order_id}")
                        print(f"  Side: {side.upper()}")
                        print(f"  Status: {status}")
                        print(f"  Size: {size}")
                        print(f"  Price: {price}")
                        print(f"  Filled Size: {filled_size}")
                        print(f"  Contract: {contract_id}")
                        print("-" * 50)
                except Exception as e:
                    print(f"Error handling order update: {e}")
                    print(f"Traceback: {traceback.format_exc()}")
        else:
            # Handle other private message types (position, fill, etc.)
            method = message.get('method', 'unknown')
            if method in ['position', 'fill', 'cancel', 'state']:
                print(f"\n[{timestamp}] {method.upper()} UPDATE:")
                print(f"  Raw Message: {message}")
                print("-" * 50)

    async def callback_order(self, message: dict) -> None:
        """Callback specifically for order updates."""
        print(f"Order callback: {message}")
        print("!" * 50)
        self.order_update_handler(message)

    async def callback_general(self, message: dict) -> None:
        """General callback for other message types."""
        print(f"General callback for other message types: {message}")
        print("-" * 50)

    async def grvt_ws_subscribe(self, api: GrvtCcxtWS, args_list: dict) -> None:
        """Subscribes to Websocket channels/feeds in args list."""
        for stream, (callback, ws_endpoint_type, params) in args_list.items():
            logger.info(f"Subscribing to {stream} {params=}")
            await api.subscribe(
                stream=stream,
                callback=callback,
                ws_end_point_type=ws_endpoint_type,
                params=params,
            )
            await asyncio.sleep(0)

    async def start_monitoring(self) -> None:
        """Start monitoring order updates."""
        try:
            print(f"Starting GRVT WebSocket monitoring for YOUR OWN orders on {self.ticker}...")
            print("This will only show updates for orders you place/cancel on the GRVT website.")
            print("\nMake sure you have set the following environment variables:")
            print("- GRVT_TRADING_ACCOUNT_ID")
            print("- GRVT_PRIVATE_KEY")
            print("- GRVT_API_KEY")
            print("- GRVT_ENV (optional, defaults to 'prod')")
            print("\nPress Ctrl+C to stop monitoring...")
            print("=" * 60)

            # Setup parameters
            params = {
                "api_key": os.getenv("GRVT_API_KEY"),
                "trading_account_id": os.getenv("GRVT_TRADING_ACCOUNT_ID"),
                "api_ws_version": os.getenv("GRVT_WS_STREAM_VERSION", "v1"),
            }
            if os.getenv("GRVT_PRIVATE_KEY"):
                params["private_key"] = os.getenv("GRVT_PRIVATE_KEY")

            env = GrvtEnv(os.getenv("GRVT_ENV", "prod"))
            loop = asyncio.get_running_loop()

            # Initialize WebSocket client
            self.test_api = GrvtCcxtWS(env, loop, logger, parameters=params)
            await self.test_api.initialize()

            # Define subscription arguments - ONLY private feeds for your own orders
            prv_args_dict = {
                # Private trade data feeds - your own orders only
                "order": (
                    self.callback_order,
                    GrvtWSEndpointType.TRADE_DATA_RPC_FULL,
                    {"instrument": self.instrument},
                ),
            }

            # Subscribe to feeds - ONLY private feeds for your own orders
            if "private_key" in params:
                print(f"Subscribing to YOUR OWN order feeds for {self.instrument}...")
                print("Monitoring: orders, positions, fills, cancellations, and state changes")
                await self.grvt_ws_subscribe(self.test_api, prv_args_dict)
            else:
                print("ERROR: Private key required to monitor your own orders!")
                print("Please set GRVT_PRIVATE_KEY environment variable.")
                return

            print("Successfully connected to GRVT WebSocket!")
            print(f"Monitoring YOUR OWN orders for {self.instrument}...")
            print("Now place/cancel orders on the GRVT website to see updates!")
            print("=" * 60)

            # Keep the connection alive
            while True:
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            print("\n\nStopping order monitoring...")
        except Exception as e:
            print(f"Error during monitoring: {e}")
            print(f"Traceback: {traceback.format_exc()}")
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        """Clean up resources."""
        if self.test_api:
            print("Disconnecting from GRVT WebSocket...")
            try:
                del self.test_api
            except Exception as e:
                print(f"Error during cleanup: {e}")
            print("Disconnected from GRVT WebSocket.")


async def main():
    """Main function."""
    # You can change the ticker here (e.g., "BTC", "ETH", "SOL")
    ticker = "BTC"

    # Check if ticker is provided as command line argument
    if len(sys.argv) > 1:
        ticker = sys.argv[1].upper()

    monitor = OrderUpdateMonitor(ticker)
    await monitor.start_monitoring()


if __name__ == "__main__":
    # Load environment variables from .env file if it exists
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("Warning: python-dotenv not installed. Make sure environment variables are set manually.")

    asyncio.run(main())
