import logging
import os
import sys
from decimal import Decimal

import sys
import os

from hedge.hedge_mode_abc import Config, HedgeBotAbc
from hedge.hedge_strategy import SmartHedgeStrategy
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchanges.edgex import EdgeXClient


class HedgeBot(HedgeBotAbc):
    """Trading bot that places post-only orders on EdgeX and hedges with market orders on Lighter."""

    def __init__(self, ticker: str, order_quantity: Decimal, fill_timeout: int = 5, iterations: int = 20):
        super().__init__(ticker, order_quantity, fill_timeout, iterations)
        self.hedge_position_strategy = SmartHedgeStrategy()
    
    def primary_exchange_name(self):
        return "edgex"
    
    def primary_client_vars(self):
        return {"account_id": os.getenv('EDGEX_ACCOUNT_ID'),
                "stark_private_key": os.getenv('EDGEX_STARK_PRIVATE_KEY'),
                "base_url": os.getenv('EDGEX_BASE_URL', 'https://pro.edgex.exchange')}
    
    def primary_client_init(self):
        """Initialize the EdgeX client."""
        if not all([self.account_id, self.stark_private_key]):
            raise ValueError("EDGEX_ACCOUNT_ID and EDGEX_STARK_PRIVATE_KEY must be set in environment variables")

        # Create config for EdgeX client
        config_dict = {
            'ticker': self.ticker,
            'contract_id': '',  # Will be set when we get contract info
            'quantity': self.order_quantity,
            'tick_size': Decimal('0.01'),  # Will be updated when we get contract info
            'close_order_side': 'sell'  # Default, will be updated based on strategy
        }

        # Wrap in Config class for EdgeX client
        config = Config(config_dict)

        # Initialize EdgeX client
        self.primary_client = EdgeXClient(config)

        self.logger.info("✅ EdgeX client initialized successfully")

    def primary_logger_level(self):
        # Disable verbose logging from external libraries
        logging.getLogger('urllib3').setLevel(logging.CRITICAL)
        logging.getLogger('requests').setLevel(logging.CRITICAL)
        logging.getLogger('websockets').setLevel(logging.CRITICAL)
        logging.getLogger('edgex_sdk').setLevel(logging.CRITICAL)
        logging.getLogger('lighter').setLevel(logging.CRITICAL)
        logging.getLogger('lighter.signer_client').setLevel(logging.CRITICAL)
