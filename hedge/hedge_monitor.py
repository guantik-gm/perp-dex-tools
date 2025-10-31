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

from hedge.hedge_strategy import HedgeStrategy
from helpers.telegram_bot import TelegramBot

if TYPE_CHECKING:
    from .hedge_strategy import StrategyExecutionContext

class HedgeMonitor:
    """对冲交易监控器 - 统一管理通知和状态监控功能"""
    
    def __init__(self, ticker: str, order_quantity: Decimal, logger: logging.Logger,
                 primary_exchange_name: str, hedge_bot_order_handler=None, hedge_bot_fee_rate_handler=None):
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
        self.position_open_data: Dict[str, Any] = {}
        self.last_status_notification_time: Optional[float] = None
        self.primary_open_price = None
        self.primary_open_quantity = None
        self.primary_open_side = None
        self.primary_close_price = None
        self.primary_close_quantity = None
        self.primary_close_side = None
        self.lighter_open_price = None
        self.lighter_open_quantity = None
        self.lighter_open_side = None
        self.lighter_close_price = None
        self.lighter_close_quantity = None
        self.lighter_close_side = None

        # 状态监控任务
        self.status_monitor_task: Optional[asyncio.Task] = None
        self.stop_flag = False

        self.hedge_bot_order_handler = hedge_bot_order_handler
        self.hedge_bot_fee_rate_handler = hedge_bot_fee_rate_handler

        self.logger.info("📡 HedgeMonitor 初始化完成")

    def get_current_order(self):
        if self.hedge_bot_order_handler:
            return self.hedge_bot_order_handler()
        else:
            raise ValueError("Hedge bot order handler is not set in HedgeMonitor")
        
    def get_primary_fee_rate(self):
        if self.hedge_bot_fee_rate_handler:
            return self.hedge_bot_fee_rate_handler()
        else:
            raise ValueError("Hedge bot fee rate handler is not set in HedgeMonitor")
        
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

    async def handle_stuck_order(self, order_id: str, order_price: Decimal, side: str,
                               consecutive_timeouts: int) -> None:
        """
        处理卡单情况并发送一次 Telegram 告警
        
        Args:
            order_id: 当前订单ID
            order_price: 订单价格
            side: 订单方向
            best_bid: 最佳买价
            best_ask: 最佳卖价
            elapsed_time: 当前订单已经等待的时间
            consecutive_timeouts: 连续超时次数
        """
        # 计算卡单总时长（每次超时10秒 * 连续次数）
        total_stuck_time = consecutive_timeouts * 10
        
        self.logger.error(f"🚨 检测到卡单！连续超时 {consecutive_timeouts} 次，卡单时长约 {total_stuck_time} 秒")
        self.logger.error(f"🚨 订单 {order_id} 可能卡住了，WebSocket 未返回最新状态")
        
        # 发送一次 Telegram 告警
        if self.telegram_bot:
            try:
                side_display = "买入" if side == 'buy' else "卖出"
                
                # 构建告警消息
                alert_msg = f"🚨 [{self.primary_exchange_name}_{self.ticker}] 卡单告警\n" \
                           f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                           f"🕐 {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                           f"📋 订单ID: {order_id}\n" \
                           f"🏷️ {side_display} ${order_price}\n" \
                           f"📊 连续超时: {consecutive_timeouts}次 (约{total_stuck_time}秒)\n" \
                           f"⚠️ WebSocket 未返回订单状态更新"
                
                self.telegram_bot.send_text(alert_msg)
                self.logger.info("✅ 卡单告警已发送")
            except Exception as e:
                self.logger.error(f"❌ 发送卡单告警失败: {e}")

    async def send_position_open_notification(self, strategy: HedgeStrategy) -> None:
        """发送开仓通知 - 使用策略提供的完整信息"""
        if not self.telegram_bot:
            return
            
        try:
            # 从策略获取完整的通知内容
            content = strategy.get_content()
            if not content:
                self.logger.warning("No notification content from triggered strategy")
                return
            
            # 从价格数据获取具体价格
            price_data = content.get('price_data', {})
            spread = price_data.get('spread', 0)
            
            # 确定对冲方向
            side = content['side']
            lighter_side = 'sell' if side == 'buy' else 'buy'
            
            self.primary_open_price = self.get_current_order().current_primary_price
            self.primary_open_quantity = self.get_current_order().current_primary_quantity
            self.primary_open_side = side
            self.lighter_open_price = self.get_current_order().current_lighter_price
            self.lighter_open_quantity = self.get_current_order().current_lighter_quantity
            self.lighter_open_side = lighter_side
            
            # 构建基础通知模板
            open_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式 - [开仓执行通知]\n" \
                     f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                     f"🕐 开仓时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                     f"📈 策略原因: {content['reason']}\n" \
                     f"🏭 {self.primary_exchange_name} 开仓方向: {self.primary_open_side.upper()}, 持仓数量: {self.primary_open_quantity}, 成交价: ${self.primary_open_price:.6f}\n" \
                     f"💡 Lighter 开仓方向: {self.lighter_open_side.upper()}, 持仓数量: {self.lighter_open_quantity}, 成交价: ${self.lighter_open_price:.6f}\n" \
                     f"💰 当前价差: ${spread:.6f}\n"
            
            # 添加策略详情
            tg_msg = content.get('tg_msg', [])
            if tg_msg:
                open_msg += f"\n\n📋 策略详情:\n" + "\n".join(tg_msg)
            
            self.telegram_bot.send_text(open_msg)
            
            # 记录开仓数据用于后续平仓通知
            self.position_open_data = {
                'quantity': self.order_quantity,
                'strategy_content': content
            }
                
        except Exception as e:
            self.logger.error(f"Failed to send position open notification: {e}")

    async def send_position_close_notification(self, strategy: HedgeStrategy, primary_client=None, lighter_proxy=None) -> None:
        """发送平仓通知 - 使用策略提供的完整信息"""
        if not self.telegram_bot:
            return
            
        try:
            if not self.position_open_data:
                self.logger.warning("No position open data found for close notification")
                return

            # 从策略获取完整的通知内容
            content = strategy.get_content()
            if not content:
                self.logger.warning("No notification content from triggered strategy")
                return
                
            # 从价格数据获取具体价格
            price_data = content.get('price_data', {})
            side = content['side']
            close_reason = content['reason']
            
            self.primary_close_price = self.get_current_order().current_primary_price
            self.primary_close_quantity = self.get_current_order().current_primary_quantity
            self.primary_close_side = side
            self.lighter_close_price = self.get_current_order().current_lighter_price
            self.lighter_close_quantity = self.get_current_order().current_lighter_quantity
            self.lighter_close_side = 'sell' if side == 'buy' else 'buy'
            
            close_spread = price_data.get('spread', 0)
                
            # 获取开仓时的价格和本金信息
            open_content = self.position_open_data.get('strategy_content', {})
            open_spread = open_content.get('price_data', {}).get('spread', 0)
            
            # 计算双边开仓本金总和(默认20x杠杆)
            primary_capital = abs(self.primary_open_price * self.primary_open_quantity / 20)
            lighter_capital = abs(self.lighter_open_price * self.lighter_open_quantity / 20)
            total_capital = primary_capital + lighter_capital
            
            # 使用准确的PnL方法获取双边收益
            primary_pnl = Decimal('0')
            lighter_pnl = Decimal('0')
            
            try:
                if primary_client:
                    primary_pnl = await primary_client.get_ticker_position_pnl()
                    primary_pnl = primary_pnl / Decimal(100)
                    self.logger.info(f"✅ {self.primary_exchange_name} PnL: {primary_pnl}")
            except Exception as e:
                self.logger.warning(f"⚠️ 无法获取Primary PnL: {e}")
                
            if primary_pnl == 0:
                self.logger.info(f"获取 {self.primary_exchange_name} PnL 数据失败，使用订单价格进行计算")
                if self.primary_open_side == 'buy':
                    primary_pnl = (self.primary_close_price - self.primary_open_price) * self.primary_open_quantity
                else:
                    primary_pnl = (self.primary_open_price - self.primary_close_price) * self.primary_open_quantity
            primary_open_fee = abs(self.primary_open_price * self.primary_open_quantity * self.get_primary_fee_rate())
            primary_close_fee = abs(self.primary_close_price * self.primary_close_quantity * self.get_primary_fee_rate())
            primary_pnl -= (primary_open_fee + primary_close_fee)
            
            try:
                if lighter_proxy:
                    lighter_pnl = await lighter_proxy.get_ticker_position_pnl()
                    lighter_pnl = lighter_pnl / Decimal(100)
                    self.logger.info(f"✅ Lighter PnL: {lighter_pnl}")
            except Exception as e:
                self.logger.warning(f"⚠️ 无法获取Lighter PnL: {e}")
            
            if lighter_pnl == 0:
                self.logger.info(f"获取 Lighter PnL 数据失败，使用订单价格进行计算")
                if self.lighter_close_price < 0:
                    self.logger.info(f"Lighter 平仓订单信息获取失败，可能是WebSocket超时未返回，跳过 Lighter PnL计算")
                    lighter_pnl = None
                else:
                    if self.lighter_open_side == 'buy':
                        lighter_pnl = (self.lighter_close_price - self.lighter_open_price) * self.lighter_open_quantity
                    else:
                        lighter_pnl = (self.lighter_open_price - self.lighter_close_price) * self.lighter_open_quantity

            # 计算总收益和收益率
            if lighter_pnl is None:
                total_pnl = "-"
                total_return_rate = "-"
            else:
                total_pnl = primary_pnl + lighter_pnl
                total_return_rate = (total_pnl / total_capital * 100) if total_capital > 0 else Decimal('0')
            
            close_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式 - [平仓执行通知]\n" \
                      f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                      f"🕐 平仓时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                      f"📈 平仓原因: {close_reason}\n" \
                     f"🏭 {self.primary_exchange_name} 开仓方向: {self.primary_open_side.upper()}, 持仓数量: {self.primary_open_quantity}, 成交价: ${self.primary_open_price:.6f}\n" \
                      f"🏭 {self.primary_exchange_name} 平仓方向: {self.primary_close_side.upper()}, 平仓数量: {self.primary_close_quantity}, 平仓价格: ${self.primary_close_price:.6f}\n" \
                     f"💡 Lighter 开仓方向: {self.lighter_open_side.upper()}, 持仓数量: {self.lighter_open_quantity}, 成交价: ${self.lighter_open_price:.6f}\n" \
                      f"💡 Lighter 平仓方向: {self.lighter_close_side.upper()}, 平仓数量: {self.lighter_close_quantity}, 平仓价格: ${self.lighter_close_price:.6f}\n" \
                      f"💰 开仓价差: ${open_spread:.6f} → 平仓价差: ${close_spread:.6f}\n" \
                      f"📊 双边收益明细:\n" \
                      f"   🏭 {self.primary_exchange_name} 开仓手续费: ${primary_open_fee:.4f}, 平仓手续费: ${primary_close_fee:.4f}, 总手续费: ${primary_open_fee + primary_close_fee:.4f}\n" \
                      f"   🏭 {self.primary_exchange_name} PnL: ${primary_pnl:.4f}\n" \
                      f"   💡 Lighter PnL: ${lighter_pnl:.4f}\n" \
                      f"   💯 总收益: ${total_pnl:.4f}\n" \
                      f"💎 投入本金: ${total_capital:.2f}\n" \
                      f"📈 总收益率: {total_return_rate:.4f}%\n"

            # 添加策略详情
            tg_msg = content.get('tg_msg', [])
            if tg_msg:
                close_msg += f"\n\n📋 策略详情:\n" + "\n".join(tg_msg)
            
            self.telegram_bot.send_text(close_msg)
            
            # 清空开仓数据
            self._reset_position_data()
                
        except Exception as e:
            self.logger.error(f"Failed to send position close notification: {e}")

    async def send_position_status_notification(self, primary_position: Decimal, lighter_position: Decimal,
                                              strategy: HedgeStrategy, primary_client, lighter_proxy) -> None:
        """发送持仓状态通知 - 使用策略提供的信息"""
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
            current_spread = abs(primary_mid - lighter_mid)   
            
            # 获取开仓策略内容
            open_content = self.position_open_data.get('strategy_content', {})
            open_side = open_content.get('side', 'buy')
            
            
            # 计算当前PnL
            if open_side == 'buy':
                primary_pnl = (primary_mid - self.primary_open_price) * abs(primary_position)
                lighter_pnl = (self.lighter_open_price - lighter_mid) * abs(lighter_position)
            else:
                primary_pnl = (self.primary_open_price - primary_mid) * abs(primary_position)
                lighter_pnl = (lighter_mid - self.lighter_open_price) * abs(lighter_position)
            
            total_pnl = primary_pnl + lighter_pnl
            
            # 发送状态通知
            if self.telegram_bot:
                # 获取触发类型的友好显示
                trigger_text = strategy.get_content().get('trigger_type', 'unknown')
                
                status_msg = f"🔄 [{self.primary_exchange_name}_{self.ticker}] 智能对冲模式\n" \
                           f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                           f"📊 持仓状态报告\n" \
                           f"🕐 报告时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                           f"📈 策略触发: {trigger_text}\n" \
                           f"🏭 Primary({self.primary_exchange_name}): 持仓 {primary_position}\n" \
                           f"   开仓价: ${self.primary_open_price } | 市价: ${primary_mid}\n" \
                           f"💡 Lighter: 持仓 {lighter_position}\n" \
                           f"   开仓价: ${self.lighter_open_price } | 市价: ${lighter_mid}\n" \
                           f"💰 当前价差: ${current_spread}\n" \
                           f"📊 实时盈亏: ${total_pnl:.4f}\n"
                
                self.telegram_bot.send_text(status_msg)
            
        except Exception as e:
            self.logger.error(f"Failed to send position status notification: {e}")

    async def _status_monitor_task(self, primary_position_getter, lighter_position_getter, 
                                 strategy: HedgeStrategy, primary_client, lighter_proxy):
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
                        primary_pos, lighter_pos, strategy, primary_client, lighter_proxy
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
                           strategy: HedgeStrategy, primary_client, lighter_proxy):
        """启动状态监控任务"""
        if self.status_monitor_task is None or self.status_monitor_task.done():
            self.status_monitor_task = asyncio.create_task(
                self._status_monitor_task(
                    primary_position_getter, lighter_position_getter,
                    strategy, primary_client, lighter_proxy
                )
            )

    def stop_status_monitor(self):
        """停止状态监控任务"""
        if self.status_monitor_task and not self.status_monitor_task.done():
            self.status_monitor_task.cancel()

    def _reset_position_data(self):
        """重置持仓数据"""
        self.position_open_data = {}

    def set_stop_flag(self, stop: bool):
        """设置停止标志"""
        self.stop_flag = stop