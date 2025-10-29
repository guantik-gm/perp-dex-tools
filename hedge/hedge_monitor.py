"""
对冲交易监控模块

负责统一管理所有通知和状态监控相关功能，提供清晰的接口供 HedgeBotAbc 调用。
"""

import asyncio
import os
import time
import logging
from decimal import Decimal
from typing import Optional, Dict, Any, TYPE_CHECKING

from helpers.telegram_bot import TelegramBot

if TYPE_CHECKING:
    from .hedge_strategy import StrategyExecutionContext

class HedgeMonitor:
    """对冲交易监控器 - 统一管理通知和状态监控功能"""
    
    def __init__(self, ticker: str, order_quantity: Decimal, logger: logging.Logger,
                 primary_exchange_name: str):
        """
        初始化监控器
        
        Args:
            ticker: 交易币种
            order_quantity: 交易数量 
            logger: 日志器
            primary_exchange_name: 主交易所名称
            telegram_bot: Telegram机器人（可选）
        """
        self.ticker = ticker
        self.order_quantity = order_quantity
        self.logger = logger
        self.primary_exchange_name = primary_exchange_name
        # Initialize Telegram notifier
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            telegram_bot = TelegramBot(token, chat_id)
        else:
            telegram_bot = None
        self.telegram_bot = telegram_bot
        
        # 持仓追踪数据
        self.position_opened_time: Optional[float] = None
        self.position_open_data: Dict[str, Any] = {}
        self.last_status_notification_time: Optional[float] = None
        
        # 状态监控任务
        self.status_monitor_task: Optional[asyncio.Task] = None
        self.stop_flag = False
        
        self.logger.info("📡 HedgeMonitor 初始化完成")

    async def send_startup_notification(self, iterations: int) -> None:
        """发送系统启动通知"""
        if not self.telegram_bot:
            return
            
        try:
            startup_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式\n" \
                        f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                        f"📡 系统启动通知\n" \
                        f"🕐 启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                        f"💰 交易数量: {self.order_quantity}\n" \
                        f"🏭 交易所组合: {self.primary_exchange_name} + Lighter\n" \
                        f"📋 计划执行: {iterations}轮"
            self.telegram_bot.send_text(startup_msg)
        except Exception as e:
            self.logger.error(f"Failed to send startup notification: {e}")

    async def send_shutdown_notification(self, primary_position: Decimal, lighter_position: Decimal) -> None:
        """发送系统停止通知"""
        if not self.telegram_bot:
            return
            
        try:
            shutdown_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式\n" \
                         f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                         f"🛑 系统停止通知\n" \
                         f"🕐 停止时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                         f"📊 持仓状态: Primary={primary_position}, Lighter={lighter_position}"
            self.telegram_bot.send_text(shutdown_msg)
        except Exception as e:
            self.logger.error(f"Failed to send shutdown notification: {e}")

    async def send_error_notification(self, error: Exception, context: str) -> None:
        """发送错误通知"""
        if not self.telegram_bot:
            return
            
        try:
            error_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式\n" \
                     f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                     f"❌ 系统异常报告\n" \
                     f"🕐 异常时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                     f"🔴 错误信息: {str(error)}\n" \
                     f"📝 上下文: {context}"
            self.telegram_bot.send_text(error_msg)
        except Exception as notify_error:
            self.logger.error(f"Failed to send error notification: {notify_error}")

    async def send_position_open_notification(self, strategy_context: 'StrategyExecutionContext') -> None:
        """发送开仓通知，使用策略提供的执行上下文"""
        if not self.telegram_bot:
            return
            
        try:
            if not strategy_context:
                self.logger.warning("No strategy context provided for open notification")
                return
                
            # 从策略上下文获取数据
            reason = strategy_context.reason
            price_data = strategy_context.price_data
            side = strategy_context.side
            estimated_close_minutes = strategy_context.estimated_close_minutes
            
            # 从价格数据获取具体价格
            if price_data:
                primary_price = price_data.get('primary_ask' if side == 'buy' else 'primary_bid', 0)
                lighter_price = price_data.get('lighter_bid' if side == 'buy' else 'lighter_ask', 0)
                spread = price_data.get('spread', 0)
            else:
                # 备用方案：如果没有价格数据，显示无数据
                primary_price = 0
                lighter_price = 0 
                spread = 0
            
            # 确定对冲方向
            lighter_side = 'sell' if side == 'buy' else 'buy'
            
            open_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式\n" \
                     f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                     f"🚀 开仓执行通知\n" \
                     f"🕐 开仓时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                     f"📈 策略原因: {reason}\n" \
                     f"🏭 Primary({self.primary_exchange_name}): {side.upper()} {self.order_quantity} @ ${primary_price}\n" \
                     f"💡 Lighter: {lighter_side.upper()} {self.order_quantity} @ ${lighter_price}\n" \
                     f"💰 当前价差: ${spread}\n" \
                     f"⏰ 预计平仓: {estimated_close_minutes}分钟"
            
            self.telegram_bot.send_text(open_msg)
            
            # 记录开仓数据用于后续平仓通知
            self.position_opened_time = time.time()
            self.position_open_data = {
                'quantity': self.order_quantity,
                'strategy_context': strategy_context
            }
                
        except Exception as e:
            self.logger.error(f"Failed to send position open notification: {e}")

    async def send_position_close_notification(self, strategy_context: 'StrategyExecutionContext') -> None:
        """发送平仓通知，使用策略提供的执行上下文"""
        if not self.telegram_bot:
            return
            
        try:
            if not self.position_open_data:
                self.logger.warning("No position open data found for close notification")
                return
                
            if not strategy_context:
                self.logger.warning("No strategy context provided for close notification")
                return
                
            # 从策略上下文获取数据
            close_reason = strategy_context.reason
            price_data = strategy_context.price_data
            side = strategy_context.side
            next_open_minutes = strategy_context.next_open_minutes or 15
                
            # 计算持仓时间
            hold_time_minutes = 0
            if self.position_opened_time:
                hold_time_minutes = (time.time() - self.position_opened_time) / 60
            
            # 从价格数据获取具体价格
            if price_data:
                primary_price = price_data.get('primary_bid' if side == 'sell' else 'primary_ask', 0)
                lighter_price = price_data.get('lighter_ask' if side == 'sell' else 'lighter_bid', 0)
                close_spread = price_data.get('spread', 0)
            else:
                # 备用方案：如果没有价格数据，显示无数据
                primary_price = 0
                lighter_price = 0
                close_spread = 0
            
            # 计算PnL - 从策略上下文获取开仓价格
            open_strategy_context = self.position_open_data.get('strategy_context')
            if open_strategy_context and open_strategy_context.price_data:
                open_side = open_strategy_context.side
                open_primary_price = open_strategy_context.price_data.get('primary_ask' if open_side == 'buy' else 'primary_bid', Decimal('0'))
                open_lighter_price = open_strategy_context.price_data.get('lighter_bid' if open_side == 'buy' else 'lighter_ask', Decimal('0'))
                open_spread = open_strategy_context.current_spread or 0
            else:
                # 备用方案：如果没有策略上下文，使用默认值
                open_side = 'buy'
                open_primary_price = Decimal('0')
                open_lighter_price = Decimal('0')
                open_spread = 0
            
            # 根据开仓方向计算PnL
            if open_side == 'buy':
                # 开仓时买入primary，卖出lighter；平仓时卖出primary，买入lighter
                primary_pnl = (primary_price - open_primary_price) * self.order_quantity
                lighter_pnl = (open_lighter_price - lighter_price) * self.order_quantity
            else:
                # 开仓时卖出primary，买入lighter；平仓时买入primary，卖出lighter
                primary_pnl = (open_primary_price - primary_price) * self.order_quantity
                lighter_pnl = (lighter_price - open_lighter_price) * self.order_quantity
            
            total_pnl = primary_pnl + lighter_pnl
            
            # 确定对冲方向
            lighter_side = 'sell' if side == 'buy' else 'buy'
            
            close_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式\n" \
                      f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                      f"🎯 平仓执行通知\n" \
                      f"🕐 平仓时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                      f"📈 平仓原因: {close_reason}\n" \
                      f"🏭 Primary({self.primary_exchange_name}): {side.upper()} {self.order_quantity} @ ${primary_price}\n" \
                      f"💡 Lighter: {lighter_side.upper()} {self.order_quantity} @ ${lighter_price}\n" \
                      f"💰 开仓价差: ${open_spread} → 平仓价差: ${close_spread}\n" \
                      f"📊 交易盈亏: ${total_pnl:.4f}\n" \
                      f"⏱️ 持仓时长: {hold_time_minutes:.1f}分钟\n" \
                      f"🔄 下次开仓: 预计{next_open_minutes:.1f}分钟后"
            
            self.telegram_bot.send_text(close_msg)
            
            # 清空开仓数据
            self._reset_position_data()
                
        except Exception as e:
            self.logger.error(f"Failed to send position close notification: {e}")

    async def send_position_status_notification(self, primary_position: Decimal, lighter_position: Decimal,
                                              hedge_position_strategy, primary_client, lighter_proxy) -> None:
        """发送持仓状态通知，使用策略的采样方法获取实时价格"""
        try:
            if not self.position_open_data or primary_position == 0:
                return
            
            results = await asyncio.gather(
                # 获取EdgeX最优买卖价 - 需要传入contract_id
                primary_client.fetch_bbo_prices(primary_client.config.contract_id),
                # 获取Lighter最优买卖价 - 通过lighter_proxy获取
                lighter_proxy.fetch_bbo_prices()
            )
            primary_best_bid, primary_best_ask = results[0]
            lighter_best_bid, lighter_best_ask = results[1]
            
            primary_mid = (primary_best_bid + primary_best_ask) / 2
            lighter_mid = (lighter_best_bid + lighter_best_ask) / 2
            primary_market_price = (primary_best_bid + primary_best_ask) / 2
            lighter_market_price = (lighter_best_bid + lighter_best_ask) / 2 
            current_spread = abs(primary_mid - lighter_mid)   
            
            # 计算当前PnL - 从策略上下文获取开仓价格
            open_strategy_context = self.position_open_data.get('strategy_context')
            if open_strategy_context and open_strategy_context.price_data:
                open_side = open_strategy_context.side
                open_primary_price = open_strategy_context.price_data.get('primary_ask' if open_side == 'buy' else 'primary_bid', Decimal('0'))
                open_lighter_price = open_strategy_context.price_data.get('lighter_bid' if open_side == 'buy' else 'lighter_ask', Decimal('0'))
            else:
                # 备用方案：如果没有策略上下文，使用默认值
                open_side = 'buy'
                open_primary_price = Decimal('0')
                open_lighter_price = Decimal('0')
            
            if open_side == 'buy':
                primary_pnl = (primary_market_price - open_primary_price) * abs(primary_position)
                lighter_pnl = (open_lighter_price - lighter_market_price) * abs(lighter_position)
            else:
                primary_pnl = (open_primary_price - primary_market_price) * abs(primary_position)
                lighter_pnl = (lighter_market_price - open_lighter_price) * abs(lighter_position)
            
            total_pnl = primary_pnl + lighter_pnl
            
            # 计算剩余时间
            estimated_remaining_minutes = 30  # 默认值
            if hedge_position_strategy and self.position_opened_time:
                # 从策略上下文获取配置信息
                strategy_context = self.position_open_data.get('strategy_context')
                if strategy_context:
                    max_close_time = strategy_context.estimated_close_minutes
                    elapsed_minutes = (time.time() - self.position_opened_time) / 60
                    estimated_remaining_minutes = max(0, max_close_time - elapsed_minutes)
            
            # 发送状态通知
            if self.telegram_bot:
                # 获取触发原因的友好显示文本
                trigger_text = {
                    'spread_threshold': '价差阈值满足',
                    'time_driven': '时间驱动',
                    'timeout': '超时触发',
                    'risk_control': '风险控制',
                    'spread_close': '价差平仓',
                    'time_close': '时间平仓',
                    'error_timeout': '错误超时'
                }.get(strategy_context.trigger.value, strategy_context.trigger.value)
                
                status_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式\n" \
                           f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                           f"📊 持仓状态报告\n" \
                           f"🕐 报告时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                           f"📈 策略触发: {trigger_text}\n" \
                           f"🏭 Primary({self.primary_exchange_name}): 持仓 {primary_position}\n" \
                           f"   开仓价: ${open_primary_price} | 市价: ${primary_market_price}\n" \
                           f"💡 Lighter: 持仓 {lighter_position}\n" \
                           f"   开仓价: ${open_lighter_price} | 市价: ${lighter_market_price}\n" \
                           f"💰 当前价差: ${current_spread}\n" \
                           f"📊 实时盈亏: ${total_pnl:.4f}\n" \
                           f"⏰ 剩余时间: {estimated_remaining_minutes:.0f}分钟"
                
                self.telegram_bot.send_text(status_msg)
            
        except Exception as e:
            self.logger.error(f"Failed to send position status notification: {e}")

    async def _status_monitor_task(self, primary_position_getter, lighter_position_getter, 
                                 hedge_position_strategy, primary_client, lighter_proxy):
        """定时状态监控任务 - 每30分钟发送一次持仓状态"""
        self.logger.info("🔔 启动定时状态监控任务（30分钟间隔）")
        
        while not self.stop_flag:
            try:
                await asyncio.sleep(1800)  # 30分钟 = 1800秒
                
                if self.stop_flag:
                    break
                    
                # 只有在持仓时才发送状态通知
                primary_pos = primary_position_getter()
                lighter_pos = lighter_position_getter()
                
                if self.position_open_data and (primary_pos != 0 or lighter_pos != 0):
                    self.logger.info("📊 发送定时持仓状态通知")
                    await self.send_position_status_notification(
                        primary_pos, lighter_pos, hedge_position_strategy, primary_client, lighter_proxy
                    )
                    self.last_status_notification_time = time.time()
                
            except asyncio.CancelledError:
                self.logger.info("🔔 定时状态监控任务被取消")
                break
            except Exception as e:
                self.logger.error(f"❌ 定时状态监控任务异常: {e}")
                # 出错后等待5分钟再继续
                await asyncio.sleep(300)

    def start_status_monitor(self, primary_position_getter, lighter_position_getter,
                           hedge_position_strategy, primary_client, lighter_proxy):
        """启动状态监控任务"""
        if self.status_monitor_task is None or self.status_monitor_task.done():
            self.status_monitor_task = asyncio.create_task(
                self._status_monitor_task(
                    primary_position_getter, lighter_position_getter,
                    hedge_position_strategy, primary_client, lighter_proxy
                )
            )

    def stop_status_monitor(self):
        """停止状态监控任务"""
        if self.status_monitor_task and not self.status_monitor_task.done():
            self.status_monitor_task.cancel()

    def _reset_position_data(self):
        """重置持仓数据"""
        self.position_opened_time = None
        self.position_open_data = {}

    def set_stop_flag(self, stop: bool):
        """设置停止标志"""
        self.stop_flag = stop

    def has_position_data(self) -> bool:
        """检查是否有持仓数据"""
        return bool(self.position_open_data)