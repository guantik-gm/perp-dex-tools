# GRVT WebSocket Order Monitoring Test

This script allows you to test the GRVT WebSocket connection and monitor YOUR OWN order updates in real-time using the pysdk package directly.

## Setup

1. Make sure you have the required environment variables set:

   ```bash
   export GRVT_TRADING_ACCOUNT_ID="your_trading_account_id"
   export GRVT_PRIVATE_KEY="your_private_key"
   export GRVT_API_KEY="your_api_key"
   export GRVT_ENV="testnet"  # or prod/staging/dev
   ```

2. Or create a `.env` file in the project root with:
   ```
   GRVT_TRADING_ACCOUNT_ID=your_trading_account_id
   GRVT_PRIVATE_KEY=your_private_key
   GRVT_API_KEY=your_api_key
   GRVT_ENV=testnet
   ```

## Usage

### Basic usage (monitor BTC orders):

```bash
python test_grvt_websocket.py
```

### Monitor specific ticker:

```bash
python test_grvt_websocket.py ETH
python test_grvt_websocket.py SOL
```

## What it does

1. Connects to GRVT WebSocket using pysdk directly
2. Subscribes ONLY to private feeds for your own trading account
3. Monitors YOUR OWN order updates, position changes, fills, and cancellations
4. Prints any messages received in real-time with detailed information
5. Shows order details including:
   - Order ID
   - Side (buy/sell)
   - Status (OPEN/FILLED/CANCELED/etc.)
   - Size and Price
   - Filled Size
   - Contract ID

## Testing

1. Run the script
2. Use the GRVT website UI to place/cancel orders
3. Watch the console for real-time order updates
4. Press Ctrl+C to stop monitoring

## Example Output

```
Starting GRVT WebSocket monitoring for YOUR OWN orders on BTC...
This will only show updates for orders you place/cancel on the GRVT website.

Make sure you have set the following environment variables:
- GRVT_TRADING_ACCOUNT_ID
- GRVT_PRIVATE_KEY
- GRVT_API_KEY
- GRVT_ENV (optional, defaults to 'testnet')

Press Ctrl+C to stop monitoring...
============================================================
Subscribing to YOUR OWN order feeds for BTC_USDT_Perp...
Monitoring: orders, positions, fills, cancellations, and state changes
Successfully connected to GRVT WebSocket!
Monitoring YOUR OWN orders for BTC_USDT_Perp...
Now place/cancel orders on the GRVT website to see updates!
============================================================

[2024-01-15 14:30:25] YOUR ORDER UPDATE:
  Order ID: 12345
  Side: BUY
  Status: OPEN
  Size: 0.001
  Price: 45000.00
  Filled Size: 0
  Contract: BTC_USDT_Perp
--------------------------------------------------

[2024-01-15 14:30:30] FILL UPDATE:
  Raw Message: {'method': 'fill', 'params': {...}, 'result': {...}}
--------------------------------------------------
```
