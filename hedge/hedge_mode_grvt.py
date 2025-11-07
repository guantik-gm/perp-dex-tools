import logging
import os
import sys
from decimal import Decimal

import sys
import os

from exchanges.grvt import GrvtClient
from hedge.hedge_mode_abc import Config, HedgeBotAbc
from hedge.strategy.liquid_risk_strategy import LiquidRiskStrategy
from hedge.strategy.price_volatility_strategy import PriceVolatilityStrategy
from hedge.strategy.spread_strategy import SpreadStrategy
from hedge.strategy.timing_strategy import TimingStrategy
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 与edgex/grvt保持一致: cancel_order, place_open_order, get_ticker_position, get_ticker_position_liquidation_price, get_ticker_position_pnl, get_ticker_position_value
# 可选: get_funding_rate, get_order_book_depth
class HedgeBot(HedgeBotAbc):
    """Trading bot that places post-only orders on GRVT and hedges with market orders on Lighter."""

    def __init__(self, ticker: str, order_quantity: Decimal, fill_timeout: int = 5, iterations: int = 20):
        super().__init__(ticker, order_quantity, fill_timeout, iterations)
        self.hedge_strategies = [
            LiquidRiskStrategy(priority=100),
            PriceVolatilityStrategy(priority=15),
            TimingStrategy(priority=20),
            SpreadStrategy(priority=10)
        ]

    def primary_exchange_name(self):
        return "Grvt"

    def primary_client_vars(self):
        return {"grvt_trading_account_id": os.getenv('GRVT_TRADING_ACCOUNT_ID'),
                "grvt_private_key": os.getenv('GRVT_PRIVATE_KEY'),
                "grvt_api_key": os.getenv('GRVT_API_KEY'),
                "grvt_environment": os.getenv('GRVT_ENVIRONMENT', 'prod')}

    def primary_client_init(self):
        """Initialize the GRVT client."""
        if not all([self.grvt_trading_account_id, self.grvt_private_key]):
            raise ValueError("GRVT_TRADING_ACCOUNT_ID and GRVT_PRIVATE_KEY must be set in environment variables")

        # Create config for GRVT client
        config_dict = {
            'ticker': self.ticker,
            'contract_id': '',  # Will be set when we get contract info
            'quantity': self.order_quantity,
            'tick_size': Decimal('0.01'),  # Will be updated when we get contract info
            'close_order_side': 'sell'  # Default, will be updated based on strategy
        }

        # Wrap in Config class for GRVT client
        config = Config(config_dict)

        # Initialize GRVT client
        self.primary_client = GrvtClient(config)

        self.logger.info("✅ GRVT client initialized successfully")

    def primary_logger_level(self):
        # Disable verbose logging from external libraries
        logging.getLogger('urllib3').setLevel(logging.CRITICAL)
        logging.getLogger('requests').setLevel(logging.CRITICAL)
        logging.getLogger('websockets').setLevel(logging.CRITICAL)
        logging.getLogger('pysdk.grvt_ccxt').setLevel(logging.CRITICAL)
        logging.getLogger('pysdk.grvt_ccxt_ws').setLevel(logging.CRITICAL)
        logging.getLogger('pysdk.grvt_ccxt_logging_selector').setLevel(logging.CRITICAL)
        logging.getLogger('pysdk.grvt_ccxt_env').setLevel(logging.CRITICAL)
        logging.getLogger('lighter').setLevel(logging.CRITICAL)
        logging.getLogger('lighter.signer_client').setLevel(logging.CRITICAL)

    def primary_fee_rate(self) -> Decimal:
        # -0.001% / 0.037%
        return Decimal('-0.00001')