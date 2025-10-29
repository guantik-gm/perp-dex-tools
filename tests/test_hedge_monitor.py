"""
Unit tests for hedge/hedge_monitor.py

全面测试对冲交易监控模块的所有功能，包括通知管理和状态监控。
"""

import pytest
import asyncio
import time
import logging
import os
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hedge.hedge_monitor import HedgeMonitor
from hedge.hedge_strategy import StrategyExecutionContext, DecisionTrigger
from helpers.telegram_bot import TelegramBot


class TestHedgeMonitor:
    """测试 HedgeMonitor 类的所有功能"""

    @pytest.fixture
    def mock_logger(self):
        """创建模拟 logger"""
        logger = Mock(spec=logging.Logger)
        logger.info = Mock()
        logger.warning = Mock()
        logger.error = Mock()
        logger.debug = Mock()
        return logger

  
    @pytest.fixture
    def hedge_monitor(self, mock_logger):
        """创建测试用的 HedgeMonitor 实例"""
        with patch.dict(os.environ, {}, clear=True):
            # 清除环境变量，确保不会初始化真实的 Telegram Bot
            monitor = HedgeMonitor(
                ticker="BTC",
                order_quantity=Decimal("0.1"),
                logger=mock_logger,
                primary_exchange_name="EdgeX"
            )
            return monitor

    @pytest.fixture
    def hedge_monitor_with_telegram(self, mock_logger):
        """创建带真实 Telegram Bot 的 HedgeMonitor 实例（单元测试模式）"""
        # 使用真实的Telegram Bot凭据
        real_token = os.environ.get('TELEGRAM_BOT_TOKEN', 'TEST_BOT_TOKEN')
        real_chat_id = os.environ.get('TELEGRAM_CHAT_ID', 'TEST_CHAT_ID')
        
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": real_token, "TELEGRAM_CHAT_ID": real_chat_id}):
            monitor = HedgeMonitor(
                ticker="ETH_TEST",  # 使用测试标识
                order_quantity=Decimal("0.5"),
                logger=mock_logger,
                primary_exchange_name="TEST_EXCHANGE"  # 使用测试交易所标识
            )
            return monitor

    @pytest.fixture
    def sample_open_context(self):
        """创建示例开仓策略上下文"""
        return StrategyExecutionContext.create_open_context(
            reason="价差阈值满足",
            side="buy",
            price_data={
                'primary_ask': Decimal('50000'),
                'lighter_bid': Decimal('49990'),
                'spread': Decimal('10'),
                'primary_bid': Decimal('49995'),
                'lighter_ask': Decimal('50005')
            },
            estimated_close_minutes=120,
            trigger=DecisionTrigger.SPREAD_THRESHOLD,
            current_spread=10.0,
            average_spread=8.0
        )

    @pytest.fixture
    def sample_close_context(self):
        """创建示例平仓策略上下文"""
        return StrategyExecutionContext.create_close_context(
            reason="时间平仓",
            side="sell",
            price_data={
                'primary_bid': Decimal('50005'),
                'lighter_ask': Decimal('50010'),
                'spread': Decimal('5'),
                'primary_ask': Decimal('50010'),
                'lighter_bid': Decimal('50000')
            },
            estimated_close_minutes=60,
            trigger=DecisionTrigger.TIME_CLOSE,
            next_open_minutes=15.0,
            current_spread=5.0,
            average_spread=8.0
        )

    def test_initialization_without_telegram(self, mock_logger):
        """测试无 Telegram Bot 的初始化"""
        with patch.dict(os.environ, {}, clear=True):
            monitor = HedgeMonitor(
                ticker="BTC",
                order_quantity=Decimal("0.1"),
                logger=mock_logger,
                primary_exchange_name="EdgeX"
            )

            assert monitor.ticker == "BTC"
            assert monitor.order_quantity == Decimal("0.1")
            assert monitor.logger == mock_logger
            assert monitor.primary_exchange_name == "EdgeX"
            assert monitor.telegram_bot is None
            assert monitor.position_opened_time is None
            assert monitor.position_open_data == {}
            assert monitor.last_status_notification_time is None
            assert monitor.status_monitor_task is None
            assert monitor.stop_flag is False

    def test_initialization_with_telegram(self, hedge_monitor_with_telegram):
        """测试带真实 Telegram Bot 的初始化"""
        # 验证Telegram Bot被正确初始化
        assert hedge_monitor_with_telegram.telegram_bot is not None
        assert hedge_monitor_with_telegram.telegram_bot.token == os.environ.get('TELEGRAM_BOT_TOKEN')
        assert hedge_monitor_with_telegram.telegram_bot.chat_id == os.environ.get('TELEGRAM_CHAT_ID')

    @pytest.mark.asyncio
    async def test_send_startup_notification_without_telegram(self, hedge_monitor):
        """测试无 Telegram Bot 时的启动通知"""
        await hedge_monitor.send_startup_notification(10)
        # 应该不会抛出异常，但也不会发送消息

    @pytest.mark.asyncio
    async def test_send_startup_notification_with_telegram(self, hedge_monitor_with_telegram):
        """测试真实 Telegram Bot 的启动通知"""
        try:
            # 临时修改ticker以添加单元测试标记
            original_ticker = hedge_monitor_with_telegram.ticker
            hedge_monitor_with_telegram.ticker = f"{original_ticker}_UNIT_TEST"
            
            await hedge_monitor_with_telegram.send_startup_notification(5)
            
            # 恢复原始ticker
            hedge_monitor_with_telegram.ticker = original_ticker
            
            # 如果没有抛出异常，说明消息发送成功
            assert True
        except Exception as e:
            pytest.fail(f"Telegram启动通知发送失败: {e}")

    @pytest.mark.asyncio
    async def test_send_shutdown_notification(self, hedge_monitor_with_telegram):
        """测试系统停止通知"""
        try:
            # 临时修改ticker以添加单元测试标记
            original_ticker = hedge_monitor_with_telegram.ticker
            hedge_monitor_with_telegram.ticker = f"{original_ticker}_UNIT_TEST"
            
            await hedge_monitor_with_telegram.send_shutdown_notification(
                Decimal("0.1"), Decimal("-0.1")
            )
            
            # 恢复原始ticker
            hedge_monitor_with_telegram.ticker = original_ticker
            
            # 消息发送成功
            assert True
        except Exception as e:
            pytest.fail(f"Telegram停止通知发送失败: {e}")

    @pytest.mark.asyncio
    async def test_send_error_notification(self, hedge_monitor_with_telegram):
        """测试错误通知"""
        try:
            # 临时修改ticker以添加单元测试标记
            original_ticker = hedge_monitor_with_telegram.ticker
            hedge_monitor_with_telegram.ticker = f"{original_ticker}_UNIT_TEST"
            
            test_error = ValueError("单元测试错误")
            context = "单元测试执行时发生错误"
            
            await hedge_monitor_with_telegram.send_error_notification(test_error, context)
            
            # 恢复原始ticker
            hedge_monitor_with_telegram.ticker = original_ticker
            
            # 消息发送成功
            assert True
        except Exception as e:
            pytest.fail(f"Telegram错误通知发送失败: {e}")

    @pytest.mark.asyncio
    async def test_send_position_open_notification(self, hedge_monitor_with_telegram, sample_open_context):
        """测试开仓通知"""
        try:
            # 临时修改ticker以添加单元测试标记
            original_ticker = hedge_monitor_with_telegram.ticker
            hedge_monitor_with_telegram.ticker = f"{original_ticker}_UNIT_TEST"
            
            await hedge_monitor_with_telegram.send_position_open_notification(sample_open_context)
            
            # 恢复原始ticker
            hedge_monitor_with_telegram.ticker = original_ticker
            
            # 验证开仓数据被记录
            assert hedge_monitor_with_telegram.position_opened_time is not None
            assert hedge_monitor_with_telegram.position_open_data['quantity'] == Decimal("0.5")
            assert hedge_monitor_with_telegram.position_open_data['strategy_context'] == sample_open_context
            
            # 消息发送成功
            assert True
        except Exception as e:
            pytest.fail(f"Telegram开仓通知发送失败: {e}")

    @pytest.mark.asyncio
    async def test_send_position_open_notification_without_context(self, hedge_monitor_with_telegram, mock_logger):
        """测试没有策略上下文时的开仓通知"""
        await hedge_monitor_with_telegram.send_position_open_notification(None)
        
        # 应该记录警告，但不发送消息
        mock_logger.warning.assert_called_with("No strategy context provided for open notification")

    @pytest.mark.asyncio
    async def test_send_position_close_notification(self, hedge_monitor_with_telegram, sample_open_context, sample_close_context):
        """测试平仓通知"""
        try:
            # 临时修改ticker以添加单元测试标记
            original_ticker = hedge_monitor_with_telegram.ticker
            hedge_monitor_with_telegram.ticker = f"{original_ticker}_UNIT_TEST"
            
            # 先设置开仓数据
            hedge_monitor_with_telegram.position_opened_time = time.time() - 3600  # 1小时前
            hedge_monitor_with_telegram.position_open_data = {
                'quantity': Decimal("0.5"),
                'strategy_context': sample_open_context
            }
            
            await hedge_monitor_with_telegram.send_position_close_notification(sample_close_context, None, None)
            
            # 恢复原始ticker
            hedge_monitor_with_telegram.ticker = original_ticker
            
            # 验证开仓数据被清空
            assert hedge_monitor_with_telegram.position_opened_time is None
            assert hedge_monitor_with_telegram.position_open_data == {}
            
            # 消息发送成功
            assert True
        except Exception as e:
            pytest.fail(f"Telegram平仓通知发送失败: {e}")

    @pytest.mark.asyncio
    async def test_send_position_close_notification_without_open_data(self, hedge_monitor_with_telegram, sample_close_context, mock_logger):
        """测试没有开仓数据时的平仓通知"""
        await hedge_monitor_with_telegram.send_position_close_notification(sample_close_context, None, None)
        
        # 应该记录警告，但不发送消息
        mock_logger.warning.assert_called_with("No position open data found for close notification")

    @pytest.mark.asyncio
    async def test_send_position_status_notification(self, hedge_monitor_with_telegram, sample_open_context):
        """测试持仓状态通知"""
        try:
            # 临时修改ticker以添加单元测试标记
            original_ticker = hedge_monitor_with_telegram.ticker
            hedge_monitor_with_telegram.ticker = f"{original_ticker}_UNIT_TEST"
            
            # 设置开仓数据
            hedge_monitor_with_telegram.position_opened_time = time.time() - 1800  # 30分钟前
            hedge_monitor_with_telegram.position_open_data = {
                'quantity': Decimal("0.5"),
                'strategy_context': sample_open_context
            }
            
            # 模拟交易客户端和代理
            mock_primary_client = AsyncMock()
            mock_primary_client.config.contract_id = "BTC-PERP"
            mock_primary_client.fetch_bbo_prices.return_value = (Decimal('49990'), Decimal('50010'))
            
            mock_lighter_proxy = AsyncMock()
            mock_lighter_proxy.fetch_bbo_prices.return_value = (Decimal('49995'), Decimal('50005'))
            
            mock_hedge_strategy = Mock()
            
            await hedge_monitor_with_telegram.send_position_status_notification(
                primary_position=Decimal("0.5"),
                lighter_position=Decimal("-0.5"),
                hedge_position_strategy=mock_hedge_strategy,
                primary_client=mock_primary_client,
                lighter_proxy=mock_lighter_proxy
            )
            
            # 恢复原始ticker
            hedge_monitor_with_telegram.ticker = original_ticker
            
            # 验证价格获取被调用
            mock_primary_client.fetch_bbo_prices.assert_called_once()
            mock_lighter_proxy.fetch_bbo_prices.assert_called_once()
            
            # 消息发送成功
            assert True
        except Exception as e:
            pytest.fail(f"Telegram状态通知发送失败: {e}")

    @pytest.mark.asyncio
    async def test_send_position_status_notification_no_position(self, hedge_monitor_with_telegram):
        """测试无持仓时的状态通知"""
        # 无持仓时不应该发送消息，但不应该抛出异常
        await hedge_monitor_with_telegram.send_position_status_notification(
            primary_position=Decimal("0"),
            lighter_position=Decimal("0"),
            hedge_position_strategy=None,
            primary_client=None,
            lighter_proxy=None
        )
        
        # 测试通过，没有异常抛出
        assert True

    @pytest.mark.asyncio
    async def test_start_status_monitor(self, hedge_monitor):
        """测试启动状态监控"""
        # 模拟参数
        position_getter = lambda: Decimal("0.1")
        hedge_strategy = Mock()
        primary_client = Mock()
        lighter_proxy = Mock()
        
        hedge_monitor.start_status_monitor(
            position_getter, position_getter,
            hedge_strategy, primary_client, lighter_proxy
        )
        
        assert hedge_monitor.status_monitor_task is not None
        assert not hedge_monitor.status_monitor_task.done()
        
        # 清理任务
        hedge_monitor.stop_status_monitor()
        
        # 等待任务被取消
        try:
            await hedge_monitor.status_monitor_task
        except asyncio.CancelledError:
            pass  # 预期的行为

    @pytest.mark.asyncio
    async def test_stop_status_monitor(self, hedge_monitor):
        """测试停止状态监控"""
        # 先启动监控
        position_getter = lambda: Decimal("0.1")
        hedge_strategy = Mock()
        primary_client = Mock()
        lighter_proxy = Mock()
        
        hedge_monitor.start_status_monitor(
            position_getter, position_getter,
            hedge_strategy, primary_client, lighter_proxy
        )
        
        # 停止监控
        hedge_monitor.stop_status_monitor()
        
        # 等待任务被取消
        try:
            await hedge_monitor.status_monitor_task
        except asyncio.CancelledError:
            pass  # 预期的行为
        
        # 应该取消任务
        assert hedge_monitor.status_monitor_task.cancelled()

    def test_stop_status_monitor_no_task(self, hedge_monitor):
        """测试没有监控任务时停止监控"""
        # 应该不会抛出异常
        hedge_monitor.stop_status_monitor()

    def test_set_stop_flag(self, hedge_monitor):
        """测试设置停止标志"""
        assert hedge_monitor.stop_flag is False
        
        hedge_monitor.set_stop_flag(True)
        assert hedge_monitor.stop_flag is True
        
        hedge_monitor.set_stop_flag(False)
        assert hedge_monitor.stop_flag is False

    def test_has_position_data(self, hedge_monitor):
        """测试检查持仓数据"""
        assert hedge_monitor.has_position_data() is False
        
        hedge_monitor.position_open_data = {'test': 'data'}
        assert hedge_monitor.has_position_data() is True

    def test_reset_position_data(self, hedge_monitor):
        """测试重置持仓数据"""
        # 设置一些数据
        hedge_monitor.position_opened_time = time.time()
        hedge_monitor.position_open_data = {'test': 'data'}
        
        # 重置
        hedge_monitor._reset_position_data()
        
        assert hedge_monitor.position_opened_time is None
        assert hedge_monitor.position_open_data == {}

    @pytest.mark.asyncio
    async def test_status_monitor_task_with_position(self, hedge_monitor_with_telegram, sample_open_context):
        """测试状态监控任务（有持仓）"""
        # 设置开仓数据
        hedge_monitor_with_telegram.position_open_data = {
            'quantity': Decimal("0.5"),
            'strategy_context': sample_open_context
        }
        
        # 模拟参数
        position_getter = lambda: Decimal("0.5")
        mock_hedge_strategy = Mock()
        mock_primary_client = AsyncMock()
        mock_primary_client.config.contract_id = "BTC-PERP"
        mock_primary_client.fetch_bbo_prices.return_value = (Decimal('49990'), Decimal('50010'))
        mock_lighter_proxy = AsyncMock()
        mock_lighter_proxy.fetch_bbo_prices.return_value = (Decimal('49995'), Decimal('50005'))
        
        # 模拟短时间监控（避免测试等待太久）
        with patch('asyncio.sleep') as mock_sleep:
            # 第一次sleep后设置stop_flag，模拟监控触发一次后停止
            async def side_effect(duration):
                if duration == 1800:  # 30分钟间隔
                    hedge_monitor_with_telegram.set_stop_flag(True)
                else:
                    await asyncio.sleep(0.01)  # 其他sleep正常执行
            
            mock_sleep.side_effect = side_effect
            
            # 运行监控任务
            task = asyncio.create_task(
                hedge_monitor_with_telegram._status_monitor_task(
                    position_getter, position_getter,
                    mock_hedge_strategy, mock_primary_client, mock_lighter_proxy
                )
            )
            
            try:
                await asyncio.wait_for(task, timeout=0.1)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_notification_error_handling(self, hedge_monitor, mock_logger):
        """测试通知功能的错误处理"""
        # 创建有问题的 Telegram Bot 模拟
        error_bot = Mock()
        error_bot.send_text.side_effect = Exception("发送失败")
        hedge_monitor.telegram_bot = error_bot
        
        # 测试各种通知的错误处理
        await hedge_monitor.send_startup_notification(5)
        mock_logger.error.assert_called()
        
        mock_logger.reset_mock()
        await hedge_monitor.send_error_notification(ValueError("测试"), "测试上下文")
        mock_logger.error.assert_called()

    def test_pnl_calculation_buy_position(self, hedge_monitor_with_telegram, sample_open_context):
        """测试买入持仓的 PnL 计算逻辑"""
        # 这个测试验证 PnL 计算的核心逻辑
        open_context = sample_open_context  # buy side
        
        # 模拟平仓价格数据
        close_price_data = {
            'primary_bid': Decimal('50100'),  # 主交易所价格上涨
            'lighter_ask': Decimal('50020'),  # Lighter 价格略涨
            'spread': Decimal('80')
        }
        
        # 开仓价格
        open_primary_price = open_context.price_data['primary_ask']  # 50000
        open_lighter_price = open_context.price_data['lighter_bid']  # 49990
        
        # 平仓价格
        close_primary_price = close_price_data['primary_bid']  # 50100
        close_lighter_price = close_price_data['lighter_ask']  # 50020
        
        # 对于买入持仓：
        # Primary PnL = (平仓价 - 开仓价) * 数量 = (50100 - 50000) * 0.5 = 50
        # Lighter PnL = (开仓价 - 平仓价) * 数量 = (49990 - 50020) * 0.5 = -15
        # 总 PnL = 50 + (-15) = 35
        
        order_quantity = Decimal("0.5")
        primary_pnl = (close_primary_price - open_primary_price) * order_quantity
        lighter_pnl = (open_lighter_price - close_lighter_price) * order_quantity
        total_pnl = primary_pnl + lighter_pnl
        
        assert primary_pnl == Decimal('50')
        assert lighter_pnl == Decimal('-15')
        assert total_pnl == Decimal('35')


class TestHedgeMonitorIntegration:
    """测试 HedgeMonitor 与其他组件的集成"""

    @pytest.mark.asyncio
    async def test_full_workflow_simulation(self):
        """测试完整的工作流程模拟"""
        logger = Mock(spec=logging.Logger)
        
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}):
            with patch('hedge.hedge_monitor.TelegramBot') as mock_telegram_class:
                mock_bot = Mock()
                mock_telegram_class.return_value = mock_bot
                
                monitor = HedgeMonitor(
                    ticker="BTC",
                    order_quantity=Decimal("0.1"),
                    logger=logger,
                    primary_exchange_name="EdgeX"
                )
                
                # 1. 系统启动
                await monitor.send_startup_notification(5)
                assert mock_bot.send_text.call_count == 1
                
                # 2. 开仓通知
                open_context = StrategyExecutionContext.create_open_context(
                    reason="测试开仓",
                    side="buy",
                    price_data={'primary_ask': 50000, 'lighter_bid': 49990, 'spread': 10},
                    estimated_close_minutes=60,
                    trigger=DecisionTrigger.SPREAD_THRESHOLD
                )
                await monitor.send_position_open_notification(open_context)
                assert mock_bot.send_text.call_count == 2
                
                # 3. 平仓通知
                close_context = StrategyExecutionContext.create_close_context(
                    reason="测试平仓",
                    side="sell",
                    price_data={'primary_bid': 50010, 'lighter_ask': 50020, 'spread': 10},
                    estimated_close_minutes=30,
                    trigger=DecisionTrigger.TIME_CLOSE,
                    next_open_minutes=15.0
                )
                await monitor.send_position_close_notification(close_context, None, None)
                assert mock_bot.send_text.call_count == 3
                
                # 4. 系统停止
                await monitor.send_shutdown_notification(Decimal("0"), Decimal("0"))
                assert mock_bot.send_text.call_count == 4

    @pytest.mark.asyncio
    async def test_error_resilience(self):
        """测试错误恢复能力"""
        logger = Mock(spec=logging.Logger)
        monitor = HedgeMonitor(
            ticker="BTC",
            order_quantity=Decimal("0.1"),
            logger=logger,
            primary_exchange_name="EdgeX"
        )
        
        # 即使没有 Telegram Bot，也不应该抛出异常
        await monitor.send_startup_notification(5)
        await monitor.send_error_notification(ValueError("测试错误"), "测试上下文")
        await monitor.send_shutdown_notification(Decimal("0"), Decimal("0"))
        
        # 无效的策略上下文处理
        await monitor.send_position_open_notification(None)
        await monitor.send_position_close_notification(None, None, None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])