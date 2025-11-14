"""
对冲交易监控模块

负责统一管理所有通知和状态监控相关功能，提供清晰的接口供 HedgeBotAbc 调用。
"""

import asyncio
import os
import time
import logging
from decimal import Decimal
from typing import List, Optional

from hedge_v1.strategy.hedge_strategy import HedgeStrategy
from helpers.telegram_bot import TelegramBot


class HedgeMonitor:
    """对冲交易监控器 - 统一管理通知和状态监控功能"""
    
    def __init__(self, ticker: str, order_quantity: Decimal, logger: logging.Logger,
                 primary_exchange_name: str, primary_fee_rate=None, position_data_handler=None):
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
        self.primary_fee_rate = primary_fee_rate
        self.position_data_handler = position_data_handler
        # Initialize Telegram notifier
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            telegram_bot = TelegramBot(token, chat_id)
        else:
            telegram_bot = None
        self.telegram_bot = telegram_bot
        
        self.last_status_notification_time: Optional[float] = None

        # 状态监控任务
        self.stop_flag = False

        self.logger.info("📡 HedgeMonitor 初始化完成")

    async def send_startup_notification(self, iterations: int) -> None:
        """发送系统启动通知"""
        if not self.telegram_bot:
            return
            
        try:
            startup_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 智能对冲模式\n" \
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
            # 使用 position_data 中的统计信息
            stats_msg = ""
            if self.position_data_handler().total_trade_count > 0:
                # 计算平均统计
                avg_pnl = self.position_data_handler().total_pnl / self.position_data_handler().total_trade_count if self.position_data_handler().total_trade_count > 0 else Decimal('0')
                avg_return_rate = sum(self.position_data_handler().return_rate_history) / len(self.position_data_handler().return_rate_history) if self.position_data_handler().return_rate_history else Decimal('0')
                avg_wear_rate = sum(self.position_data_handler().wear_rate_history) / len(self.position_data_handler().wear_rate_history) if self.position_data_handler().wear_rate_history else Decimal('0')
                
                stats_msg = f"\n📈 本轮统计信息:\n" \
                           f"   🔢 总交易次数: {self.position_data_handler().total_trade_count}\n" \
                           f"   💰 总交易量: ${self.position_data_handler().total_trade_volume:.2f}\n" \
                           f"   💯 总收益: ${self.position_data_handler().total_pnl:.4f}\n" \
                           f"   📊 平均收益: ${avg_pnl:.4f}\n" \
                           f"   📈 平均收益率: {avg_return_rate:.4f}%\n" \
                           f"   ⚡ 平均磨损率: {avg_wear_rate:.4f}%"
            
            shutdown_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 智能对冲模式\n" \
                         f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                         f"🛑 系统停止通知\n" \
                         f"🕐 停止时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                         f"📊 持仓状态: Primary={primary_position}, Lighter={lighter_position}" \
                         f"{stats_msg}"
            
            self.telegram_bot.send_text(shutdown_msg)
        except Exception as e:
            self.logger.error(f"Failed to send shutdown notification: {e}")

    async def send_error_notification(self, error: Exception, context: str) -> None:
        """发送错误通知"""
        if not self.telegram_bot:
            return
            
        try:
            error_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 智能对冲模式\n" \
                     f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                     f"❌ 系统异常报告\n" \
                     f"🕐 异常时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                     f"🔴 错误信息: {str(error)}\n" \
                     f"📝 上下文: {context}"
            self.telegram_bot.send_text(error_msg)
        except Exception as notify_error:
            self.logger.error(f"Failed to send error notification: {notify_error}")

    async def send_position_open_notification(self, times: str, side: str, strategies: List[HedgeStrategy]) -> None:
        """发送开仓通知 - 使用策略提供的完整信息"""
        if not self.telegram_bot:
            return
        
        self.open_side = side
        self.open_triggered_strategies = strategies
        
        try:
            lighter_side = 'sell' if side == 'buy' else 'buy'
            
            # 使用新的 HedgePositionData 结构中的开仓属性
            self.primary_open_price = self.position_data_handler().current_primary_open_price
            self.primary_open_quantity = self.position_data_handler().current_primary_open_quantity
            self.primary_open_side = side
            self.lighter_open_price = self.position_data_handler().current_lighter_open_price
            self.lighter_open_quantity = self.position_data_handler().current_lighter_open_quantity
            self.lighter_open_side = lighter_side
            
            strategy_msgs = [f"+++++{strategy.name}+++++\n" + "\t\n".join(strategy.get_msgs()) for strategy in strategies]
            strategy_msg = "----------\n【触发策略列表】\n" + "\n\n".join(strategy_msgs)
            
            # 构建基础通知模板
            open_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 对冲模式第【{times}】次 - 【开仓执行通知】\n" \
                     f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                     f"🕐 开仓时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                     f"🏭 {self.primary_exchange_name} 开仓方向: {self.primary_open_side.upper()}, 持仓数量: {self.primary_open_quantity}, 开仓价格: ${self.primary_open_price:.6f}\n" \
                     f"💡 Lighter 开仓方向: {self.lighter_open_side.upper()}, 持仓数量: {self.lighter_open_quantity}, 开仓价格: ${self.lighter_open_price:.6f}\n" \
                     f"{strategy_msg}"
            
            self.telegram_bot.send_text(open_msg)
            
        except Exception as e:
            self.logger.error(f"Failed to send position open notification: {e}")

    async def send_position_close_notification(self, times: str, side: str, strategies: List[HedgeStrategy], primary_client=None, lighter_proxy=None) -> None:
        """发送平仓通知 - 使用 position_data 中的数据"""
        if not self.telegram_bot:
            return
        
        self.close_side = side
        self.close_triggered_strategies = strategies
        
        try:
            # 使用新的 HedgePositionData 结构中的平仓属性
            self.primary_close_price = self.position_data_handler().current_primary_close_price
            self.primary_close_quantity = self.position_data_handler().current_primary_close_quantity
            self.primary_close_side = side
            self.lighter_close_price = self.position_data_handler().current_lighter_close_price
            self.lighter_close_quantity = self.position_data_handler().current_lighter_close_quantity
            self.lighter_close_side = 'sell' if side == 'buy' else 'buy'
            
            # 使用 position_data 中的 PnL 数据
            primary_pnl = self.position_data_handler().current_primary_pnl
            lighter_pnl = self.position_data_handler().current_lighter_pnl
            total_pnl = self.position_data_handler().current_pnl
            total_volume = self.position_data_handler().current_trade_volume
            total_return_rate = self.position_data_handler().current_return_rate
            wear_rate = self.position_data_handler().current_wear_rate
            current_primary_position = self.position_data_handler().current_primary_position
            current_lighter_position = self.position_data_handler().current_lighter_position
            
            # 计算手续费用于显示
            fee_rate = Decimal(str(self.primary_fee_rate))
            primary_open_fee = Decimal(str(self.primary_open_price)) * Decimal(str(self.primary_open_quantity)) * fee_rate
            primary_close_fee = Decimal(str(self.primary_close_price)) * Decimal(str(self.primary_close_quantity)) * fee_rate
            
            # 预先格式化显示值
            lighter_pnl_str = "-" if lighter_pnl is None else f"${lighter_pnl:.4f}"
            total_pnl_str = "-" if total_pnl is None else f"${total_pnl:.4f}"
            total_return_rate_str = "-" if total_return_rate is None else f"{total_return_rate:.4f}%"
            wear_rate_str = "-" if wear_rate is None else f"{wear_rate:.4f}%"
            
            # 检查持仓异常
            position_warning = ""
            if abs(current_primary_position) > Decimal('0') or abs(current_lighter_position) > Decimal('0'):
                position_warning = "\n🚨 警告：平仓后持仓非零，请手动检查！"
            
            strategy_msgs = [f"+++++{strategy.name}+++++\n" + "\t\n".join(strategy.get_msgs()) for strategy in strategies]
            strategy_msg = "----------\n【触发策略列表】\n" + "\n\n".join(strategy_msgs)
            
            close_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 对冲模式第【{times}】次 - 【平仓执行通知】\n" \
                      f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                      f"🕐 平仓时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                     f"🏭 {self.primary_exchange_name} 开仓方向: {self.primary_open_side.upper()}, 持仓数量: {self.primary_open_quantity}, 开仓价格: ${self.primary_open_price:.6f}\n" \
                      f"🏭 {self.primary_exchange_name} 平仓方向: {self.primary_close_side.upper()}, 平仓数量: {self.primary_close_quantity}, 平仓价格: ${self.primary_close_price:.6f}\n" \
                     f"💡 Lighter 开仓方向: {self.lighter_open_side.upper()}, 持仓数量: {self.lighter_open_quantity}, 开仓价格: ${self.lighter_open_price:.6f}\n" \
                      f"💡 Lighter 平仓方向: {self.lighter_close_side.upper()}, 平仓数量: {self.lighter_close_quantity}, 平仓价格: ${self.lighter_close_price:.6f}\n" \
                      "----------\n" \
                      f"📊 双边收益明细:\n" \
                      f"   🏭 {self.primary_exchange_name} 开仓手续费: ${primary_open_fee:.4f}, 平仓手续费: ${primary_close_fee:.4f}, 总手续费: ${primary_open_fee + primary_close_fee:.4f}\n" \
                      f"   🏭 {self.primary_exchange_name} PnL: ${primary_pnl:.4f}\n" \
                      f"   💡 Lighter PnL: {lighter_pnl_str}\n" \
                      f"   💯 总收益: {total_pnl_str}\n" \
                      f"💎 双边投入本金: ${self.position_data_handler().current_capital:.2f} - 总收益率: {total_return_rate_str}\n" \
                      f"📈 单边交易量(开平仓): {total_volume} - 单边磨损率: {wear_rate_str}\n" \
                      f"📋 当前持仓状态: {self.primary_exchange_name}: {current_primary_position:.4f}, Lighter: {current_lighter_position:.4f}\n" \
                      f"{strategy_msg}{position_warning}"
            
            result = self.telegram_bot.send_text(close_msg)
            if not result.get('ok'):
                self.logger.info(f"telegram 通知消息失败: {close_msg}")
            
        except Exception as e:
            self.logger.error(f"Failed to send position close notification: {e}")

    def set_stop_flag(self, stop: bool):
        """设置停止标志"""
        self.stop_flag = stop