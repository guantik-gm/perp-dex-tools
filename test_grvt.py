import os
import asyncio
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from pysdk.grvt_ccxt import GrvtCcxt
from pysdk.grvt_ccxt_ws import GrvtCcxtWS
from pysdk.grvt_ccxt_env import GrvtEnv
from pysdk.grvt_ccxt_types import GrvtOrderSide

import dotenv
dotenv.load_dotenv()


trading_account_id = os.getenv('GRVT_TRADING_ACCOUNT_ID')
private_key = os.getenv('GRVT_PRIVATE_KEY')
api_key = os.getenv('GRVT_API_KEY')
environment = os.getenv('GRVT_ENVIRONMENT', 'prod')

env_map = {
    'prod': GrvtEnv.PROD,
    'testnet': GrvtEnv.TESTNET,
    'staging': GrvtEnv.STAGING,
    'dev': GrvtEnv.DEV
}

env = env_map.get(environment.lower(), GrvtEnv.PROD)

parameters = {
    'trading_account_id': trading_account_id,
    'private_key': private_key,
    'api_key': api_key
}

# Initialize REST client
rest_client = GrvtCcxt(
    env=env,
    parameters=parameters
)


markets = rest_client.fetch_markets()

positions = rest_client.fetch_positions()

active_orders = rest_client.fetch_open_orders(symbol='BTC_USDT_Perp')

order_data = rest_client.fetch_order(id='0x0101010503e9b55a00000000359e3c84')
cancel_result = rest_client.cancel_order(id='0x0101010503e9b55a00000000359e3c84')

order_book = rest_client.fetch_order_book(symbol='BTC_USDT_Perp', limit=10)


best_bid, best_ask = rest_client.fetch_bbo_prices(symbol='BTC_USDT_Perp')

order_result = rest_client.create_limit_order(
    symbol='BTC_USDT_Perp',
    side='buy',
    amount=Decimal('0.001'),
    price=Decimal(116000),
    params={'post_only': True}
)