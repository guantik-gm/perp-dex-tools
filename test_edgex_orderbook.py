#!/usr/bin/env python3
"""
测试EdgeX WebSocket订单簿功能
"""
import asyncio
import os
import sys
from decimal import Decimal
from dotenv import load_dotenv

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from exchanges.edgex import EdgeXClient
from hedge_v1.hedge_mode_abc import Config


async def test_edgex_orderbook():
    """Test EdgeX WebSocket order book functionality."""
    load_dotenv()

    print("🚀 Starting EdgeX WebSocket Order Book Test...")

    # Create config
    config_dict = {
        'ticker': 'BTC',
        'contract_id': '',  # Will be set after getting contract info
        'quantity': Decimal('0.001'),
        'tick_size': Decimal('0.01'),
        'close_order_side': 'sell'
    }
    config = Config(config_dict)

    # Initialize EdgeX client
    print("📡 Initializing EdgeX client...")
    client = EdgeXClient(config)

    try:
        # Get contract info
        print("📋 Fetching contract information...")
        contract_id, tick_size = await client.get_contract_attributes()
        print(f"✅ Contract ID: {contract_id}, Tick Size: {tick_size}")

        # Connect to WebSocket
        print("🔌 Connecting to WebSocket...")
        await client.connect()

        # Wait for order book to be ready
        print("⏳ Waiting for order book to initialize...")
        max_wait = 30
        wait_time = 0
        while not client.order_book_ready and wait_time < max_wait:
            await asyncio.sleep(1)
            wait_time += 1
            if wait_time % 5 == 0:
                print(f"   Still waiting... ({wait_time}s)")

        if not client.order_book_ready:
            print("❌ Order book failed to initialize within 30 seconds")
            return

        print("✅ Order book is ready!")

        # Test 1: Get best prices
        print("\n=== Test 1: Get Best Prices ===")
        best_bid, best_ask = client.get_best_prices()
        print(f"Best Bid: {best_bid}")
        print(f"Best Ask: {best_ask}")
        mid_price = client.get_mid_price_from_orderbook()
        print(f"Mid Price: {mid_price}")

        # Test 2: Get order book levels
        print("\n=== Test 2: Get Order Book Levels ===")
        bids = client.get_order_book_levels('bids', limit=5)
        asks = client.get_order_book_levels('asks', limit=5)
        print("Top 5 Bids:")
        for i, bid in enumerate(bids, 1):
            print(f"  {i}. Price: {bid['price']}, Size: {bid['size']}")
        print("Top 5 Asks:")
        for i, ask in enumerate(asks, 1):
            print(f"  {i}. Price: {ask['price']}, Size: {ask['size']}")

        # Test 3: Calculate execution prices
        print("\n=== Test 3: Calculate Execution Prices ===")
        test_quantity = Decimal('0.01')
        buy_exec_price = client.calculate_execution_price('buy', test_quantity)
        sell_exec_price = client.calculate_execution_price('sell', test_quantity)
        print(f"Quantity: {test_quantity}")
        print(f"Buy Execution Price (VWAP): {buy_exec_price}")
        print(f"Sell Execution Price (VWAP): {sell_exec_price}")

        if buy_exec_price and sell_exec_price:
            spread = buy_exec_price - sell_exec_price
            print(f"Execution Spread: {spread}")

        # Test 4: Compare with REST API
        print("\n=== Test 4: Compare WebSocket vs REST API ===")
        rest_bid, rest_ask = await client.fetch_bbo_prices(contract_id)
        print(f"REST API - Bid: {rest_bid}, Ask: {rest_ask}")
        print(f"WebSocket - Bid: {best_bid}, Ask: {best_ask}")

        if best_bid and best_ask:
            bid_diff = abs(best_bid - rest_bid) if rest_bid else Decimal('0')
            ask_diff = abs(best_ask - rest_ask) if rest_ask else Decimal('0')
            print(f"Bid Difference: {bid_diff}")
            print(f"Ask Difference: {ask_diff}")

        # Monitor for a few seconds
        print("\n=== Monitoring Order Book Updates (10 seconds) ===")
        for i in range(10):
            await asyncio.sleep(1)
            current_bid, current_ask = client.get_best_prices()
            print(f"[{i+1}s] Bid: {current_bid}, Ask: {current_ask}, "
                  f"Bids: {len(client.order_book['bids'])}, Asks: {len(client.order_book['asks'])}")

        print("\n✅ All tests completed successfully!")

    except Exception as e:
        print(f"\n❌ Error during test: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🔌 Disconnecting...")
        await client.disconnect()
        print("👋 Test finished")


if __name__ == "__main__":
    asyncio.run(test_edgex_orderbook())
