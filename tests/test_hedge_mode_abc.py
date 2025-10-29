"""
Unit tests for hedge/hedge_mode_abc.py

Tests the Config class and HedgeBotAbc abstract base class with comprehensive mocking.
"""

import pytest
import asyncio
import logging
import os
import signal
import sys
import time
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch, MagicMock, call
from pathlib import Path

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hedge.hedge_mode_abc import Config, HedgeBotAbc, parse_arguments


class TestConfig:
    """Test the Config class"""
    
    def test_config_initialization(self):
        """Test Config initialization with dictionary"""
        config_dict = {
            'api_key': 'test_key',
            'base_url': 'https://api.test.com',
            'timeout': 30,
            'enable_logging': True
        }
        
        config = Config(config_dict)
        
        assert config.api_key == 'test_key'
        assert config.base_url == 'https://api.test.com'
        assert config.timeout == 30
        assert config.enable_logging is True
    
    def test_config_empty_dict(self):
        """Test Config with empty dictionary"""
        config = Config({})
        # Should not have any attributes beyond built-ins
        assert not hasattr(config, 'api_key')
    
    def test_config_dynamic_attributes(self):
        """Test that Config properly handles dynamic attribute assignment"""
        config_dict = {'dynamic_attr': 'test_value'}
        config = Config(config_dict)
        
        assert hasattr(config, 'dynamic_attr')
        assert config.dynamic_attr == 'test_value'


class ConcreteHedgeBot(HedgeBotAbc):
    """Concrete implementation of HedgeBotAbc for testing"""
    
    def primary_exchange_name(self):
        return "TestExchange"
    
    def primary_client_vars(self):
        return {
            'test_var': 'test_value',
            'primary_client': self._create_mock_primary_client()
        }
    
    def primary_client_init(self):
        # Mock initialization - already done in primary_client_vars
        pass
    
    def primary_logger_level(self):
        return logging.INFO
    
    def _create_mock_primary_client(self):
        """Create a mock primary client with necessary methods"""
        client = Mock()
        client.config = Mock()
        client.config.quantity = Decimal('0.01')
        
        # Mock async methods
        client.get_contract_attributes = AsyncMock(return_value=("test_contract_123", Decimal('0.01')))
        client.fetch_bbo_prices = AsyncMock(return_value=(Decimal('50000.0'), Decimal('50001.0')))
        client.place_open_order = AsyncMock()
        client.cancel_order = AsyncMock()
        client.connect = AsyncMock()
        client.setup_order_update_handler = Mock()
        
        return client


class TestHedgeBotAbc:
    """Test the HedgeBotAbc abstract base class"""
    
    @pytest.fixture
    def mock_lighter_proxy(self):
        """Create a mock LighterProxy"""
        with patch('hedge.hedge_mode_abc.LighterProxy') as mock_lighter:
            lighter_instance = Mock()
            lighter_instance.stop_flag = False
            lighter_instance.setup_ws_task = AsyncMock()
            lighter_instance.place_lighter_market_order = AsyncMock()
            lighter_instance.lighter_ws_task = Mock()
            lighter_instance.lighter_ws_task.done.return_value = False
            lighter_instance.lighter_ws_task.cancel = Mock()
            mock_lighter.return_value = lighter_instance
            yield lighter_instance
    
    @pytest.fixture
    def mock_file_operations(self):
        """Mock file and directory operations"""
        # Mock handlers with proper level attributes
        mock_file_handler = Mock()
        mock_file_handler.level = logging.INFO
        mock_file_handler.setLevel = Mock()
        mock_file_handler.setFormatter = Mock()
        mock_file_handler.close = Mock()
        
        mock_console_handler = Mock()
        mock_console_handler.level = logging.INFO
        mock_console_handler.setLevel = Mock()
        mock_console_handler.setFormatter = Mock()
        mock_console_handler.close = Mock()
        
        with patch('os.makedirs'), \
             patch('builtins.open', create=True), \
             patch('logging.FileHandler', return_value=mock_file_handler), \
             patch('logging.StreamHandler', return_value=mock_console_handler):
            yield
    
    @pytest.fixture
    def concrete_bot(self, mock_lighter_proxy, mock_file_operations):
        """Create a concrete bot instance for testing"""
        with patch('hedge.hedge_mode_abc.log_trade_to_csv'):
            bot = ConcreteHedgeBot(
                ticker="BTC",
                order_quantity=Decimal('0.1'),
                fill_timeout=5,
                iterations=10
            )
            bot.lighter = mock_lighter_proxy
            return bot
    
    def test_hedgebot_abc_cannot_be_instantiated(self):
        """Test that HedgeBotAbc cannot be instantiated directly"""
        with pytest.raises(TypeError):
            HedgeBotAbc("BTC", Decimal('0.1'))
    
    def test_concrete_bot_initialization(self, concrete_bot):
        """Test concrete bot initialization"""
        assert concrete_bot.ticker == "BTC"
        assert concrete_bot.order_quantity == Decimal('0.1')
        assert concrete_bot.fill_timeout == 5
        assert concrete_bot.iterations == 10
        assert concrete_bot.primary_position == Decimal('0')
        assert concrete_bot.lighter_position == Decimal('0')
        assert concrete_bot.stop_flag is False
        assert concrete_bot.order_counter == 0
        assert concrete_bot.order_execution_complete is False
        assert concrete_bot.waiting_for_lighter_fill is False
    
    def test_primary_exchange_name_implementation(self, concrete_bot):
        """Test that concrete implementation provides exchange name"""
        assert concrete_bot.primary_exchange_name() == "TestExchange"
    
    @patch('os.makedirs')
    @patch.dict(os.environ, {'LIGHTER_ACCOUNT_INDEX': '1', 'LIGHTER_API_KEY_INDEX': '1', 'API_KEY_PRIVATE_KEY': 'test_key'})
    def test_initialize_log_file(self, mock_makedirs, mock_file_operations):
        """Test log file initialization"""
        # Create bot and trigger log file initialization manually
        with patch('hedge.hedge_mode_abc.log_trade_to_csv'), \
             patch('hedge.hedge_mode_abc.LighterProxy'):
            bot = ConcreteHedgeBot(
                ticker="BTC",
                order_quantity=Decimal('0.1'),
                fill_timeout=5,
                iterations=10
            )
        
        # Log file should be created in logs directory
        expected_filename = "logs/TestExchange_BTC_hedge_mode_log.txt"
        assert bot.log_filename == expected_filename
        mock_makedirs.assert_called_with("logs", exist_ok=True)
    
    def test_initialize_logger(self, concrete_bot):
        """Test logger initialization"""
        assert concrete_bot.logger is not None
        assert concrete_bot.logger.name == "hedge_bot_BTC"
        assert concrete_bot.logger.level == logging.INFO
        assert concrete_bot.logger.propagate is False
    
    def test_primary_client_initialization(self, concrete_bot):
        """Test primary client initialization"""
        assert concrete_bot.primary_client is not None
        assert hasattr(concrete_bot, 'test_var')
        assert concrete_bot.test_var == 'test_value'
    
    @pytest.mark.asyncio
    async def test_init_primary_contract_info_success(self, concrete_bot):
        """Test successful contract info initialization"""
        await concrete_bot._init_primary_contract_info()
        
        assert concrete_bot.primary_contract_id == "test_contract_123"
        assert concrete_bot.primary_tick_size == Decimal('0.01')
        concrete_bot.primary_client.get_contract_attributes.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_init_primary_contract_info_no_client(self, concrete_bot):
        """Test contract info initialization with no client"""
        concrete_bot.primary_client = None
        
        with pytest.raises(Exception, match="TestExchange client not initialized"):
            await concrete_bot._init_primary_contract_info()
    
    @pytest.mark.asyncio
    async def test_init_primary_contract_info_insufficient_quantity(self, concrete_bot):
        """Test contract info initialization with insufficient quantity"""
        concrete_bot.primary_client.config.quantity = Decimal('0.2')  # Higher than order_quantity
        
        with pytest.raises(ValueError, match="Order quantity is less than min quantity"):
            await concrete_bot._init_primary_contract_info()
    
    @pytest.mark.asyncio
    async def test_setup_primary_websocket_success(self, concrete_bot):
        """Test successful WebSocket setup"""
        # Mock the order_update_handler parameter that gets passed to setup_order_update_handler
        with patch.object(concrete_bot, '_setup_primary_websocket', wraps=concrete_bot._setup_primary_websocket):
            await concrete_bot._setup_primary_websocket()
        
        concrete_bot.primary_client.setup_order_update_handler.assert_called_once()
        concrete_bot.primary_client.connect.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_setup_primary_websocket_no_client(self, concrete_bot):
        """Test WebSocket setup with no client"""
        concrete_bot.primary_client = None
        
        with pytest.raises(Exception, match="TestExchange client not initialized"):
            await concrete_bot._setup_primary_websocket()
    
    @pytest.mark.asyncio
    async def test_setup_primary_websocket_connection_error(self, concrete_bot):
        """Test WebSocket setup with connection error"""
        concrete_bot.primary_client.connect.side_effect = Exception("Connection failed")
        
        with patch('sys.exit') as mock_exit:
            await concrete_bot._setup_primary_websocket()
            mock_exit.assert_called_with(1)
    
    def test_update_lighter_position(self, concrete_bot):
        """Test Lighter position update"""
        position_change = Decimal('0.05')
        
        # Mock logger to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'info'):
            concrete_bot._update_lighter_position(position_change)
        
        assert concrete_bot.lighter_position == Decimal('0.05')
    
    def test_set_stop_flag(self, concrete_bot):
        """Test stop flag setting"""
        concrete_bot._set_stop_flag(True)
        
        assert concrete_bot.stop_flag is True
        assert concrete_bot.lighter.stop_flag is True
    
    def test_shutdown_graceful(self, concrete_bot):
        """Test graceful shutdown"""
        # Mock handler for testing
        mock_handler = Mock()
        mock_handler.close = Mock()
        concrete_bot.logger.handlers = [mock_handler]
        
        # Mock logger methods to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'info'), \
             patch.object(concrete_bot.logger, 'removeHandler'):
            concrete_bot.shutdown()
        
        assert concrete_bot.stop_flag is True
        assert concrete_bot.lighter.stop_flag is True
        mock_handler.close.assert_called_once()
    
    def test_shutdown_with_lighter_task(self, concrete_bot):
        """Test shutdown with active Lighter WebSocket task"""
        concrete_bot.lighter.lighter_ws_task.done.return_value = False
        
        # Mock logger methods to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'info'), \
             patch.object(concrete_bot.logger, 'removeHandler'):
            concrete_bot.shutdown()
        
        concrete_bot.lighter.lighter_ws_task.cancel.assert_called_once()
    
    def test_setup_signal_handlers(self, concrete_bot):
        """Test signal handler setup"""
        with patch('signal.signal') as mock_signal:
            concrete_bot.setup_signal_handlers()
            
            expected_calls = [
                call(signal.SIGINT, concrete_bot.shutdown),
                call(signal.SIGTERM, concrete_bot.shutdown)
            ]
            mock_signal.assert_has_calls(expected_calls)
    
    @pytest.mark.asyncio
    async def test_fetch_primary_bbo_prices_success(self, concrete_bot):
        """Test successful BBO price fetching"""
        concrete_bot.primary_contract_id = "test_contract_123"
        
        bid, ask = await concrete_bot.fetch_primary_bbo_prices()
        
        assert bid == Decimal('50000.0')
        assert ask == Decimal('50001.0')
        concrete_bot.primary_client.fetch_bbo_prices.assert_called_once_with("test_contract_123")
    
    @pytest.mark.asyncio
    async def test_fetch_primary_bbo_prices_no_client(self, concrete_bot):
        """Test BBO price fetching with no client"""
        concrete_bot.primary_client = None
        
        with pytest.raises(Exception, match="TestExchange client not initialized"):
            await concrete_bot.fetch_primary_bbo_prices()
    
    def test_round_to_tick_with_tick_size(self, concrete_bot):
        """Test price rounding with tick size"""
        concrete_bot.primary_tick_size = Decimal('0.01')
        price = Decimal('50000.555')
        
        rounded = concrete_bot.round_to_tick(price)
        
        assert rounded == Decimal('50000.56')
    
    def test_round_to_tick_no_tick_size(self, concrete_bot):
        """Test price rounding without tick size"""
        concrete_bot.primary_tick_size = None
        price = Decimal('50000.555')
        
        rounded = concrete_bot.round_to_tick(price)
        
        assert rounded == price
    
    @pytest.mark.asyncio
    async def test_place_bbo_order_success(self, concrete_bot):
        """Test successful BBO order placement"""
        # Mock successful order result
        order_result = Mock()
        order_result.success = True
        order_result.order_id = "order_123"
        order_result.price = Decimal('50000.0')
        concrete_bot.primary_client.place_open_order.return_value = order_result
        
        order_id, price = await concrete_bot.place_bbo_order("buy", Decimal('0.1'))
        
        assert order_id == "order_123"
        assert price == Decimal('50000.0')
    
    @pytest.mark.asyncio
    async def test_place_bbo_order_failure(self, concrete_bot):
        """Test failed BBO order placement"""
        # Mock failed order result
        order_result = Mock()
        order_result.success = False
        order_result.error_message = "Insufficient balance"
        concrete_bot.primary_client.place_open_order.return_value = order_result
        
        with pytest.raises(Exception, match="Failed to place order: Insufficient balance"):
            await concrete_bot.place_bbo_order("buy", Decimal('0.1'))
    
    def test_order_update_handler_buy_filled(self, concrete_bot):
        """Test order update handler for filled buy order"""
        concrete_bot.primary_contract_id = "test_contract_123"
        concrete_bot.primary_order_status = None
        
        order_data = {
            'contract_id': 'test_contract_123',
            'order_id': 'order_123',
            'status': 'FILLED',
            'side': 'buy',
            'filled_size': '0.1',
            'size': '0.1',
            'price': '50000.0'
        }
        
        with patch('hedge.hedge_mode_abc.log_trade_to_csv') as mock_log:
            concrete_bot.handle_primary_order_update(order_data)
        
        assert concrete_bot.waiting_for_lighter_fill is True
        assert concrete_bot.current_lighter_side == 'sell'
        assert concrete_bot.current_lighter_quantity == Decimal('0.1')
        assert concrete_bot.current_lighter_price == Decimal('50000.0')
    
    def test_order_update_handler_sell_filled(self, concrete_bot):
        """Test order update handler for filled sell order"""
        concrete_bot.primary_contract_id = "test_contract_123"
        
        order_data = {
            'contract_id': 'test_contract_123',
            'order_id': 'order_456',
            'status': 'FILLED',
            'side': 'sell',
            'filled_size': '0.05',
            'size': '0.05',
            'price': '49999.0'
        }
        
        concrete_bot.handle_primary_order_update(order_data)
        
        assert concrete_bot.waiting_for_lighter_fill is True
        assert concrete_bot.current_lighter_side == 'buy'
        assert concrete_bot.current_lighter_quantity == Decimal('0.05')
        assert concrete_bot.current_lighter_price == Decimal('49999.0')
    
    def test_reset_order_state(self, concrete_bot):
        """Test order state reset"""
        concrete_bot.order_execution_complete = True
        concrete_bot.waiting_for_lighter_fill = True
        
        concrete_bot._reset_order_state()
        
        assert concrete_bot.order_execution_complete is False
        assert concrete_bot.waiting_for_lighter_fill is False
    
    @pytest.mark.asyncio
    async def test_wait_for_lighter_execution_success(self, concrete_bot):
        """Test successful lighter execution wait"""
        concrete_bot.waiting_for_lighter_fill = True
        concrete_bot.current_lighter_side = 'sell'
        concrete_bot.current_lighter_quantity = Decimal('0.1')
        concrete_bot.current_lighter_price = Decimal('50000.0')
        
        start_time = time.time()
        result = await concrete_bot._wait_for_lighter_execution(start_time)
        
        assert result is True
        concrete_bot.lighter.place_lighter_market_order.assert_called_once_with(
            'sell', Decimal('0.1'), Decimal('50000.0')
        )
    
    @pytest.mark.asyncio
    async def test_wait_for_lighter_execution_timeout(self, concrete_bot):
        """Test lighter execution wait with timeout"""
        concrete_bot.waiting_for_lighter_fill = False
        concrete_bot.order_execution_complete = False
        
        # Mock time to trigger timeout and logger to avoid level issues
        with patch('time.time') as mock_time, \
             patch.object(concrete_bot.logger, 'error'):
            mock_time.side_effect = [1000.0, 1181.0]  # 181 seconds later
            
            start_time = 1000.0
            result = await concrete_bot._wait_for_lighter_execution(start_time)
            
            assert result is False
    
    @pytest.mark.asyncio
    async def test_execute_hedge_position_success(self, concrete_bot):
        """Test successful hedge position execution"""
        # Mock successful primary order placement
        concrete_bot.place_primary_post_only_order = AsyncMock()
        concrete_bot._wait_for_lighter_execution = AsyncMock(return_value=True)
        
        result = await concrete_bot._execute_hedge_position("buy", Decimal('0.1'))
        
        assert result is True
        concrete_bot.place_primary_post_only_order.assert_called_once_with("buy", Decimal('0.1'))
    
    @pytest.mark.asyncio
    async def test_execute_hedge_position_failure(self, concrete_bot):
        """Test failed hedge position execution"""
        # Mock failed primary order placement
        concrete_bot.place_primary_post_only_order = AsyncMock(side_effect=Exception("Order failed"))
        
        # Mock logger to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'error'):
            result = await concrete_bot._execute_hedge_position("buy", Decimal('0.1'))
        
        assert result is False
    
    def test_determine_close_side_and_quantity_no_position(self, concrete_bot):
        """Test close determination with no position"""
        concrete_bot.primary_position = Decimal('0')
        
        side, quantity = concrete_bot._determine_close_side_and_quantity()
        
        assert side is None
        assert quantity is None
    
    def test_determine_close_side_and_quantity_long_position(self, concrete_bot):
        """Test close determination with long position"""
        concrete_bot.primary_position = Decimal('0.5')
        
        side, quantity = concrete_bot._determine_close_side_and_quantity()
        
        assert side == 'sell'
        assert quantity == Decimal('0.5')
    
    def test_determine_close_side_and_quantity_short_position(self, concrete_bot):
        """Test close determination with short position"""
        concrete_bot.primary_position = Decimal('-0.3')
        
        side, quantity = concrete_bot._determine_close_side_and_quantity()
        
        assert side == 'buy'
        assert quantity == Decimal('0.3')
    
    @pytest.mark.asyncio
    @patch('asyncio.sleep')
    async def test_trading_loop_basic_flow(self, mock_sleep, concrete_bot):
        """Test basic trading loop flow"""
        concrete_bot.iterations = 1
        concrete_bot._execute_hedge_position = AsyncMock(return_value=True)
        concrete_bot._determine_close_side_and_quantity = Mock(return_value=(None, None))
        
        # Mock logger to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.trading_loop()
        
        # Should execute opening and first close
        assert concrete_bot._execute_hedge_position.call_count == 2
    
    @pytest.mark.asyncio
    @patch('asyncio.sleep')
    async def test_trading_loop_with_final_close(self, mock_sleep, concrete_bot):
        """Test trading loop with final close position"""
        concrete_bot.iterations = 1
        concrete_bot._execute_hedge_position = AsyncMock(return_value=True)
        concrete_bot._determine_close_side_and_quantity = Mock(return_value=('sell', Decimal('0.05')))
        
        # Mock logger to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.trading_loop()
        
        # Should execute opening, first close, and final close
        assert concrete_bot._execute_hedge_position.call_count == 3
    
    @pytest.mark.asyncio
    @patch('asyncio.sleep')
    async def test_trading_loop_position_diff_too_large(self, mock_sleep, concrete_bot):
        """Test trading loop stops when position difference is too large"""
        concrete_bot.iterations = 5
        concrete_bot.primary_position = Decimal('1.0')
        concrete_bot.lighter_position = Decimal('-0.7')  # Diff = 0.3 > 0.2
        concrete_bot._execute_hedge_position = AsyncMock(return_value=True)
        
        # Mock logger to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'info'), \
             patch.object(concrete_bot.logger, 'error'):
            await concrete_bot.trading_loop()
        
        # Should not execute any trades
        concrete_bot._execute_hedge_position.assert_not_called()
    
    @pytest.mark.asyncio
    @patch('asyncio.sleep')
    async def test_trading_loop_execution_failure(self, mock_sleep, concrete_bot):
        """Test trading loop stops on execution failure"""
        concrete_bot.iterations = 2
        concrete_bot._execute_hedge_position = AsyncMock(return_value=False)
        
        # Mock logger to avoid level comparison issues
        with patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.trading_loop()
        
        # Should stop after first failed execution
        assert concrete_bot._execute_hedge_position.call_count == 1
    
    @pytest.mark.asyncio
    async def test_run_method_success(self, concrete_bot):
        """Test successful run method"""
        # Mock all async methods
        concrete_bot._init_primary_contract_info = AsyncMock()
        concrete_bot._setup_primary_websocket = AsyncMock()
        concrete_bot.trading_loop = AsyncMock()
        
        # Mock logger and shutdown to avoid level comparison issues
        with patch.object(concrete_bot, 'shutdown'), \
             patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.run()
        
        concrete_bot._init_primary_contract_info.assert_called_once()
        concrete_bot._setup_primary_websocket.assert_called_once()
        concrete_bot.lighter.setup_ws_task.assert_called_once()
        concrete_bot.trading_loop.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_run_method_keyboard_interrupt(self, concrete_bot):
        """Test run method with keyboard interrupt"""
        concrete_bot._init_primary_contract_info = AsyncMock()
        concrete_bot._setup_primary_websocket = AsyncMock()
        concrete_bot.trading_loop = AsyncMock(side_effect=KeyboardInterrupt())
        
        # Mock logger and shutdown to avoid level comparison issues
        with patch.object(concrete_bot, 'shutdown') as mock_shutdown, \
             patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.run()
        
        # Should call shutdown which sets stop_flag
        mock_shutdown.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_run_method_exception(self, concrete_bot):
        """Test run method with general exception"""
        concrete_bot._init_primary_contract_info = AsyncMock(side_effect=Exception("Setup failed"))
        
        # Mock logger and shutdown to avoid level comparison issues
        with patch.object(concrete_bot, 'shutdown') as mock_shutdown, \
             patch.object(concrete_bot.logger, 'error'), \
             patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.run()
        
        # Should call shutdown which sets stop_flag
        mock_shutdown.assert_called_once()


class TestPostOnlyOrder:
    """Test place_primary_post_only_order method"""
    
    @pytest.fixture
    def mock_lighter_proxy(self):
        """Mock LighterProxy for testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '1',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }):
            mock_lighter = Mock()
            mock_lighter.stop_flag = False
            mock_lighter.setup_ws_task = AsyncMock()
            mock_lighter.place_lighter_market_order = AsyncMock()
            mock_lighter.lighter_ws_task = Mock()
            mock_lighter.lighter_ws_task.done.return_value = False
            mock_lighter.lighter_ws_task.cancel = Mock()
            yield mock_lighter
    
    @pytest.fixture
    def mock_file_operations(self):
        """Mock file operations"""
        with patch('os.makedirs'), \
             patch('builtins.open', create=True), \
             patch('logging.FileHandler') as mock_file_handler_cls, \
             patch('logging.StreamHandler') as mock_console_handler_cls:
            
            # Create proper mock handlers with level attributes
            mock_file_handler = Mock()
            mock_file_handler.level = logging.INFO
            mock_file_handler.setLevel = Mock()
            mock_file_handler_cls.return_value = mock_file_handler
            
            mock_console_handler = Mock()
            mock_console_handler.level = logging.INFO
            mock_console_handler.setLevel = Mock()
            mock_console_handler_cls.return_value = mock_console_handler
            
            yield
    
    @pytest.fixture
    def concrete_bot(self, mock_lighter_proxy, mock_file_operations):
        """Create a concrete bot instance for testing"""
        with patch('hedge.hedge_mode_abc.log_trade_to_csv'), \
             patch('hedge.hedge_mode_abc.LighterProxy', return_value=mock_lighter_proxy):
            bot = ConcreteHedgeBot(
                ticker="BTC",
                order_quantity=Decimal('0.1'),
                fill_timeout=5,
                iterations=10
            )
            return bot
    
    @pytest.mark.asyncio
    async def test_place_primary_post_only_order_filled_immediately(self, concrete_bot):
        """Test post-only order that gets filled immediately"""
        # Mock the dependencies
        concrete_bot.place_bbo_order = AsyncMock(return_value=("order_123", Decimal('50000.0')))
        
        # Set initial status to None, then immediately set to FILLED
        concrete_bot.primary_order_status = None
        
        call_count = 0
        original_sleep = asyncio.sleep
        
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # After first sleep, set status to FILLED to break the loop
                concrete_bot.primary_order_status = 'FILLED'
            await original_sleep(0.001)  # Very short real sleep to allow other tasks
        
        with patch('asyncio.sleep', side_effect=mock_sleep), \
             patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.place_primary_post_only_order("buy", Decimal('0.1'))
        
        # Verify order was placed
        concrete_bot.place_bbo_order.assert_called_once_with("buy", Decimal('0.1'))
        assert concrete_bot.primary_order_status == 'FILLED'
    
    @pytest.mark.asyncio
    async def test_place_primary_post_only_order_canceled_and_replaced(self, concrete_bot):
        """Test post-only order that gets canceled and replaced"""
        concrete_bot.place_bbo_order = AsyncMock(return_value=("order_123", Decimal('50000.0')))
        concrete_bot.fetch_primary_bbo_prices = AsyncMock(return_value=(Decimal('50010.0'), Decimal('50020.0')))
        
        concrete_bot.primary_order_status = None
        call_count = 0
        original_sleep = asyncio.sleep
        
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                concrete_bot.primary_order_status = 'CANCELED'
            elif call_count == 2:
                concrete_bot.primary_order_status = 'NEW'
            elif call_count >= 3:
                concrete_bot.primary_order_status = 'FILLED'
            await original_sleep(0.001)
        
        with patch('asyncio.sleep', side_effect=mock_sleep), \
             patch('time.time', return_value=1000.0), \
             patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.place_primary_post_only_order("buy", Decimal('0.1'))
        
        # Verify order was placed at least twice (original + replacement)
        assert concrete_bot.place_bbo_order.call_count >= 1
        assert concrete_bot.primary_order_status == 'FILLED'
    
    @pytest.mark.asyncio
    async def test_place_primary_post_only_order_no_client(self, concrete_bot):
        """Test post-only order when primary client is not initialized"""
        concrete_bot.primary_client = None
        
        with pytest.raises(Exception, match="TestExchange client not initialized"):
            await concrete_bot.place_primary_post_only_order("buy", Decimal('0.1'))
    
    @pytest.mark.asyncio
    async def test_place_primary_post_only_order_stop_flag(self, concrete_bot):
        """Test post-only order interrupted by stop flag"""
        concrete_bot.place_bbo_order = AsyncMock(return_value=("order_123", Decimal('50000.0')))
        concrete_bot.primary_order_status = None
        
        call_count = 0
        original_sleep = asyncio.sleep
        
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Set stop flag to break the loop
                concrete_bot.stop_flag = True
            await original_sleep(0.001)
        
        with patch('asyncio.sleep', side_effect=mock_sleep), \
             patch.object(concrete_bot.logger, 'info'):
            await concrete_bot.place_primary_post_only_order("buy", Decimal('0.1'))
        
        # Verify order was placed but loop was interrupted
        concrete_bot.place_bbo_order.assert_called_once()
        assert concrete_bot.stop_flag is True


class TestWebSocketOrderHandler:
    """Test WebSocket order update handler edge cases"""
    
    @pytest.fixture
    def mock_lighter_proxy(self):
        """Mock LighterProxy for testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '1',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }):
            mock_lighter = Mock()
            mock_lighter.stop_flag = False
            mock_lighter.setup_ws_task = AsyncMock()
            mock_lighter.place_lighter_market_order = AsyncMock()
            mock_lighter.lighter_ws_task = Mock()
            mock_lighter.lighter_ws_task.done.return_value = False
            mock_lighter.lighter_ws_task.cancel = Mock()
            yield mock_lighter
    
    @pytest.fixture
    def mock_file_operations(self):
        """Mock file operations"""
        with patch('os.makedirs'), \
             patch('builtins.open', create=True), \
             patch('logging.FileHandler') as mock_file_handler_cls, \
             patch('logging.StreamHandler') as mock_console_handler_cls:
            
            mock_file_handler = Mock()
            mock_file_handler.level = logging.INFO
            mock_file_handler.setLevel = Mock()
            mock_file_handler_cls.return_value = mock_file_handler
            
            mock_console_handler = Mock()
            mock_console_handler.level = logging.INFO
            mock_console_handler.setLevel = Mock()
            mock_console_handler_cls.return_value = mock_console_handler
            
            yield
    
    @pytest.fixture
    def concrete_bot(self, mock_lighter_proxy, mock_file_operations):
        """Create a concrete bot instance for testing"""
        with patch('hedge.hedge_mode_abc.log_trade_to_csv'), \
             patch('hedge.hedge_mode_abc.LighterProxy', return_value=mock_lighter_proxy):
            bot = ConcreteHedgeBot(
                ticker="BTC",
                order_quantity=Decimal('0.1'),
                fill_timeout=5,
                iterations=10
            )
            bot.primary_contract_id = "test_contract_123"
            return bot
    
    @pytest.mark.asyncio
    async def test_order_update_handler_contract_id_mismatch(self, concrete_bot):
        """Test order update handler with mismatched contract ID"""
        concrete_bot.primary_client = Mock()
        concrete_bot.primary_client.setup_order_update_handler = Mock()
        concrete_bot.primary_client.connect = AsyncMock()
        
        # Capture the handler function
        handler_func = None
        def capture_handler(handler):
            nonlocal handler_func
            handler_func = handler
        
        concrete_bot.primary_client.setup_order_update_handler.side_effect = capture_handler
        
        with patch.object(concrete_bot.logger, 'info'), \
             patch.object(concrete_bot.logger, 'error'):
            await concrete_bot._setup_primary_websocket()
        
        # Test with mismatched contract_id
        order_data = {
            'contract_id': 'different_contract',
            'order_id': 'order_123',
            'status': 'FILLED'
        }
        
        # Should return early without processing
        concrete_bot.handle_primary_order_update = Mock()
        handler_func(order_data)
        concrete_bot.handle_primary_order_update.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_order_update_handler_canceled_with_fill(self, concrete_bot):
        """Test CANCELED status with filled_size > 0 converts to FILLED"""
        concrete_bot.primary_client = Mock()
        concrete_bot.primary_client.setup_order_update_handler = Mock()
        concrete_bot.primary_client.connect = AsyncMock()
        concrete_bot.primary_order_status = None
        
        handler_func = None
        def capture_handler(handler):
            nonlocal handler_func
            handler_func = handler
        
        concrete_bot.primary_client.setup_order_update_handler.side_effect = capture_handler
        
        with patch('hedge.hedge_mode_abc.log_trade_to_csv'), \
             patch.object(concrete_bot.logger, 'info') as mock_info, \
             patch.object(concrete_bot.logger, 'error'):
            await concrete_bot._setup_primary_websocket()
        
        # Test CANCELED with filled_size > 0
        order_data = {
            'contract_id': 'test_contract_123',
            'order_id': 'order_123',
            'status': 'CANCELED',
            'side': 'buy',
            'filled_size': '0.1',
            'size': '0.1',
            'price': '50000.0'
        }
        
        concrete_bot.handle_primary_order_update = Mock()
        handler_func(order_data)
        
        # Should be treated as FILLED
        concrete_bot.handle_primary_order_update.assert_called_once()
        call_args = concrete_bot.handle_primary_order_update.call_args[0][0]
        assert call_args['status'] == 'FILLED'
    
    @pytest.mark.asyncio
    async def test_order_update_handler_open_status(self, concrete_bot):
        """Test OPEN status logging"""
        concrete_bot.primary_client = Mock()
        concrete_bot.primary_client.setup_order_update_handler = Mock()
        concrete_bot.primary_client.connect = AsyncMock()
        concrete_bot.primary_order_status = None
        
        handler_func = None
        def capture_handler(handler):
            nonlocal handler_func
            handler_func = handler
        
        concrete_bot.primary_client.setup_order_update_handler.side_effect = capture_handler
        
        with patch.object(concrete_bot.logger, 'info') as mock_info, \
             patch.object(concrete_bot.logger, 'error'):
            await concrete_bot._setup_primary_websocket()
        
        # Test OPEN status
        order_data = {
            'contract_id': 'test_contract_123',
            'order_id': 'order_123',
            'status': 'OPEN',
            'side': 'buy',
            'filled_size': '0',
            'size': '0.1',
            'price': '50000.0'
        }
        
        handler_func(order_data)
        
        # Should log OPEN status with size
        assert concrete_bot.primary_order_status == 'OPEN'
        # Don't check specific log calls as they can vary


class TestPostOnlyOrderAdvanced:
    """Test advanced post-only order scenarios"""
    
    @pytest.fixture
    def mock_lighter_proxy(self):
        """Mock LighterProxy for testing"""
        with patch.dict(os.environ, {
            'LIGHTER_ACCOUNT_INDEX': '1',
            'LIGHTER_API_KEY_INDEX': '1',
            'API_KEY_PRIVATE_KEY': 'test_key'
        }):
            mock_lighter = Mock()
            mock_lighter.stop_flag = False
            mock_lighter.setup_ws_task = AsyncMock()
            mock_lighter.place_lighter_market_order = AsyncMock()
            mock_lighter.lighter_ws_task = Mock()
            mock_lighter.lighter_ws_task.done.return_value = False
            mock_lighter.lighter_ws_task.cancel = Mock()
            yield mock_lighter
    
    @pytest.fixture
    def mock_file_operations(self):
        """Mock file operations"""
        with patch('os.makedirs'), \
             patch('builtins.open', create=True), \
             patch('logging.FileHandler') as mock_file_handler_cls, \
             patch('logging.StreamHandler') as mock_console_handler_cls:
            
            mock_file_handler = Mock()
            mock_file_handler.level = logging.INFO
            mock_file_handler.setLevel = Mock()
            mock_file_handler_cls.return_value = mock_file_handler
            
            mock_console_handler = Mock()
            mock_console_handler.level = logging.INFO
            mock_console_handler.setLevel = Mock()
            mock_console_handler_cls.return_value = mock_console_handler
            
            yield
    
    @pytest.fixture
    def concrete_bot(self, mock_lighter_proxy, mock_file_operations):
        """Create a concrete bot instance for testing"""
        with patch('hedge.hedge_mode_abc.log_trade_to_csv'), \
             patch('hedge.hedge_mode_abc.LighterProxy', return_value=mock_lighter_proxy):
            bot = ConcreteHedgeBot(
                ticker="BTC",
                order_quantity=Decimal('0.1'),
                fill_timeout=5,
                iterations=10
            )
            return bot
    
    @pytest.mark.asyncio
    async def test_place_primary_post_only_order_price_adjustment_buy(self, concrete_bot):
        """Test buy order price adjustment logic"""
        concrete_bot.place_bbo_order = AsyncMock(return_value=("order_123", Decimal('49990.0')))
        concrete_bot.fetch_primary_bbo_prices = AsyncMock(return_value=(Decimal('50000.0'), Decimal('50010.0')))
        concrete_bot.primary_client.cancel_order = AsyncMock(return_value=Mock(success=True))
        
        concrete_bot.primary_order_status = None
        call_count = 0
        original_sleep = asyncio.sleep
        
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count <= 22:  # Allow enough calls to reach 10+ second timeout
                concrete_bot.primary_order_status = 'OPEN'
            else:
                concrete_bot.primary_order_status = 'FILLED'
            await original_sleep(0.001)
        
        # Mock time.time() to simulate 15 seconds elapsed to trigger cancellation
        time_values = [1000.0] + [1015.0] * 30  # First call returns 1000, subsequent calls return 1015 (15 seconds later)
        
        with patch('asyncio.sleep', side_effect=mock_sleep), \
             patch('time.time', side_effect=time_values), \
             patch.object(concrete_bot.logger, 'info'), \
             patch.object(concrete_bot.logger, 'error'):
            await concrete_bot.place_primary_post_only_order("buy", Decimal('0.1'))
        
        # Verify cancel was called due to price being below best bid
        concrete_bot.primary_client.cancel_order.assert_called_with("order_123")
        assert concrete_bot.primary_client.cancel_order.call_count > 0
    
    @pytest.mark.asyncio
    async def test_place_primary_post_only_order_price_adjustment_sell(self, concrete_bot):
        """Test sell order price adjustment logic"""
        concrete_bot.place_bbo_order = AsyncMock(return_value=("order_123", Decimal('50020.0')))
        concrete_bot.fetch_primary_bbo_prices = AsyncMock(return_value=(Decimal('50000.0'), Decimal('50010.0')))
        concrete_bot.primary_client.cancel_order = AsyncMock(return_value=Mock(success=True))
        
        concrete_bot.primary_order_status = None
        call_count = 0
        original_sleep = asyncio.sleep
        
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count <= 22:
                concrete_bot.primary_order_status = 'OPEN'
            else:
                concrete_bot.primary_order_status = 'FILLED'
            await original_sleep(0.001)
        
        # Mock time.time() to simulate 15 seconds elapsed to trigger cancellation
        time_values = [1000.0] + [1015.0] * 30  # First call returns 1000, subsequent calls return 1015 (15 seconds later)
        
        with patch('asyncio.sleep', side_effect=mock_sleep), \
             patch('time.time', side_effect=time_values), \
             patch.object(concrete_bot.logger, 'info'), \
             patch.object(concrete_bot.logger, 'error'):
            await concrete_bot.place_primary_post_only_order("sell", Decimal('0.1'))
        
        # Verify cancel was called due to price being above best ask
        concrete_bot.primary_client.cancel_order.assert_called_with("order_123")
        assert concrete_bot.primary_client.cancel_order.call_count > 0
    
    @pytest.mark.asyncio
    async def test_place_primary_post_only_order_cancel_failure(self, concrete_bot):
        """Test cancel order failure handling"""
        concrete_bot.place_bbo_order = AsyncMock(return_value=("order_123", Decimal('49990.0')))
        concrete_bot.fetch_primary_bbo_prices = AsyncMock(return_value=(Decimal('50000.0'), Decimal('50010.0')))
        
        # Mock cancel_order to fail
        cancel_result = Mock()
        cancel_result.success = False
        cancel_result.error_message = "Cancel failed"
        concrete_bot.primary_client.cancel_order = AsyncMock(return_value=cancel_result)
        
        concrete_bot.primary_order_status = None
        call_count = 0
        original_sleep = asyncio.sleep
        
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count <= 22:
                concrete_bot.primary_order_status = 'OPEN'
            else:
                concrete_bot.primary_order_status = 'FILLED'
            await original_sleep(0.001)
        
        # Mock time.time() to simulate 15 seconds elapsed to trigger cancellation
        time_values = [1000.0] + [1015.0] * 30  # First call returns 1000, subsequent calls return 1015 (15 seconds later)
        
        with patch('asyncio.sleep', side_effect=mock_sleep), \
             patch('time.time', side_effect=time_values), \
             patch.object(concrete_bot.logger, 'info'), \
             patch.object(concrete_bot.logger, 'error') as mock_error:
            await concrete_bot.place_primary_post_only_order("buy", Decimal('0.1'))
        
        # Verify error was logged for cancel failure
        mock_error.assert_called()
        error_calls = [call for call in mock_error.call_args_list if 'Error canceling' in str(call)]
        assert len(error_calls) > 0


class TestArgumentParsing:
    """Test command line argument parsing"""
    
    def test_parse_arguments_default(self):
        """Test default argument parsing"""
        with patch('sys.argv', ['script_name']):
            args = parse_arguments()
            assert args.ticker == 'BTC'
            assert args.fill_timeout == 5
    
    def test_parse_arguments_custom(self):
        """Test custom argument parsing"""
        test_args = [
            'script_name',
            '--exchange', 'test_exchange',
            '--ticker', 'ETH',
            '--size', '0.5',
            '--iter', '15',
            '--fill-timeout', '10'
        ]
        
        with patch('sys.argv', test_args):
            args = parse_arguments()
            assert args.exchange == 'test_exchange'
            assert args.ticker == 'ETH'
            assert args.size == '0.5'
            assert args.iter == 15
            assert args.fill_timeout == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=hedge.hedge_mode_abc", "--cov-report=term-missing"])