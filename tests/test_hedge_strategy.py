"""
Unit tests for hedge/hedge_strategy.py

Tests all classes and methods with proper mocking of external dependencies.
"""

import pytest
import asyncio
import time
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hedge.hedge_strategy import (
    HedgeStrategy, 
    SpreadSampler, 
    TimingController, 
    SmartHedgeStrategy
)


class TestHedgeStrategy:
    """Test the abstract HedgeStrategy base class"""
    
    def test_hedge_strategy_is_abstract(self):
        """Test that HedgeStrategy cannot be instantiated directly"""
        with pytest.raises(TypeError):
            HedgeStrategy()
    
    def test_hedge_strategy_subclass_implementation(self):
        """Test that a concrete subclass can be created"""
        class ConcreteStrategy(HedgeStrategy):
            async def wait_open(self, hedge_bot):
                pass
            
            async def wait_close(self, hedge_bot):
                pass
        
        strategy = ConcreteStrategy()
        assert strategy.open_side == 'buy'


class TestSpreadSampler:
    """Test the SpreadSampler class"""
    
    @pytest.fixture
    def spread_sampler(self):
        """Create a SpreadSampler instance for testing"""
        return SpreadSampler(sample_count_range=(5, 10), cache_duration=60)
    
    @pytest.fixture
    def mock_primary_client(self):
        """Create a mock primary client (EdgeX)"""
        client = Mock()
        client.config = Mock()
        client.config.contract_id = "test_contract"
        # Mock fetch_bbo_prices to return (bid, ask) as Decimal
        client.fetch_bbo_prices = AsyncMock(return_value=(Decimal('2000.5'), Decimal('2001.0')))
        return client
    
    @pytest.fixture
    def mock_lighter_proxy(self):
        """Create a mock lighter proxy"""
        proxy = Mock()
        # Mock fetch_bbo_prices to return (bid, ask) as Decimal  
        proxy.fetch_bbo_prices = AsyncMock(return_value=(Decimal('2000.8'), Decimal('2001.3')))
        return proxy
    
    @pytest.fixture
    def mock_logger(self):
        """Create a mock logger"""
        logger = Mock()
        logger.info = Mock()
        logger.warning = Mock()
        logger.error = Mock()
        return logger
    
    def test_spread_sampler_initialization(self, spread_sampler):
        """Test SpreadSampler initialization"""
        assert spread_sampler.sample_count_range == (5, 10)
        assert spread_sampler.cache_duration == 60
        assert spread_sampler.spread_history == []
        assert spread_sampler.average_spread is None
        assert spread_sampler.last_update_time is None
        assert spread_sampler.logger is None
    
    @pytest.mark.asyncio
    async def test_sample_current_spread(self, spread_sampler, mock_primary_client, mock_lighter_proxy):
        """Test sampling current spread"""
        sample = await spread_sampler.sample_current_spread(mock_primary_client, mock_lighter_proxy)
        
        assert 'spread' in sample
        assert 'primary_mid' in sample
        assert 'lighter_mid' in sample
        assert 'primary_bid' in sample
        assert 'primary_ask' in sample
        assert 'lighter_bid' in sample
        assert 'lighter_ask' in sample
        assert 'timestamp' in sample
        
        # Check calculations
        expected_primary_mid = (Decimal('2000.5') + Decimal('2001.0')) / 2
        expected_lighter_mid = (Decimal('2000.8') + Decimal('2001.3')) / 2
        expected_spread = abs(expected_primary_mid - expected_lighter_mid)
        
        assert sample['primary_mid'] == expected_primary_mid
        assert sample['lighter_mid'] == expected_lighter_mid
        assert sample['spread'] == expected_spread
    
    @pytest.mark.asyncio
    async def test_sample_current_spread_with_exception(self, spread_sampler, mock_primary_client, mock_lighter_proxy, mock_logger):
        """Test sampling with exception handling"""
        spread_sampler.logger = mock_logger
        mock_primary_client.fetch_bbo_prices.side_effect = Exception("Network error")
        
        with pytest.raises(Exception):
            await spread_sampler.sample_current_spread(mock_primary_client, mock_lighter_proxy)
        
        mock_logger.error.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('random.randint')
    @patch('random.uniform') 
    @patch('asyncio.sleep')
    async def test_calculate_average_spread(self, mock_sleep, mock_uniform, mock_randint, 
                                          spread_sampler, mock_primary_client, mock_lighter_proxy, mock_logger):
        """Test calculating average spread"""
        spread_sampler.logger = mock_logger
        mock_randint.return_value = 3  # Sample 3 times
        mock_uniform.return_value = 1.0  # 1 second sleep
        mock_sleep.return_value = None  # Mock asyncio.sleep
        
        # Mock consecutive samples with different spreads
        sample_data = [
            (Decimal('2000.0'), Decimal('2001.0'), Decimal('2000.5'), Decimal('2001.5')),  # spread = 0.5
            (Decimal('2000.2'), Decimal('2001.2'), Decimal('2000.7'), Decimal('2001.7')),  # spread = 0.5  
            (Decimal('2000.1'), Decimal('2001.1'), Decimal('2000.6'), Decimal('2001.6')),  # spread = 0.5
        ]
        
        call_count = 0
        async def mock_fetch_bbo_primary(contract_id):
            nonlocal call_count
            result = sample_data[call_count][:2]
            call_count += 1
            return result
            
        async def mock_fetch_bbo_lighter():
            return sample_data[call_count-1][2:]
        
        mock_primary_client.fetch_bbo_prices.side_effect = mock_fetch_bbo_primary
        mock_lighter_proxy.fetch_bbo_prices.side_effect = mock_fetch_bbo_lighter
        
        avg_spread = await spread_sampler.calculate_average_spread(mock_primary_client, mock_lighter_proxy)
        
        assert avg_spread == Decimal('0.5')
        assert spread_sampler.average_spread == Decimal('0.5')
        assert len(spread_sampler.spread_history) == 3
        mock_logger.info.assert_called()
    
    @pytest.mark.asyncio
    async def test_calculate_average_spread_insufficient_samples(self, spread_sampler, mock_primary_client, mock_lighter_proxy):
        """Test calculating average spread with insufficient samples"""
        # Mock to always fail
        mock_primary_client.fetch_bbo_prices.side_effect = Exception("Network error")
        
        with pytest.raises(Exception, match="采样失败：有效样本不足"):
            await spread_sampler.calculate_average_spread(mock_primary_client, mock_lighter_proxy)
    
    @pytest.mark.asyncio
    async def test_calculate_average_spread_cache(self, spread_sampler):
        """Test average spread caching functionality"""
        spread_sampler.average_spread = Decimal('0.5')
        spread_sampler.last_update_time = time.time()
        
        # Should return cached value when force_refresh=False
        result = await spread_sampler.calculate_average_spread(Mock(), Mock(), force_refresh=False)
        assert result == Decimal('0.5')
    
    def test_should_open_by_spread(self, spread_sampler):
        """Test spread-based open decision"""
        # No average spread set
        assert not spread_sampler.should_open_by_spread(Decimal('1.0'))
        
        # Set average spread
        spread_sampler.average_spread = Decimal('0.5')
        
        # Current spread greater than average - should open
        assert spread_sampler.should_open_by_spread(Decimal('0.6'))
        
        # Current spread less than average - should not open
        assert not spread_sampler.should_open_by_spread(Decimal('0.4'))
    
    def test_should_close_by_spread(self, spread_sampler):
        """Test spread-based close decision"""
        # No average spread set
        assert not spread_sampler.should_close_by_spread(Decimal('0.3'))
        
        # Set average spread
        spread_sampler.average_spread = Decimal('0.5')
        
        # Current spread <= average and meets profit threshold - should close
        assert spread_sampler.should_close_by_spread(Decimal('0.48'), profit_threshold=Decimal('0.05'))
        
        # Current spread > average - should not close
        assert not spread_sampler.should_close_by_spread(Decimal('0.6'), profit_threshold=Decimal('0.05'))
        
        # Current spread <= average but below profit threshold - should not close
        assert not spread_sampler.should_close_by_spread(Decimal('0.4'), profit_threshold=Decimal('0.05'))


class TestTimingController:
    """Test the TimingController class"""
    
    @pytest.fixture
    def timing_controller(self):
        """Create a TimingController instance for testing"""
        return TimingController()
    
    @pytest.fixture
    def mock_logger(self):
        """Create a mock logger"""
        logger = Mock()
        logger.info = Mock()
        return logger
    
    def test_timing_controller_initialization(self, timing_controller):
        """Test TimingController initialization"""
        assert timing_controller.last_close_time is None
        assert timing_controller.next_open_time is None
        assert timing_controller.next_close_time is None
        assert timing_controller.is_first_trade is True
        assert timing_controller.logger is None
    
    @patch('random.uniform')
    def test_schedule_next_open_first_trade(self, mock_uniform, timing_controller, mock_logger):
        """Test scheduling next open for first trade"""
        timing_controller.logger = mock_logger
        mock_uniform.return_value = 10.0
        
        # First trade should return True immediately
        result = timing_controller.schedule_next_open(5, 20)
        assert result is True
        
        # Should not have called random.uniform for first trade
        mock_uniform.assert_not_called()
    
    @patch('random.uniform')
    @patch('time.time')
    def test_schedule_next_open_subsequent_trade(self, mock_time, mock_uniform, timing_controller, mock_logger):
        """Test scheduling next open for subsequent trades"""
        timing_controller.logger = mock_logger
        timing_controller.last_close_time = 1000.0
        mock_uniform.return_value = 10.0  # 10 minutes
        mock_time.return_value = 1000.0
        
        result = timing_controller.schedule_next_open(5, 20)
        assert result is False
        
        expected_next_open = 1000.0 + (10.0 * 60)
        assert timing_controller.next_open_time == expected_next_open
        mock_uniform.assert_called_once_with(5, 20)
        mock_logger.info.assert_called_once()
    
    @patch('random.uniform')
    @patch('time.time')
    def test_schedule_next_close(self, mock_time, mock_uniform, timing_controller, mock_logger):
        """Test scheduling next close"""
        timing_controller.logger = mock_logger
        mock_uniform.return_value = 60.0  # 60 minutes
        mock_time.return_value = 1000.0
        
        timing_controller.schedule_next_close(30, 120)
        
        expected_next_close = 1000.0 + (60.0 * 60)
        assert timing_controller.next_close_time == expected_next_close
        mock_uniform.assert_called_once_with(30, 120)
        mock_logger.info.assert_called_once()
    
    @patch('time.time')
    def test_can_open_by_time(self, mock_time, timing_controller):
        """Test time-based open decision"""
        mock_time.return_value = 1000.0
        
        # First trade should always be allowed
        assert timing_controller.can_open_by_time() is True
        
        # Set not first trade
        timing_controller.is_first_trade = False
        
        # No next_open_time set should be allowed
        assert timing_controller.can_open_by_time() is True
        
        # Next open time in future - should not be allowed
        timing_controller.next_open_time = 1500.0
        assert timing_controller.can_open_by_time() is False
        
        # Next open time in past - should be allowed
        timing_controller.next_open_time = 500.0
        assert timing_controller.can_open_by_time() is True
    
    @patch('time.time')
    def test_should_close_by_time(self, mock_time, timing_controller):
        """Test time-based close decision"""
        mock_time.return_value = 1000.0
        
        # No next_close_time set
        assert timing_controller.should_close_by_time() is False
        
        # Next close time in future
        timing_controller.next_close_time = 1500.0
        assert timing_controller.should_close_by_time() is False
        
        # Next close time in past
        timing_controller.next_close_time = 500.0
        assert timing_controller.should_close_by_time() is True
    
    @patch('time.time')
    def test_record_close(self, mock_time, timing_controller):
        """Test recording close time"""
        mock_time.return_value = 1000.0
        timing_controller.next_close_time = 1500.0
        
        timing_controller.record_close()
        
        assert timing_controller.last_close_time == 1000.0
        assert timing_controller.is_first_trade is False
        assert timing_controller.next_close_time is None


class TestSmartHedgeStrategy:
    """Test the SmartHedgeStrategy class"""
    
    @pytest.fixture
    def smart_strategy(self):
        """Create a SmartHedgeStrategy instance for testing"""
        return SmartHedgeStrategy(
            sample_count_range=(3, 5),
            cache_duration=30,
            profit_threshold=0.05,
            open_wait_range=(2, 5),
            close_wait_range=(10, 30),
            sleep_time=1,
            max_open_wait_minutes=5,
            max_close_wait_minutes=10
        )
    
    @pytest.fixture
    def mock_hedge_bot(self):
        """Create a mock hedge bot"""
        hedge_bot = Mock()
        hedge_bot.logger = Mock()
        hedge_bot.logger.info = Mock()
        hedge_bot.logger.warning = Mock()
        hedge_bot.logger.error = Mock()
        hedge_bot.stop_flag = False
        
        # Mock primary client
        hedge_bot.primary_client = Mock()
        hedge_bot.primary_client.config = Mock()
        hedge_bot.primary_client.config.contract_id = "test_contract"
        hedge_bot.primary_client.fetch_bbo_prices = AsyncMock(return_value=(Decimal('2000.5'), Decimal('2001.0')))
        
        # Mock lighter proxy
        hedge_bot.lighter = Mock()
        hedge_bot.lighter.fetch_bbo_prices = AsyncMock(return_value=(Decimal('2000.8'), Decimal('2001.3')))
        
        return hedge_bot
    
    def test_smart_hedge_strategy_initialization(self, smart_strategy):
        """Test SmartHedgeStrategy initialization"""
        assert isinstance(smart_strategy.spread_sampler, SpreadSampler)
        assert isinstance(smart_strategy.timing_controller, TimingController)
        assert smart_strategy.sleep_time == 1
        assert smart_strategy.profit_threshold == 0.05
        assert smart_strategy.open_wait_range == (2, 5)
        assert smart_strategy.close_wait_range == (10, 30)
        assert smart_strategy.max_open_wait_minutes == 5
        assert smart_strategy.max_close_wait_minutes == 10
        assert smart_strategy.open_decision_start_time is None
        assert smart_strategy.close_decision_start_time is None
    
    def test_setup_logger(self, smart_strategy, mock_hedge_bot):
        """Test logger setup"""
        smart_strategy._setup_logger(mock_hedge_bot)
        
        assert smart_strategy.spread_sampler.logger == mock_hedge_bot.logger
        assert smart_strategy.timing_controller.logger == mock_hedge_bot.logger
    
    @patch('time.time')
    def test_is_open_timeout(self, mock_time, smart_strategy):
        """Test open timeout checking"""
        mock_time.return_value = 1000.0
        
        # No start time set
        assert smart_strategy._is_open_timeout() is False
        
        # Start time set but within limit
        smart_strategy.open_decision_start_time = 1000.0 - (3 * 60)  # 3 minutes ago
        assert smart_strategy._is_open_timeout() is False
        
        # Start time set and exceeded limit
        smart_strategy.open_decision_start_time = 1000.0 - (6 * 60)  # 6 minutes ago
        assert smart_strategy._is_open_timeout() is True
    
    @patch('time.time')
    def test_is_close_timeout(self, mock_time, smart_strategy):
        """Test close timeout checking"""
        mock_time.return_value = 1000.0
        
        # No start time set
        assert smart_strategy._is_close_timeout() is False
        
        # Start time set but within limit
        smart_strategy.close_decision_start_time = 1000.0 - (5 * 60)  # 5 minutes ago
        assert smart_strategy._is_close_timeout() is False
        
        # Start time set and exceeded limit
        smart_strategy.close_decision_start_time = 1000.0 - (12 * 60)  # 12 minutes ago
        assert smart_strategy._is_close_timeout() is True
    
    def test_reset_open_decision_time(self, smart_strategy):
        """Test resetting open decision time"""
        smart_strategy.open_decision_start_time = 1000.0
        smart_strategy._reset_open_decision_time()
        assert smart_strategy.open_decision_start_time is None
    
    def test_reset_close_decision_time(self, smart_strategy):
        """Test resetting close decision time"""
        smart_strategy.close_decision_start_time = 1000.0
        smart_strategy._reset_close_decision_time()
        assert smart_strategy.close_decision_start_time is None
    
    @pytest.mark.asyncio
    @patch('time.time')
    @patch('asyncio.sleep')
    async def test_wait_open_first_trade_spread_favorable(self, mock_sleep, mock_time, smart_strategy, mock_hedge_bot):
        """Test wait_open for first trade with favorable spread"""
        mock_time.return_value = 1000.0
        mock_sleep.return_value = None
        
        # Mock spread sampler methods
        smart_strategy.spread_sampler.calculate_average_spread = AsyncMock()
        smart_strategy.spread_sampler.sample_current_spread = AsyncMock(return_value={
            'spread': Decimal('0.6'),
            'primary_mid': Decimal('2000.75'),
            'lighter_mid': Decimal('2001.05'),
            'primary_bid': Decimal('2000.5'),
            'primary_ask': Decimal('2001.0'),
            'lighter_bid': Decimal('2000.8'),
            'lighter_ask': Decimal('2001.3'),
            'timestamp': 1000.0
        })
        smart_strategy.spread_sampler.should_open_by_spread = Mock(return_value=True)
        smart_strategy.spread_sampler.average_spread = Decimal('0.5')
        
        # Mock timing controller
        smart_strategy.timing_controller.schedule_next_close = Mock()
        
        await smart_strategy.wait_open(mock_hedge_bot)
        
        # Should set open_side based on price comparison
        assert smart_strategy.open_side == 'buy'  # primary_mid < lighter_mid
        
        # Should have called methods
        smart_strategy.spread_sampler.calculate_average_spread.assert_called()
        smart_strategy.timing_controller.schedule_next_close.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('time.time')
    @patch('asyncio.sleep')
    async def test_wait_open_timeout_protection(self, mock_sleep, mock_time, smart_strategy, mock_hedge_bot):
        """Test wait_open timeout protection"""
        # Mock time progression to trigger timeout
        # Provide enough values for all time.time() calls during the test
        base_time = 1000.0
        timeout_time = base_time + (6 * 60)  # 6 minutes later to trigger timeout
        # Return increasing time values to simulate time progression
        mock_time.return_value = base_time
        
        # Counter to track calls and simulate time progression
        call_count = [0]
        def time_side_effect():
            call_count[0] += 1
            if call_count[0] <= 3:  # First few calls return base time
                return base_time
            else:  # Later calls return timeout time
                return timeout_time
        
        mock_time.side_effect = time_side_effect
        mock_sleep.return_value = None
        
        # Mock unfavorable conditions
        smart_strategy.spread_sampler.sample_current_spread = AsyncMock(return_value={
            'spread': Decimal('0.3'),
            'primary_mid': Decimal('2000.75'),
            'lighter_mid': Decimal('2001.05'),
            'primary_bid': Decimal('2000.5'),
            'primary_ask': Decimal('2001.0'),
            'lighter_bid': Decimal('2000.8'),
            'lighter_ask': Decimal('2001.3'),
            'timestamp': 1000.0
        })
        smart_strategy.spread_sampler.should_open_by_spread = Mock(return_value=False)
        smart_strategy.timing_controller.can_open_by_time = Mock(return_value=False)
        smart_strategy.timing_controller.schedule_next_close = Mock()
        
        await smart_strategy.wait_open(mock_hedge_bot)
        
        # Should trigger timeout protection
        mock_hedge_bot.logger.warning.assert_called()
        smart_strategy.timing_controller.schedule_next_close.assert_called()
    
    @pytest.mark.asyncio
    @patch('time.time')
    @patch('asyncio.sleep')
    async def test_wait_close_spread_favorable(self, mock_sleep, mock_time, smart_strategy, mock_hedge_bot):
        """Test wait_close with favorable spread"""
        mock_time.return_value = 1000.0
        mock_sleep.return_value = None
        
        # Mock spread sampler methods
        smart_strategy.spread_sampler.sample_current_spread = AsyncMock(return_value={
            'spread': Decimal('0.4'),
            'primary_mid': Decimal('2000.75'),
            'lighter_mid': Decimal('2001.05'),
            'timestamp': 1000.0
        })
        smart_strategy.spread_sampler.should_close_by_spread = Mock(return_value=True)
        # Set average spread for should_close_by_spread calculation
        smart_strategy.spread_sampler.average_spread = Decimal('0.5')
        
        # Mock timing controller
        smart_strategy.timing_controller.record_close = Mock()
        smart_strategy.timing_controller.schedule_next_open = Mock()
        
        await smart_strategy.wait_close(mock_hedge_bot)
        
        # Should have called methods
        smart_strategy.timing_controller.record_close.assert_called_once()
        smart_strategy.timing_controller.schedule_next_open.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('time.time')
    @patch('asyncio.sleep')
    async def test_wait_close_time_based(self, mock_sleep, mock_time, smart_strategy, mock_hedge_bot):
        """Test wait_close with time-based trigger"""
        mock_time.return_value = 1000.0
        mock_sleep.return_value = None
        
        # Mock spread sampler methods
        smart_strategy.spread_sampler.sample_current_spread = AsyncMock(return_value={
            'spread': Decimal('0.6'),
            'timestamp': 1000.0
        })
        smart_strategy.spread_sampler.should_close_by_spread = Mock(return_value=False)
        smart_strategy.spread_sampler.average_spread = Decimal('0.5')
        
        # Mock timing controller to trigger time-based close
        smart_strategy.timing_controller.should_close_by_time = Mock(return_value=True)
        smart_strategy.timing_controller.record_close = Mock()
        smart_strategy.timing_controller.schedule_next_open = Mock()
        
        await smart_strategy.wait_close(mock_hedge_bot)
        
        # Should have called methods
        smart_strategy.timing_controller.record_close.assert_called_once()
        smart_strategy.timing_controller.schedule_next_open.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_wait_open_cancelled_error(self, smart_strategy, mock_hedge_bot):
        """Test wait_open with stop_flag set"""
        mock_hedge_bot.stop_flag = True
        
        with pytest.raises(asyncio.CancelledError, match="开仓等待被中断"):
            await smart_strategy.wait_open(mock_hedge_bot)
    
    @pytest.mark.asyncio
    async def test_wait_close_cancelled_error(self, smart_strategy, mock_hedge_bot):
        """Test wait_close with stop_flag set"""
        mock_hedge_bot.stop_flag = True
        
        with pytest.raises(asyncio.CancelledError, match="平仓等待被中断"):
            await smart_strategy.wait_close(mock_hedge_bot)
    
    # ========== 风险控制功能测试 ==========
    
    @pytest.mark.asyncio
    async def test_check_liquidation_risk_both_exchanges_safe(self, smart_strategy, mock_hedge_bot):
        """Test liquidation risk check when both exchanges are safe"""
        # Mock liquidation prices - safe distance from current prices
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)  # Far from 2000.75
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)  # Far from 2001.05
        
        current_sample = {
            'primary_mid': 2000.75,
            'lighter_mid': 2001.05
        }
        
        result = await smart_strategy._check_liquidation_risk(mock_hedge_bot, current_sample)
        
        assert result is False
        mock_hedge_bot.logger.debug.assert_called()  # Should log safe status
    
    @pytest.mark.asyncio
    async def test_check_liquidation_risk_primary_exchange_danger(self, smart_strategy, mock_hedge_bot):
        """Test liquidation risk check when primary exchange is near liquidation"""
        # Mock liquidation prices - primary exchange near liquidation
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(return_value=2100.0)  # Close to 2000.75
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)  # Safe
        
        current_sample = {
            'primary_mid': 2000.75,
            'lighter_mid': 2001.05
        }
        
        result = await smart_strategy._check_liquidation_risk(mock_hedge_bot, current_sample)
        
        assert result is True
        mock_hedge_bot.logger.warning.assert_called()  # Should log danger status
    
    @pytest.mark.asyncio
    async def test_check_liquidation_risk_lighter_exchange_danger(self, smart_strategy, mock_hedge_bot):
        """Test liquidation risk check when lighter exchange is near liquidation"""
        # Mock liquidation prices - lighter exchange near liquidation
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)  # Safe
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(return_value=2100.0)  # Close to 2001.05
        
        current_sample = {
            'primary_mid': 2000.75,
            'lighter_mid': 2001.05
        }
        
        result = await smart_strategy._check_liquidation_risk(mock_hedge_bot, current_sample)
        
        assert result is True
        mock_hedge_bot.logger.warning.assert_called()  # Should log danger status
    
    @pytest.mark.asyncio
    async def test_check_liquidation_risk_primary_exception(self, smart_strategy, mock_hedge_bot):
        """Test liquidation risk check when primary exchange throws exception"""
        # Mock primary exchange to throw exception, lighter safe
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(
            side_effect=Exception("Primary API error")
        )
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)  # Safe
        
        current_sample = {
            'primary_mid': 2000.75,
            'lighter_mid': 2001.05
        }
        
        result = await smart_strategy._check_liquidation_risk(mock_hedge_bot, current_sample)
        
        assert result is False  # Should continue with only lighter check
        mock_hedge_bot.logger.warning.assert_any_call("⚠️ 获取Primary清算价格失败: Primary API error")
    
    @pytest.mark.asyncio
    async def test_check_liquidation_risk_lighter_exception(self, smart_strategy, mock_hedge_bot):
        """Test liquidation risk check when lighter exchange throws exception"""
        # Mock lighter exchange to throw exception, primary safe
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)  # Safe
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(
            side_effect=Exception("Lighter API error")
        )
        
        current_sample = {
            'primary_mid': 2000.75,
            'lighter_mid': 2001.05
        }
        
        result = await smart_strategy._check_liquidation_risk(mock_hedge_bot, current_sample)
        
        assert result is False  # Should continue with only primary check
        mock_hedge_bot.logger.warning.assert_any_call("⚠️ 获取Lighter清算价格失败: Lighter API error")
    
    @pytest.mark.asyncio
    async def test_check_liquidation_risk_both_exceptions(self, smart_strategy, mock_hedge_bot):
        """Test liquidation risk check when both exchanges throw exceptions"""
        # Mock both exchanges to throw exceptions
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(
            side_effect=Exception("Primary API error")
        )
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(
            side_effect=Exception("Lighter API error")
        )
        
        current_sample = {
            'primary_mid': 2000.75,
            'lighter_mid': 2001.05
        }
        
        result = await smart_strategy._check_liquidation_risk(mock_hedge_bot, current_sample)
        
        assert result is False  # Should skip risk check
        mock_hedge_bot.logger.warning.assert_any_call("⚠️ 无法获取任何清算价格，跳过风险控制检查")
    
    @pytest.mark.asyncio
    async def test_check_liquidation_risk_general_exception(self, smart_strategy, mock_hedge_bot):
        """Test liquidation risk check with general exception"""
        # Mock to throw general exception during the check
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(
            side_effect=asyncio.TimeoutError("Timeout error")
        )
        
        current_sample = {
            'primary_mid': 2000.75,
            'lighter_mid': 2001.05
        }
        
        result = await smart_strategy._check_liquidation_risk(mock_hedge_bot, current_sample)
        
        assert result is False  # Should handle exception gracefully
        mock_hedge_bot.logger.error.assert_called()  # Should log the error
    
    def test_check_single_exchange_risk_safe(self, smart_strategy):
        """Test single exchange risk check - safe case"""
        logger = Mock()
        
        # Safe distance: current price 2000, liquidation price 1500, distance > 20%
        result = smart_strategy._check_single_exchange_risk(
            "TestExchange", 2000.0, 1500.0, logger
        )
        
        assert result is False
        logger.debug.assert_called()  # Should log safe status
        logger.warning.assert_not_called()
    
    def test_check_single_exchange_risk_danger(self, smart_strategy):
        """Test single exchange risk check - danger case"""
        logger = Mock()
        
        # Danger distance: current price 2000, liquidation price 2100, distance < 20%
        result = smart_strategy._check_single_exchange_risk(
            "TestExchange", 2000.0, 2100.0, logger
        )
        
        assert result is True
        logger.warning.assert_called()  # Should log danger status
        logger.debug.assert_not_called()
    
    def test_check_single_exchange_risk_exact_threshold(self, smart_strategy):
        """Test single exchange risk check - exactly at threshold"""
        logger = Mock()
        
        # Exact threshold: current price 2000, liquidation price 2500, distance = 20%
        result = smart_strategy._check_single_exchange_risk(
            "TestExchange", 2000.0, 2500.0, logger
        )
        
        assert result is True  # Should trigger at exactly 20%
        logger.warning.assert_called()
    
    def test_check_single_exchange_risk_custom_threshold(self):
        """Test single exchange risk check with custom threshold"""
        # Create strategy with custom risk threshold
        custom_strategy = SmartHedgeStrategy(risk_threshold=0.10)  # 10% threshold
        logger = Mock()
        
        # Distance 15%: should be safe with 10% threshold
        result = custom_strategy._check_single_exchange_risk(
            "TestExchange", 2000.0, 1700.0, logger  # Distance = 300/1700 ≈ 17.6%
        )
        
        assert result is False
        logger.debug.assert_called()
    
    def test_check_single_exchange_risk_invalid_liquidation_price(self, smart_strategy):
        """Test single exchange risk check with invalid liquidation prices"""
        logger = Mock()
        
        # Test None liquidation price
        result = smart_strategy._check_single_exchange_risk(
            "TestExchange", 2000.0, None, logger
        )
        assert result is False
        
        # Test zero liquidation price
        result = smart_strategy._check_single_exchange_risk(
            "TestExchange", 2000.0, 0.0, logger
        )
        assert result is False
        
        # Test negative liquidation price
        result = smart_strategy._check_single_exchange_risk(
            "TestExchange", 2000.0, -100.0, logger
        )
        assert result is False
        
        # Should not have called any logging for invalid prices
        logger.debug.assert_not_called()
        logger.warning.assert_not_called()
    
    @pytest.mark.asyncio
    @patch('time.time')
    @patch('asyncio.sleep')
    async def test_wait_close_with_risk_control_trigger(self, mock_sleep, mock_time, smart_strategy, mock_hedge_bot):
        """Test wait_close with risk control triggering immediate close"""
        mock_time.return_value = 1000.0
        mock_sleep.return_value = None
        
        # Mock dangerous liquidation prices
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(return_value=2100.0)
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)
        
        # Mock spread sampler methods
        smart_strategy.spread_sampler.sample_current_spread = AsyncMock(return_value={
            'spread': Decimal('0.6'),
            'primary_mid': 2000.75,  # Close to liquidation price 2100
            'lighter_mid': 2001.05,
            'timestamp': 1000.0
        })
        
        # Mock timing controller
        smart_strategy.timing_controller.record_close = Mock()
        smart_strategy.timing_controller.schedule_next_open = Mock()
        
        await smart_strategy.wait_close(mock_hedge_bot)
        
        # Should trigger risk control
        mock_hedge_bot.logger.warning.assert_any_call("🚨 风险控制触发：价格接近清算线，立即双边平仓")
        smart_strategy.timing_controller.record_close.assert_called_once()
        smart_strategy.timing_controller.schedule_next_open.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('time.time')
    @patch('asyncio.sleep')
    async def test_wait_close_risk_control_no_trigger(self, mock_sleep, mock_time, smart_strategy, mock_hedge_bot):
        """Test wait_close with risk control not triggering"""
        mock_time.return_value = 1000.0
        mock_sleep.return_value = None
        
        # Mock safe liquidation prices
        mock_hedge_bot.primary_client.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)
        mock_hedge_bot.lighter.get_ticker_position_liquidation_price = AsyncMock(return_value=1500.0)
        
        # Mock spread sampler methods
        smart_strategy.spread_sampler.sample_current_spread = AsyncMock(return_value={
            'spread': Decimal('0.4'),
            'primary_mid': 2000.75,  # Safe distance from liquidation
            'lighter_mid': 2001.05,
            'timestamp': 1000.0
        })
        smart_strategy.spread_sampler.should_close_by_spread = Mock(return_value=True)
        smart_strategy.spread_sampler.average_spread = Decimal('0.5')
        
        # Mock timing controller
        smart_strategy.timing_controller.record_close = Mock()
        smart_strategy.timing_controller.schedule_next_open = Mock()
        
        await smart_strategy.wait_close(mock_hedge_bot)
        
        # Should not trigger risk control, but should close normally
        risk_warning_called = any("风险控制触发" in str(call) for call in mock_hedge_bot.logger.warning.call_args_list)
        assert not risk_warning_called
        
        # Should close based on spread
        spread_close_called = any("价差维度满足平仓" in str(call) for call in mock_hedge_bot.logger.info.call_args_list)
        assert spread_close_called
    
    def test_smart_hedge_strategy_initialization_with_risk_threshold(self):
        """Test SmartHedgeStrategy initialization with custom risk threshold"""
        strategy = SmartHedgeStrategy(
            sample_count_range=(3, 5),
            cache_duration=30,
            profit_threshold=0.05,
            risk_threshold=0.15,  # Custom 15% risk threshold
            open_wait_range=(2, 5),
            close_wait_range=(10, 30)
        )
        
        assert strategy.risk_threshold == 0.15
        assert isinstance(strategy.spread_sampler, SpreadSampler)
        assert isinstance(strategy.timing_controller, TimingController)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=hedge.hedge_strategy", "--cov-report=term-missing"])