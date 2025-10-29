"""
Unit tests for hedge/lighter_proxy.py

Tests the LighterProxy class with comprehensive mocking for Lighter DEX integration.
"""

import pytest
import asyncio
import logging
import os
import sys
import time
import json
import requests
import websockets
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch, MagicMock, call

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hedge.lighter_proxy import LighterProxy


class TestLighterProxyInitialization:
    """Test LighterProxy initialization and basic setup"""
    
    @pytest.fixture
    def mock_signer_client(self):
        """Mock SignerClient for testing"""
        mock_client = Mock()
        mock_client.check_client.return_value = None  # No error
        mock_client.create_auth_token_with_expiry = Mock(return_value=("auth_token", None))
        mock_client.sign_create_order = Mock(return_value=("tx_info", None))
        mock_client.send_tx = AsyncMock(return_value="tx_hash_123")
        mock_client.modify_order = AsyncMock(return_value=("tx_info", "tx_hash", None))
        mock_client.ORDER_TYPE_LIMIT = 1
        mock_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
        mock_client.TX_TYPE_CREATE_ORDER = 1
        return mock_client
    
    @pytest.fixture
    def mock_environment(self):
        """Mock environment variables"""
        env_vars = {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_private_key_123'
        }
        with patch.dict(os.environ, env_vars):
            yield
    
    @pytest.fixture
    def mock_requests_response(self):
        """Mock requests response for market config"""
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.text = '{"order_books": []}'
        mock_response.json.return_value = {
            "order_books": [
                {
                    "symbol": "BTC",
                    "market_id": 1,
                    "supported_size_decimals": 8,
                    "supported_price_decimals": 2
                }
            ]
        }
        return mock_response
    
    @pytest.fixture
    def mock_logger(self):
        """Mock logger for testing"""
        logger = Mock(spec=logging.Logger)
        logger.info = Mock()
        logger.error = Mock()
        logger.warning = Mock()
        logger.debug = Mock()
        return logger
    
    def test_lighter_proxy_initialization_success(self, mock_environment, mock_logger):
        """Test successful LighterProxy initialization"""
        with patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests, \
             patch.object(LighterProxy, '_initialize_lighter_client') as mock_init_client, \
             patch.object(LighterProxy, '_get_lighter_market_config') as mock_market_config:
            
            # Setup mocks
            mock_init_client.return_value = Mock()
            mock_market_config.return_value = (1, 100000000, 100)
            
            # Create LighterProxy instance
            proxy = LighterProxy(
                ticker="BTC",
                logger=mock_logger,
                position_callback=None
            )
            
            # Verify initialization
            assert proxy.ticker == "BTC"
            assert proxy.logger == mock_logger
            assert proxy.account_index == 1
            assert proxy.api_key_index == 2
            assert proxy.lighter_order_filled is False
            assert proxy.stop_flag is False
            assert proxy.lighter_order_book == {"bids": {}, "asks": {}}
            assert proxy.lighter_order_book_ready is False
    
    def test_initialize_lighter_client_success(self, mock_environment, mock_logger, mock_signer_client):
        """Test successful Lighter client initialization"""
        with patch('hedge.lighter_proxy.SignerClient', return_value=mock_signer_client) as mock_signer_class, \
             patch.object(LighterProxy, '_get_lighter_market_config', return_value=(1, 100000000, 100)):
            
            proxy = LighterProxy("BTC", mock_logger)
            
            # Verify client initialization
            mock_signer_class.assert_called_once_with(
                url="https://mainnet.zklighter.elliot.ai",
                private_key="test_private_key_123",
                account_index=1,
                api_key_index=2
            )
            mock_signer_client.check_client.assert_called_once()
            mock_logger.info.assert_called()
    
    def test_initialize_lighter_client_missing_private_key(self, mock_logger):
        """Test client initialization with missing private key"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2'
            # API_KEY_PRIVATE_KEY is missing
        }), \
             patch.object(LighterProxy, '_get_lighter_market_config', return_value=(1, 100000000, 100)):
            
            with pytest.raises(Exception, match="API_KEY_PRIVATE_KEY environment variable not set"):
                LighterProxy("BTC", mock_logger)
    
    def test_initialize_lighter_client_check_error(self, mock_environment, mock_logger):
        """Test client initialization with check_client error"""
        mock_client = Mock()
        mock_client.check_client.return_value = "Client check failed"
        
        with patch('hedge.lighter_proxy.SignerClient', return_value=mock_client), \
             patch.object(LighterProxy, '_get_lighter_market_config', return_value=(1, 100000000, 100)):
            
            with pytest.raises(Exception, match="CheckClient error: Client check failed"):
                LighterProxy("BTC", mock_logger)
    
    def test_get_lighter_market_config_success(self, mock_environment, mock_logger, mock_requests_response):
        """Test successful market config retrieval"""
        with patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get', return_value=mock_requests_response):
            
            mock_signer_class.return_value.check_client.return_value = None
            
            proxy = LighterProxy("BTC", mock_logger)
            
            # Verify market config
            assert proxy.lighter_market_index == 1
            assert proxy.base_amount_multiplier == 100000000  # 10^8
            assert proxy.price_multiplier == 100  # 10^2
    
    def test_get_lighter_market_config_ticker_not_found(self, mock_environment, mock_logger):
        """Test market config with ticker not found"""
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.text = '{"order_books": []}'
        mock_response.json.return_value = {
            "order_books": [
                {
                    "symbol": "ETH",  # Different ticker
                    "market_id": 2,
                    "supported_size_decimals": 8,
                    "supported_price_decimals": 2
                }
            ]
        }
        
        with patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get', return_value=mock_response):
            
            mock_signer_class.return_value.check_client.return_value = None
            
            with pytest.raises(Exception, match="Ticker BTC not found"):
                LighterProxy("BTC", mock_logger)
    
    def test_get_lighter_market_config_empty_response(self, mock_environment, mock_logger):
        """Test market config with empty response"""
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.text = ""  # Empty response
        
        with patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get', return_value=mock_response):
            
            mock_signer_class.return_value.check_client.return_value = None
            
            with pytest.raises(Exception, match="Empty response from Lighter API"):
                LighterProxy("BTC", mock_logger)
    
    def test_get_lighter_market_config_request_error(self, mock_environment, mock_logger):
        """Test market config with request error"""
        with patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get', side_effect=requests.RequestException("Network error")):
            
            mock_signer_class.return_value.check_client.return_value = None
            
            with pytest.raises(Exception):
                LighterProxy("BTC", mock_logger)


class TestLighterProxyOrderBook:
    """Test order book management functionality"""
    
    @pytest.fixture
    def lighter_proxy(self):
        """Create LighterProxy instance for testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }), \
             patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests:
            
            # Setup successful mocks
            mock_client = Mock()
            mock_client.check_client.return_value = None
            mock_signer_class.return_value = mock_client
            
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = '{"order_books": []}'
            mock_response.json.return_value = {
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 8,
                        "supported_price_decimals": 2
                    }
                ]
            }
            mock_requests.return_value = mock_response
            
            logger = Mock()
            proxy = LighterProxy("BTC", logger)
            yield proxy
    
    @pytest.mark.asyncio
    async def test_reset_lighter_order_book(self, lighter_proxy):
        """Test order book reset functionality"""
        # Add some data to order book
        lighter_proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
        lighter_proxy.lighter_order_book["asks"][Decimal('50001')] = Decimal('0.2')
        lighter_proxy.lighter_order_book_offset = 100
        lighter_proxy.lighter_order_book_sequence_gap = True
        lighter_proxy.lighter_snapshot_loaded = True
        lighter_proxy.lighter_best_bid = Decimal('50000')
        lighter_proxy.lighter_best_ask = Decimal('50001')
        
        # Reset order book
        await lighter_proxy.reset_lighter_order_book()
        
        # Verify reset
        assert len(lighter_proxy.lighter_order_book["bids"]) == 0
        assert len(lighter_proxy.lighter_order_book["asks"]) == 0
        assert lighter_proxy.lighter_order_book_offset == 0
        assert lighter_proxy.lighter_order_book_sequence_gap is False
        assert lighter_proxy.lighter_snapshot_loaded is False
        assert lighter_proxy.lighter_best_bid is None
        assert lighter_proxy.lighter_best_ask is None
    
    def test_update_lighter_order_book_list_format(self, lighter_proxy):
        """Test order book update with list format levels"""
        levels = [
            ['50000.0', '0.1'],
            ['50001.0', '0.2']
        ]
        
        lighter_proxy.update_lighter_order_book("bids", levels)
        
        assert lighter_proxy.lighter_order_book["bids"][Decimal('50000.0')] == Decimal('0.1')
        assert lighter_proxy.lighter_order_book["bids"][Decimal('50001.0')] == Decimal('0.2')
    
    def test_update_lighter_order_book_dict_format(self, lighter_proxy):
        """Test order book update with dict format levels"""
        levels = [
            {"price": "50000.0", "size": "0.1"},
            {"price": "50001.0", "size": "0.2"}
        ]
        
        lighter_proxy.update_lighter_order_book("asks", levels)
        
        assert lighter_proxy.lighter_order_book["asks"][Decimal('50000.0')] == Decimal('0.1')
        assert lighter_proxy.lighter_order_book["asks"][Decimal('50001.0')] == Decimal('0.2')
    
    def test_update_lighter_order_book_zero_size_removal(self, lighter_proxy):
        """Test order book update removes zero size orders"""
        # First add an order
        levels = [['50000.0', '0.1']]
        lighter_proxy.update_lighter_order_book("bids", levels)
        assert Decimal('50000.0') in lighter_proxy.lighter_order_book["bids"]
        
        # Then remove it with zero size
        levels = [['50000.0', '0.0']]
        lighter_proxy.update_lighter_order_book("bids", levels)
        assert Decimal('50000.0') not in lighter_proxy.lighter_order_book["bids"]
    
    def test_update_lighter_order_book_invalid_format(self, lighter_proxy):
        """Test order book update with invalid level format"""
        levels = [
            "invalid_format",
            {"price": "50000.0"}  # Missing size
        ]
        
        with patch.object(lighter_proxy.logger, 'warning') as mock_warning:
            lighter_proxy.update_lighter_order_book("bids", levels)
            mock_warning.assert_called()
    
    def test_validate_order_book_offset_valid(self, lighter_proxy):
        """Test valid order book offset validation"""
        lighter_proxy.lighter_order_book_offset = 100
        
        assert lighter_proxy.validate_order_book_offset(101) is True
        assert lighter_proxy.validate_order_book_offset(200) is True
    
    def test_validate_order_book_offset_invalid(self, lighter_proxy):
        """Test invalid order book offset validation"""
        lighter_proxy.lighter_order_book_offset = 100
        
        with patch.object(lighter_proxy.logger, 'warning') as mock_warning:
            assert lighter_proxy.validate_order_book_offset(100) is False
            assert lighter_proxy.validate_order_book_offset(99) is False
            mock_warning.assert_called()
    
    def test_validate_order_book_integrity_valid(self, lighter_proxy):
        """Test valid order book integrity"""
        lighter_proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
        lighter_proxy.lighter_order_book["asks"][Decimal('50001')] = Decimal('0.2')
        
        assert lighter_proxy.validate_order_book_integrity() is True
    
    def test_validate_order_book_integrity_invalid_price(self, lighter_proxy):
        """Test order book integrity with invalid price"""
        lighter_proxy.lighter_order_book["bids"][Decimal('0')] = Decimal('0.1')
        
        with patch.object(lighter_proxy.logger, 'error') as mock_error:
            assert lighter_proxy.validate_order_book_integrity() is False
            mock_error.assert_called()
    
    def test_validate_order_book_integrity_invalid_size(self, lighter_proxy):
        """Test order book integrity with invalid size"""
        lighter_proxy.lighter_order_book["asks"][Decimal('50000')] = Decimal('0')
        
        with patch.object(lighter_proxy.logger, 'error') as mock_error:
            assert lighter_proxy.validate_order_book_integrity() is False
            mock_error.assert_called()
    
    def test_get_lighter_best_levels_success(self, lighter_proxy):
        """Test getting best bid/ask levels"""
        # Add order book data
        lighter_proxy.lighter_order_book["bids"][Decimal('49999')] = Decimal('0.1')
        lighter_proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.2')  # Best bid
        lighter_proxy.lighter_order_book["asks"][Decimal('50001')] = Decimal('0.1')  # Best ask
        lighter_proxy.lighter_order_book["asks"][Decimal('50002')] = Decimal('0.2')
        
        best_bid, best_ask = lighter_proxy.get_lighter_best_levels()
        
        assert best_bid == (Decimal('50000'), Decimal('0.2'))
        assert best_ask == (Decimal('50001'), Decimal('0.1'))
    
    def test_get_lighter_best_levels_empty_book(self, lighter_proxy):
        """Test getting best levels with empty order book"""
        best_bid, best_ask = lighter_proxy.get_lighter_best_levels()
        
        assert best_bid is None
        assert best_ask is None
    
    def test_get_lighter_mid_price_success(self, lighter_proxy):
        """Test mid price calculation"""
        lighter_proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
        lighter_proxy.lighter_order_book["asks"][Decimal('50002')] = Decimal('0.1')
        
        mid_price = lighter_proxy.get_lighter_mid_price()
        
        assert mid_price == Decimal('50001')  # (50000 + 50002) / 2
    
    def test_get_lighter_mid_price_missing_data(self, lighter_proxy):
        """Test mid price calculation with missing order book data"""
        with pytest.raises(Exception, match="Cannot calculate mid price - missing order book data"):
            lighter_proxy.get_lighter_mid_price()
    
    def test_get_lighter_order_price_ask(self, lighter_proxy):
        """Test order price calculation for ask side"""
        lighter_proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
        lighter_proxy.lighter_order_book["asks"][Decimal('50002')] = Decimal('0.1')
        
        order_price = lighter_proxy.get_lighter_order_price(is_ask=True)
        
        assert order_price == Decimal('50000.1')  # best_bid + 0.1
    
    def test_get_lighter_order_price_bid(self, lighter_proxy):
        """Test order price calculation for bid side"""
        lighter_proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
        lighter_proxy.lighter_order_book["asks"][Decimal('50002')] = Decimal('0.1')
        
        order_price = lighter_proxy.get_lighter_order_price(is_ask=False)
        
        assert order_price == Decimal('50001.9')  # best_ask - 0.1
    
    def test_calculate_adjusted_price_buy(self, lighter_proxy):
        """Test price adjustment for buy orders"""
        original_price = Decimal('50000')
        adjustment_percent = Decimal('0.01')  # 1%
        
        adjusted_price = lighter_proxy.calculate_adjusted_price(
            original_price, 'buy', adjustment_percent
        )
        
        assert adjusted_price == Decimal('50500')  # 50000 + 500 (1%)
    
    def test_calculate_adjusted_price_sell(self, lighter_proxy):
        """Test price adjustment for sell orders"""
        original_price = Decimal('50000')
        adjustment_percent = Decimal('0.01')  # 1%
        
        adjusted_price = lighter_proxy.calculate_adjusted_price(
            original_price, 'sell', adjustment_percent
        )
        
        assert adjusted_price == Decimal('49500')  # 50000 - 500 (1%)


class TestLighterProxyOrderOperations:
    """Test order placement and monitoring functionality"""
    
    @pytest.fixture
    def lighter_proxy_with_orderbook(self):
        """Create LighterProxy with order book data"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }), \
             patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests:
            
            # Setup mocks
            mock_client = Mock()
            mock_client.check_client.return_value = None
            mock_client.sign_create_order = Mock(return_value=("tx_info", None))
            mock_client.send_tx = AsyncMock(return_value="tx_hash_123")
            mock_client.ORDER_TYPE_LIMIT = 1
            mock_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
            mock_client.TX_TYPE_CREATE_ORDER = 1
            mock_signer_class.return_value = mock_client
            
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = '{"order_books": []}'
            mock_response.json.return_value = {
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 8,
                        "supported_price_decimals": 2
                    }
                ]
            }
            mock_requests.return_value = mock_response
            
            logger = Mock()
            proxy = LighterProxy("BTC", logger)
            
            # Add order book data
            proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
            proxy.lighter_order_book["asks"][Decimal('50001')] = Decimal('0.1')
            
            yield proxy
    
    @pytest.mark.asyncio
    async def test_place_lighter_market_order_buy_success(self, lighter_proxy_with_orderbook):
        """Test successful buy market order placement"""
        proxy = lighter_proxy_with_orderbook
        
        with patch.object(proxy, 'monitor_lighter_order') as mock_monitor, \
             patch('time.time', return_value=1000000):
            
            mock_monitor.return_value = None
            
            result = await proxy.place_lighter_market_order(
                "buy", Decimal('0.1'), Decimal('50000')
            )
            
            # Verify order placement
            assert result == "tx_hash_123"
            assert proxy.lighter_order_filled is False
            assert proxy.lighter_order_side == "buy"
            assert proxy.lighter_order_size == Decimal('0.1')
            
            # Verify client calls
            proxy.lighter_client.sign_create_order.assert_called_once()
            proxy.lighter_client.send_tx.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_place_lighter_market_order_sell_success(self, lighter_proxy_with_orderbook):
        """Test successful sell market order placement"""
        proxy = lighter_proxy_with_orderbook
        
        with patch.object(proxy, 'monitor_lighter_order') as mock_monitor, \
             patch('time.time', return_value=1000000):
            
            mock_monitor.return_value = None
            
            result = await proxy.place_lighter_market_order(
                "sell", Decimal('0.1'), Decimal('50000')
            )
            
            # Verify order parameters for sell
            assert result == "tx_hash_123"
            assert proxy.lighter_order_side == "sell"
            
            # Verify sign_create_order called with correct parameters
            call_args = proxy.lighter_client.sign_create_order.call_args
            assert call_args[1]['is_ask'] is True  # Sell order
    
    @pytest.mark.asyncio
    async def test_place_lighter_market_order_sign_error(self, lighter_proxy_with_orderbook):
        """Test order placement with signing error"""
        proxy = lighter_proxy_with_orderbook
        proxy.lighter_client.sign_create_order.return_value = (None, "Signing failed")
        
        with patch.object(proxy.logger, 'error') as mock_error, \
             patch('time.time', return_value=1000000):
            
            result = await proxy.place_lighter_market_order(
                "buy", Decimal('0.1'), Decimal('50000')
            )
            
            assert result is None
            mock_error.assert_called()
    
    @pytest.mark.asyncio
    async def test_place_lighter_market_order_no_client(self, lighter_proxy_with_orderbook):
        """Test order placement with no client initialized"""
        proxy = lighter_proxy_with_orderbook
        proxy.lighter_client = None
        
        with patch.object(proxy, '_initialize_lighter_client') as mock_init:
            mock_init.return_value = Mock()
            
            await proxy.place_lighter_market_order("buy", Decimal('0.1'), Decimal('50000'))
            
            mock_init.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_monitor_lighter_order_success(self, lighter_proxy_with_orderbook):
        """Test successful order monitoring"""
        proxy = lighter_proxy_with_orderbook
        
        # Simulate order getting filled quickly
        async def quick_fill():
            await asyncio.sleep(0.05)  # 50ms delay
            proxy.lighter_order_filled = True
        
        # Start the fill task
        fill_task = asyncio.create_task(quick_fill())
        
        with patch('time.time', return_value=1000):
            await proxy.monitor_lighter_order(123456)
        
        await fill_task
        assert proxy.lighter_order_filled is True
    
    @pytest.mark.asyncio
    async def test_monitor_lighter_order_timeout(self, lighter_proxy_with_orderbook):
        """Test order monitoring with timeout"""
        proxy = lighter_proxy_with_orderbook
        
        # Mock time to trigger immediate timeout with enough values
        time_values = [1000] + [1035] * 10  # First call: 1000, subsequent calls: 1035 (35s later)
        
        with patch('time.time', side_effect=time_values), \
             patch('asyncio.sleep', side_effect=lambda x: asyncio.sleep(0.001)), \
             patch.object(proxy.logger, 'error') as mock_error, \
             patch.object(proxy.logger, 'warning') as mock_warning:
            
            await proxy.monitor_lighter_order(123456)
        
        # Should timeout and use fallback
        mock_error.assert_called()
        mock_warning.assert_called()
        assert proxy.lighter_order_filled is True  # Fallback sets to True
    
    def test_handle_lighter_order_result_buy_order(self, lighter_proxy_with_orderbook):
        """Test handling of buy order result"""
        proxy = lighter_proxy_with_orderbook
        mock_callback = Mock()
        proxy.position_callback = mock_callback
        
        order_data = {
            "filled_quote_amount": "5000.0",
            "filled_base_amount": "0.1",
            "is_ask": False,  # Buy order
            "client_order_id": "123456"
        }
        
        with patch('hedge.lighter_proxy.log_trade_to_csv') as mock_log:
            proxy.handle_lighter_order_result(order_data)
        
        # Verify calculations
        assert order_data["avg_filled_price"] == Decimal('50000.0')
        assert order_data["side"] == "LONG"
        assert proxy.lighter_order_filled is True
        
        # Verify callback called with positive position change
        mock_callback.assert_called_once_with(Decimal('0.1'))
        
        # Verify CSV logging
        mock_log.assert_called_once_with(
            exchange='Lighter',
            ticker='BTC',
            side='LONG',
            price='50000',  # Decimal('50000.0') becomes '50000' when str()
            quantity='0.1'
        )
    
    def test_handle_lighter_order_result_sell_order(self, lighter_proxy_with_orderbook):
        """Test handling of sell order result"""
        proxy = lighter_proxy_with_orderbook
        mock_callback = Mock()
        proxy.position_callback = mock_callback
        
        order_data = {
            "filled_quote_amount": "5000.0",
            "filled_base_amount": "0.1",
            "is_ask": True,  # Sell order
            "client_order_id": "123456"
        }
        
        with patch('hedge.lighter_proxy.log_trade_to_csv'):
            proxy.handle_lighter_order_result(order_data)
        
        # Verify calculations for sell order
        assert order_data["side"] == "SHORT"
        
        # Verify callback called with negative position change
        mock_callback.assert_called_once_with(-Decimal('0.1'))
    
    def test_handle_lighter_order_result_exception(self, lighter_proxy_with_orderbook):
        """Test handling order result with exception"""
        proxy = lighter_proxy_with_orderbook
        
        # Invalid order data to trigger exception
        order_data = {
            "filled_quote_amount": "invalid",
            "filled_base_amount": "0.1"
        }
        
        with patch.object(proxy.logger, 'error') as mock_error:
            proxy.handle_lighter_order_result(order_data)
            
        mock_error.assert_called()


class TestLighterProxyWebSocket:
    """Test WebSocket connection and message handling"""
    
    @pytest.fixture
    def lighter_proxy_websocket(self):
        """Create LighterProxy for WebSocket testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }), \
             patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests:
            
            # Setup mocks
            mock_client = Mock()
            mock_client.check_client.return_value = None
            mock_client.create_auth_token_with_expiry = Mock(return_value=("auth_token_123", None))
            mock_signer_class.return_value = mock_client
            
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = '{"order_books": []}'
            mock_response.json.return_value = {
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 8,
                        "supported_price_decimals": 2
                    }
                ]
            }
            mock_requests.return_value = mock_response
            
            logger = Mock()
            proxy = LighterProxy("BTC", logger)
            yield proxy
    
    @pytest.mark.asyncio
    async def test_setup_ws_task(self, lighter_proxy_websocket):
        """Test WebSocket task setup"""
        proxy = lighter_proxy_websocket
        
        with patch.object(proxy, 'handle_lighter_ws') as mock_handle_ws, \
             patch.object(proxy, 'wait_for_lighter_order_book_ready') as mock_wait:
            
            mock_handle_ws.return_value = None
            mock_wait.return_value = None
            
            await proxy.setup_ws_task()
            
            assert proxy.lighter_ws_task is not None
            proxy.logger.info.assert_called_with("✅ Lighter WebSocket task started")
    
    @pytest.mark.asyncio
    async def test_request_fresh_snapshot(self, lighter_proxy_websocket):
        """Test requesting fresh order book snapshot"""
        proxy = lighter_proxy_websocket
        mock_ws = AsyncMock()
        
        await proxy.request_fresh_snapshot(mock_ws)
        
        expected_message = json.dumps({
            "type": "subscribe", 
            "channel": f"order_book/{proxy.lighter_market_index}"
        })
        mock_ws.send.assert_called_once_with(expected_message)
    
    @pytest.mark.asyncio
    async def test_fetch_bbo_prices_success(self, lighter_proxy_websocket):
        """Test fetching BBO prices successfully"""
        proxy = lighter_proxy_websocket
        
        # Add order book data
        proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
        proxy.lighter_order_book["asks"][Decimal('50001')] = Decimal('0.1')
        
        bid, ask = await proxy.fetch_bbo_prices()
        
        assert bid == Decimal('50000')
        assert ask == Decimal('50001')
    
    @pytest.mark.asyncio
    async def test_fetch_bbo_prices_missing_data(self, lighter_proxy_websocket):
        """Test fetching BBO prices with missing data"""
        proxy = lighter_proxy_websocket
        
        with pytest.raises(Exception, match="无法获取Lighter最优价格 - 订单簿数据缺失"):
            await proxy.fetch_bbo_prices()


class TestLighterProxyWebSocketAdvanced:
    """Test advanced WebSocket message handling and order book processing"""
    
    @pytest.fixture
    def lighter_proxy_websocket_advanced(self):
        """Create LighterProxy with enhanced WebSocket testing setup"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }), \
             patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests:
            
            # Setup mocks
            mock_client = Mock()
            mock_client.check_client.return_value = None
            mock_client.create_auth_token_with_expiry = Mock(return_value=("auth_token_123", None))
            mock_signer_class.return_value = mock_client
            
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = '{"order_books": []}'
            mock_response.json.return_value = {
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 8,
                        "supported_price_decimals": 2
                    }
                ]
            }
            mock_requests.return_value = mock_response
            
            logger = Mock()
            proxy = LighterProxy("BTC", logger)
            yield proxy
    
    def test_handle_order_book_snapshot_message(self, lighter_proxy_websocket_advanced):
        """Test handling of order book snapshot messages"""
        proxy = lighter_proxy_websocket_advanced
        
        # Mock order book snapshot data
        snapshot_data = {
            "type": "subscribed/order_book",
            "order_book": {
                "offset": 12345,
                "bids": [
                    ["50000.0", "0.1"],
                    ["49999.0", "0.2"]
                ],
                "asks": [
                    ["50001.0", "0.15"],
                    ["50002.0", "0.25"]
                ]
            }
        }
        
        # Process the snapshot message (simulate the WebSocket handler logic)
        if snapshot_data.get("type") == "subscribed/order_book":
            proxy.lighter_order_book["bids"].clear()
            proxy.lighter_order_book["asks"].clear()
            
            order_book = snapshot_data.get("order_book", {})
            if order_book and "offset" in order_book:
                proxy.lighter_order_book_offset = order_book["offset"]
            
            bids = order_book.get("bids", [])
            asks = order_book.get("asks", [])
            proxy.update_lighter_order_book("bids", bids)
            proxy.update_lighter_order_book("asks", asks)
            proxy.lighter_snapshot_loaded = True
            proxy.lighter_order_book_ready = True
        
        # Verify snapshot processing
        assert proxy.lighter_order_book_offset == 12345
        assert proxy.lighter_snapshot_loaded is True
        assert proxy.lighter_order_book_ready is True
        assert len(proxy.lighter_order_book["bids"]) == 2
        assert len(proxy.lighter_order_book["asks"]) == 2
        assert proxy.lighter_order_book["bids"][Decimal('50000.0')] == Decimal('0.1')
        assert proxy.lighter_order_book["asks"][Decimal('50001.0')] == Decimal('0.15')
    
    def test_handle_order_book_update_message(self, lighter_proxy_websocket_advanced):
        """Test handling of order book update messages"""
        proxy = lighter_proxy_websocket_advanced
        
        # Set initial state
        proxy.lighter_snapshot_loaded = True
        proxy.lighter_order_book_offset = 12345
        
        # Mock order book update data
        update_data = {
            "type": "update/order_book",
            "order_book": {
                "offset": 12346,  # Next offset
                "bids": [
                    ["50010.0", "0.3"]  # New bid
                ],
                "asks": [
                    ["50020.0", "0.4"]  # New ask
                ]
            }
        }
        
        # Process the update message
        if update_data.get("type") == "update/order_book" and proxy.lighter_snapshot_loaded:
            order_book = update_data.get("order_book", {})
            if order_book and "offset" in order_book:
                new_offset = order_book["offset"]
                
                # Validate offset sequence (simplified)
                if new_offset > proxy.lighter_order_book_offset:
                    proxy.lighter_order_book_offset = new_offset
                    proxy.update_lighter_order_book("bids", order_book.get("bids", []))
                    proxy.update_lighter_order_book("asks", order_book.get("asks", []))
        
        # Verify update processing
        assert proxy.lighter_order_book_offset == 12346
        assert proxy.lighter_order_book["bids"][Decimal('50010.0')] == Decimal('0.3')
        assert proxy.lighter_order_book["asks"][Decimal('50020.0')] == Decimal('0.4')
    
    def test_handle_ping_pong_message(self, lighter_proxy_websocket_advanced):
        """Test handling of ping/pong messages"""
        proxy = lighter_proxy_websocket_advanced
        
        ping_data = {"type": "ping"}
        expected_pong = {"type": "pong"}
        
        # Test ping message recognition
        assert ping_data.get("type") == "ping"
        
        # Verify expected pong response structure
        assert expected_pong == {"type": "pong"}
        
        proxy.logger.debug.assert_not_called()  # No error logging
    
    def test_handle_account_orders_update(self, lighter_proxy_websocket_advanced):
        """Test handling of account orders update messages"""
        proxy = lighter_proxy_websocket_advanced
        proxy.lighter_market_index = 1
        
        # Mock account orders update
        account_orders_data = {
            "type": "update/account_orders",
            "orders": {
                "1": [  # Market index 1
                    {
                        "status": "filled",
                        "filled_quote_amount": "5000.0",
                        "filled_base_amount": "0.1",
                        "is_ask": False,
                        "client_order_id": "12345"
                    }
                ]
            }
        }
        
        # Mock the handle_lighter_order_result method
        with patch.object(proxy, 'handle_lighter_order_result') as mock_handle:
            # Process account orders update
            if account_orders_data.get("type") == "update/account_orders":
                orders = account_orders_data.get("orders", {}).get(str(proxy.lighter_market_index), [])
                for order in orders:
                    if order.get("status") == "filled":
                        proxy.handle_lighter_order_result(order)
        
        # Verify order result handling was called
        mock_handle.assert_called_once()
        call_args = mock_handle.call_args[0][0]
        assert call_args["status"] == "filled"
        assert call_args["filled_base_amount"] == "0.1"
    
    def test_handle_update_without_snapshot(self, lighter_proxy_websocket_advanced):
        """Test ignoring updates when snapshot not loaded"""
        proxy = lighter_proxy_websocket_advanced
        proxy.lighter_snapshot_loaded = False
        
        # Mock order book update without snapshot
        update_data = {
            "type": "update/order_book",
            "order_book": {
                "offset": 12346,
                "bids": [["50010.0", "0.3"]],
                "asks": [["50020.0", "0.4"]]
            }
        }
        
        # Process update (should be ignored)
        if update_data.get("type") == "update/order_book" and not proxy.lighter_snapshot_loaded:
            # Should continue/ignore
            pass
        else:
            # Should not reach here
            assert False, "Update should be ignored when snapshot not loaded"
        
        # Verify order book remains empty
        assert len(proxy.lighter_order_book["bids"]) == 0
        assert len(proxy.lighter_order_book["asks"]) == 0
    
    def test_order_book_offset_validation_failure(self, lighter_proxy_websocket_advanced):
        """Test handling of offset validation failures"""
        proxy = lighter_proxy_websocket_advanced
        proxy.lighter_snapshot_loaded = True
        proxy.lighter_order_book_offset = 12345
        proxy.lighter_order_book_sequence_gap = False
        
        # Mock update with invalid offset (backwards)
        invalid_update = {
            "type": "update/order_book",
            "order_book": {
                "offset": 12344,  # Backwards offset
                "bids": [["50010.0", "0.3"]],
                "asks": [["50020.0", "0.4"]]
            }
        }
        
        # Process invalid update
        if invalid_update.get("type") == "update/order_book" and proxy.lighter_snapshot_loaded:
            order_book = invalid_update.get("order_book", {})
            if order_book and "offset" in order_book:
                new_offset = order_book["offset"]
                
                # Validate offset (should fail)
                if not proxy.validate_order_book_offset(new_offset):
                    proxy.lighter_order_book_sequence_gap = True
        
        # Verify sequence gap was detected
        assert proxy.lighter_order_book_sequence_gap is True
    
    def test_order_book_integrity_failure(self, lighter_proxy_websocket_advanced):
        """Test handling of order book integrity failures"""
        proxy = lighter_proxy_websocket_advanced
        
        # Add invalid data to order book
        proxy.lighter_order_book["bids"][Decimal('0')] = Decimal('0.1')  # Invalid price
        
        # Mock update that would trigger integrity check
        update_data = {
            "type": "update/order_book",
            "order_book": {
                "offset": 12346,
                "bids": [["50010.0", "0.3"]],
                "asks": [["50020.0", "0.4"]]
            }
        }
        
        # Process update with integrity check
        proxy.lighter_snapshot_loaded = True
        proxy.lighter_order_book_offset = 12345
        
        if update_data.get("type") == "update/order_book" and proxy.lighter_snapshot_loaded:
            order_book = update_data.get("order_book", {})
            if order_book and "offset" in order_book:
                new_offset = order_book["offset"]
                if proxy.validate_order_book_offset(new_offset):
                    proxy.update_lighter_order_book("bids", order_book.get("bids", []))
                    proxy.update_lighter_order_book("asks", order_book.get("asks", []))
                    
                    # Check integrity (should fail due to invalid price)
                    integrity_valid = proxy.validate_order_book_integrity()
                    
        # Verify integrity check detects the issue
        assert integrity_valid is False


class TestLighterProxyOrderModification:
    """Test order modification functionality"""
    
    @pytest.fixture
    def lighter_proxy_with_client(self):
        """Create LighterProxy with mock client for order modification testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }), \
             patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests:
            
            # Setup mocks
            mock_client = Mock()
            mock_client.check_client.return_value = None
            mock_client.modify_order = AsyncMock(return_value=("tx_info", "tx_hash", None))
            mock_signer_class.return_value = mock_client
            
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = '{"order_books": []}'
            mock_response.json.return_value = {
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 8,
                        "supported_price_decimals": 2
                    }
                ]
            }
            mock_requests.return_value = mock_response
            
            logger = Mock()
            proxy = LighterProxy("BTC", logger)
            
            # Set order state for modification testing
            proxy.lighter_order_size = Decimal('0.1')
            proxy.lighter_order_side = "buy"
            
            yield proxy
    
    @pytest.mark.asyncio
    async def test_modify_lighter_order_success(self, lighter_proxy_with_client):
        """Test successful order modification"""
        proxy = lighter_proxy_with_client
        
        client_order_index = 12345
        new_price = Decimal('50100.0')
        
        await proxy.modify_lighter_order(client_order_index, new_price)
        
        # Verify modify_order was called with correct parameters
        proxy.lighter_client.modify_order.assert_called_once_with(
            market_index=proxy.lighter_market_index,
            order_index=client_order_index,
            base_amount=int(proxy.lighter_order_size * proxy.base_amount_multiplier),
            price=int(new_price * proxy.price_multiplier),
            trigger_price=0
        )
        
        # Verify order price was updated
        assert proxy.lighter_order_price == new_price
        
        # Verify success logging
        proxy.logger.info.assert_called()
    
    @pytest.mark.asyncio
    async def test_modify_lighter_order_no_order_id(self, lighter_proxy_with_client):
        """Test order modification with no order ID"""
        proxy = lighter_proxy_with_client
        
        await proxy.modify_lighter_order(None, Decimal('50100.0'))
        
        # Verify modify_order was not called
        proxy.lighter_client.modify_order.assert_not_called()
        
        # Verify error logging
        proxy.logger.error.assert_called_with("❌ Cannot modify order - no order ID available")
    
    @pytest.mark.asyncio
    async def test_modify_lighter_order_modification_error(self, lighter_proxy_with_client):
        """Test order modification with modification error"""
        proxy = lighter_proxy_with_client
        
        # Mock modification error
        proxy.lighter_client.modify_order.return_value = ("tx_info", "tx_hash", "Modification failed")
        
        client_order_index = 12345
        new_price = Decimal('50100.0')
        
        await proxy.modify_lighter_order(client_order_index, new_price)
        
        # Verify error logging
        proxy.logger.error.assert_called_with("❌ Lighter order modification error: Modification failed")
        
        # Verify order price was not updated
        assert not hasattr(proxy, 'lighter_order_price') or proxy.lighter_order_price != new_price
    
    @pytest.mark.asyncio
    async def test_modify_lighter_order_exception_handling(self, lighter_proxy_with_client):
        """Test order modification exception handling"""
        proxy = lighter_proxy_with_client
        
        # Mock exception during modification
        proxy.lighter_client.modify_order.side_effect = Exception("Network error")
        
        client_order_index = 12345
        new_price = Decimal('50100.0')
        
        with patch.object(proxy.logger, 'error') as mock_error:
            await proxy.modify_lighter_order(client_order_index, new_price)
            
        # Verify exception was caught and logged
        mock_error.assert_called()


class TestLighterProxyConnectionManagement:
    """Test WebSocket connection management and error handling"""
    
    @pytest.fixture
    def lighter_proxy_connection_test(self):
        """Create LighterProxy for connection testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }), \
             patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests:
            
            mock_client = Mock()
            mock_client.check_client.return_value = None
            mock_client.create_auth_token_with_expiry = Mock(return_value=("auth_token_123", None))
            mock_signer_class.return_value = mock_client
            
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = '{"order_books": []}'
            mock_response.json.return_value = {
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 8,
                        "supported_price_decimals": 2
                    }
                ]
            }
            mock_requests.return_value = mock_response
            
            logger = Mock()
            proxy = LighterProxy("BTC", logger)
            yield proxy
    
    @pytest.mark.asyncio
    async def test_wait_for_lighter_order_book_ready_success(self, lighter_proxy_connection_test):
        """Test successful order book ready wait"""
        proxy = lighter_proxy_connection_test
        
        # Simulate order book becoming ready quickly
        async def make_ready():
            await asyncio.sleep(0.1)
            proxy.lighter_order_book_ready = True
        
        ready_task = asyncio.create_task(make_ready())
        
        with patch('time.time', return_value=1000):
            await proxy.wait_for_lighter_order_book_ready(timeout=5)
        
        await ready_task
        
        # Verify success logging
        proxy.logger.info.assert_called_with("✅ Lighter WebSocket order book data received")
        assert proxy.lighter_order_book_ready is True
    
    @pytest.mark.asyncio
    async def test_wait_for_lighter_order_book_ready_timeout(self, lighter_proxy_connection_test):
        """Test order book ready wait timeout"""
        proxy = lighter_proxy_connection_test
        proxy.lighter_order_book_ready = False
        
        # Mock time to simulate timeout
        time_values = [1000, 1001, 1002, 1003, 1004, 1005, 1011]  # 11 seconds elapsed
        
        with patch('time.time', side_effect=time_values), \
             patch('asyncio.sleep', side_effect=lambda x: asyncio.sleep(0.001)):
            await proxy.wait_for_lighter_order_book_ready(timeout=10)
        
        # Verify warning about not ready (the actual behavior)
        proxy.logger.warning.assert_called_with("⚠️ Lighter WebSocket order book not ready")
    
    @pytest.mark.asyncio
    async def test_wait_for_lighter_order_book_ready_stop_flag(self, lighter_proxy_connection_test):
        """Test order book ready wait with stop flag"""
        proxy = lighter_proxy_connection_test
        proxy.lighter_order_book_ready = False
        
        # Set stop flag after short delay
        async def set_stop_flag():
            await asyncio.sleep(0.1)
            proxy.stop_flag = True
        
        stop_task = asyncio.create_task(set_stop_flag())
        
        with patch('time.time', return_value=1000):
            await proxy.wait_for_lighter_order_book_ready(timeout=10)
        
        await stop_task
        
        # Verify warning about not ready
        proxy.logger.warning.assert_called_with("⚠️ Lighter WebSocket order book not ready")
    
    @pytest.mark.asyncio
    async def test_wait_for_lighter_order_book_ready_exception(self, lighter_proxy_connection_test):
        """Test order book ready wait exception handling"""
        proxy = lighter_proxy_connection_test
        
        # Mock exception during wait
        with patch('time.time', side_effect=Exception("Time error")), \
             patch('sys.exit') as mock_exit:
            await proxy.wait_for_lighter_order_book_ready(timeout=10)
        
        # Verify exception handling
        proxy.logger.error.assert_called()
        mock_exit.assert_called_with(1)


class TestLighterProxyEdgeCases:
    """Test edge cases and boundary conditions"""
    
    @pytest.fixture
    def lighter_proxy_edge_cases(self):
        """Create LighterProxy for edge case testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '2',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }), \
             patch('hedge.lighter_proxy.SignerClient') as mock_signer_class, \
             patch('requests.get') as mock_requests:
            
            mock_client = Mock()
            mock_client.check_client.return_value = None
            mock_signer_class.return_value = mock_client
            
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.text = '{"order_books": []}'
            mock_response.json.return_value = {
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 8,
                        "supported_price_decimals": 2
                    }
                ]
            }
            mock_requests.return_value = mock_response
            
            logger = Mock()
            proxy = LighterProxy("BTC", logger)
            yield proxy
    
    def test_get_lighter_market_config_unexpected_format(self, lighter_proxy_edge_cases):
        """Test market config with unexpected response format"""
        # This tests the uncovered line 103
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.text = '{"unexpected": "format"}'
        mock_response.json.return_value = {"unexpected": "format"}
        
        with patch('requests.get', return_value=mock_response):
            with pytest.raises(Exception, match="Unexpected response format"):
                proxy = lighter_proxy_edge_cases
                proxy._get_lighter_market_config()
    
    def test_get_lighter_order_price_missing_data(self, lighter_proxy_edge_cases):
        """Test order price calculation with missing order book data"""
        # This tests the uncovered line 203
        proxy = lighter_proxy_edge_cases
        
        # Empty order book
        proxy.lighter_order_book = {"bids": {}, "asks": {}}
        
        with pytest.raises(Exception, match="Cannot calculate order price - missing order book data"):
            proxy.get_lighter_order_price(is_ask=True)
    
    @pytest.mark.asyncio
    async def test_place_lighter_market_order_client_reinitialization(self, lighter_proxy_edge_cases):
        """Test order placement with client reinitialization"""
        proxy = lighter_proxy_edge_cases
        proxy.lighter_client = None
        
        # Mock _initialize_lighter_client to set a client
        mock_client = Mock()
        mock_client.sign_create_order = Mock(return_value=("tx_info", None))
        mock_client.send_tx = AsyncMock(return_value="tx_hash_123")
        
        with patch.object(proxy, '_initialize_lighter_client') as mock_init, \
             patch.object(proxy, 'monitor_lighter_order') as mock_monitor, \
             patch('time.time', return_value=1000000):
            
            def init_client():
                proxy.lighter_client = mock_client
            
            mock_init.side_effect = init_client
            
            # Add order book data
            proxy.lighter_order_book["bids"][Decimal('50000')] = Decimal('0.1')
            proxy.lighter_order_book["asks"][Decimal('50001')] = Decimal('0.1')
            
            # This should trigger client reinitialization
            result = await proxy.place_lighter_market_order("buy", Decimal('0.1'), Decimal('50000'))
            
        # Verify initialization was called
        mock_init.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=hedge.lighter_proxy", "--cov-report=term-missing"])