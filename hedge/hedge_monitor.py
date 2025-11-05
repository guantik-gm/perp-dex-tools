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

from hedge.strategy.hedge_strategy import HedgeStrategy
from helpers.telegram_bot import TelegramBot


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
       
        self.open_side = None 
        self.open_triggered_strategies = None
        self.close_side = None
        self.close_triggered_strategies = None

        # 状态监控任务
        self.status_monitor_task: Optional[asyncio.Task] = None
        self.stop_flag = False

        self.hedge_bot_order_handler = hedge_bot_order_handler
        self.hedge_bot_fee_rate_handler = hedge_bot_fee_rate_handler
        
        # 累计统计信息
        self.total_trade_count = 0  # 总交易次数
        self.total_trade_volume = Decimal('0')  # 总交易量
        self.total_profit_loss = Decimal('0')  # 总收益
        self.profit_loss_history = []  # 收益历史记录
        self.wear_rate_history = []  # 磨损率历史记录
        self.return_rate_history = []  # 收益率历史记录

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
    
    async def get_current_positions(self, primary_client, lighter_proxy):
        """获取当前持仓数量"""
        try:
            primary_position_value = Decimal('0')
            lighter_position_value = Decimal('0')
            
            # 获取Primary持仓
            try:
                if primary_client:
                    primary_position_value = await primary_client.get_ticker_position_value()
            except Exception as e:
                self.logger.warning(f"⚠️ 获取Primary持仓失败: {e}")
            
            # 获取Lighter持仓
            try:
                if lighter_proxy:
                    lighter_position_value = await lighter_proxy.get_ticker_position_value()
            except Exception as e:
                self.logger.warning(f"⚠️ 获取Lighter持仓失败: {e}")
            
            return primary_position_value, lighter_position_value
            
        except Exception as e:
            self.logger.error(f"❌ 获取持仓信息失败: {e}")
            return Decimal('0'), Decimal('0')
        
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
            # 计算统计信息
            avg_profit_loss = Decimal('0')
            avg_return_rate = Decimal('0')
            avg_wear_rate = Decimal('0')
            
            if self.profit_loss_history:
                avg_profit_loss = sum(self.profit_loss_history) / len(self.profit_loss_history)
            if self.return_rate_history:
                avg_return_rate = sum(self.return_rate_history) / len(self.return_rate_history)
            if self.wear_rate_history:
                avg_wear_rate = sum(self.wear_rate_history) / len(self.wear_rate_history)
            
            # 格式化统计信息
            stats_msg = ""
            if self.total_trade_count > 0:
                stats_msg = f"\n📈 本轮统计信息:\n" \
                           f"   🔢 总交易次数: {self.total_trade_count}\n" \
                           f"   💰 总交易量: ${self.total_trade_volume:.2f}\n" \
                           f"   💯 总收益: ${self.total_profit_loss:.4f}\n" \
                           f"   📊 平均收益: ${avg_profit_loss:.4f}\n" \
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
            
            self.primary_open_price = self.get_current_order().current_primary_price
            self.primary_open_quantity = self.get_current_order().current_primary_quantity
            self.primary_open_side = side
            self.lighter_open_price = self.get_current_order().current_lighter_price
            self.lighter_open_quantity = self.get_current_order().current_lighter_quantity
            self.lighter_open_side = lighter_side
            
            strategy_msgs = [f"【📋 策略: {strategy.name}】\n" + "\t\n".join(strategy.get_msgs()) for strategy in strategies]
            strategy_msg = "\n触发策略列表\n" + "\n".join(strategy_msgs)
            
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
        """发送平仓通知 - 使用策略提供的完整信息"""
        if not self.telegram_bot:
            return
        
        self.close_side = side
        self.close_triggered_strategies = strategies
        
        try:
            self.primary_close_price = self.get_current_order().current_primary_price
            self.primary_close_quantity = self.get_current_order().current_primary_quantity
            self.primary_close_side = side
            self.lighter_close_price = self.get_current_order().current_lighter_price
            self.lighter_close_quantity = self.get_current_order().current_lighter_quantity
            self.lighter_close_side = 'sell' if side == 'buy' else 'buy'
            
            # 计算双边开仓本金总和(默认20x杠杆)
            primary_capital = abs(Decimal(str(self.primary_open_price)) * Decimal(str(self.primary_open_quantity)) / Decimal('20'))
            lighter_capital = abs(Decimal(str(self.lighter_open_price)) * Decimal(str(self.lighter_open_quantity)) / Decimal('20'))
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
                    primary_pnl = (Decimal(str(self.primary_close_price)) - Decimal(str(self.primary_open_price))) * Decimal(str(self.primary_open_quantity))
                else:
                    primary_pnl = (Decimal(str(self.primary_open_price)) - Decimal(str(self.primary_close_price))) * Decimal(str(self.primary_open_quantity))
            primary_open_fee = abs(Decimal(str(self.primary_open_price)) * Decimal(str(self.primary_open_quantity)) * Decimal(str(self.get_primary_fee_rate())))
            primary_close_fee = abs(Decimal(str(self.primary_close_price)) * Decimal(str(self.primary_close_quantity)) * Decimal(str(self.get_primary_fee_rate())))
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
                        lighter_pnl = (Decimal(str(self.lighter_close_price)) - Decimal(str(self.lighter_open_price))) * Decimal(str(self.lighter_open_quantity))
                    else:
                        lighter_pnl = (Decimal(str(self.lighter_open_price)) - Decimal(str(self.lighter_close_price))) * Decimal(str(self.lighter_open_quantity))

            total_volume = Decimal(str(self.primary_open_price)) * Decimal(str(self.primary_open_quantity))
            # 计算总收益和收益率
            if lighter_pnl is None:
                total_pnl = None
                total_return_rate = None
                ware_rate = None
            else:
                total_pnl = primary_pnl + lighter_pnl
                total_return_rate = (total_pnl / total_capital * 100) if total_capital > 0 else Decimal('0')
                wear_rate = total_pnl / total_volume * 100
            
            # 累积统计数据
            self.total_trade_count += 1
            self.total_trade_volume += total_volume
            if total_pnl is not None:
                self.total_profit_loss += total_pnl
                self.profit_loss_history.append(total_pnl)
            if total_return_rate is not None:
                self.return_rate_history.append(total_return_rate)
            if wear_rate is not None:
                self.wear_rate_history.append(wear_rate)
            
            # 预先格式化显示值
            lighter_pnl_str = "-" if lighter_pnl is None else f"${lighter_pnl:.4f}"
            total_pnl_str = "-" if total_pnl is None else f"${total_pnl:.4f}"
            total_return_rate_str = "-" if total_return_rate is None else f"{total_return_rate:.4f}%"
            ware_rate_str = "-" if wear_rate is None else f"{wear_rate:.4f}%"
            
            # 获取当前持仓状态（理论上平仓后应该为0）
            current_primary_position, current_lighter_position = await self.get_current_positions(primary_client, lighter_proxy)
            
            # 检查持仓异常
            position_warning = ""
            if abs(current_primary_position) > Decimal('0') or abs(current_lighter_position) > Decimal('0'):
                position_warning = "\n🚨 警告：平仓后持仓非零，请手动检查！"
            
            strategy_msgs = [f"【📋 策略: {strategy.name}】\n" + "\t\n".join(strategy.get_msgs()) for strategy in strategies]
            strategy_msg = "\n触发策略列表\n" + "\n".join(strategy_msgs)
            
            close_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 对冲模式第【{times}】次 - 【平仓执行通知】\n" \
                      f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                      f"🕐 平仓时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                     f"🏭 {self.primary_exchange_name} 开仓方向: {self.primary_open_side.upper()}, 持仓数量: {self.primary_open_quantity}, 开仓价格: ${self.primary_open_price:.6f}\n" \
                      f"🏭 {self.primary_exchange_name} 平仓方向: {self.primary_close_side.upper()}, 平仓数量: {self.primary_close_quantity}, 平仓价格: ${self.primary_close_price:.6f}\n" \
                     f"💡 Lighter 开仓方向: {self.lighter_open_side.upper()}, 持仓数量: {self.lighter_open_quantity}, 开仓价格: ${self.lighter_open_price:.6f}\n" \
                      f"💡 Lighter 平仓方向: {self.lighter_close_side.upper()}, 平仓数量: {self.lighter_close_quantity}, 平仓价格: ${self.lighter_close_price:.6f}\n" \
                      f"📊 双边收益明细:\n" \
                      f"   🏭 {self.primary_exchange_name} 开仓手续费: ${primary_open_fee:.4f}, 平仓手续费: ${primary_close_fee:.4f}, 总手续费: ${primary_open_fee + primary_close_fee:.4f}\n" \
                      f"   🏭 {self.primary_exchange_name} PnL: ${primary_pnl:.4f}\n" \
                      f"   💡 Lighter PnL: {lighter_pnl_str}\n" \
                      f"   💯 总收益: {total_pnl_str}\n" \
                      f"💎 投入本金: ${total_capital:.2f} - 总收益率: {total_return_rate_str}\n" \
                      f"📈 单边交易量: {total_volume} - 单边磨损率: {ware_rate_str}\n" \
                      f"📋 当前持仓状态: {self.primary_exchange_name}: {current_primary_position:.4f}, Lighter: {current_lighter_position:.4f}\n" \
                      f"{strategy_msg}{position_warning}"
            
            result = self.telegram_bot.send_text(close_msg)
            self.logger.info(f"telegram 消息通知结果: {result}")
            if not result.get('ok'):
                self.logger.info(f"telegram 通知消息失败: {close_msg}")
            
        except Exception as e:
            self.logger.error(f"Failed to send position close notification: {e}")

    async def send_position_status_notification(self, primary_position: Decimal, lighter_position: Decimal,
                                              strategy: HedgeStrategy, primary_client, lighter_proxy) -> None:
        """发送持仓状态通知 - 使用策略提供的信息"""
        try:
            results = await asyncio.gather(
                # 获取EdgeX最优买卖价 - 需要传入contract_id
                primary_client.fetch_bbo_prices(primary_client.config.contract_id),
                # 获取Lighter最优买卖价 - 通过lighter_proxy获取
                lighter_proxy.fetch_bbo_prices()
            )
            primary_best_bid, primary_best_ask = results[0]
            lighter_best_bid, lighter_best_ask = results[1]
            
            primary_mid = (Decimal(str(primary_best_bid)) + Decimal(str(primary_best_ask))) / Decimal('2')
            lighter_mid = (Decimal(str(lighter_best_bid)) + Decimal(str(lighter_best_ask))) / Decimal('2')
            current_spread = abs(primary_mid - lighter_mid)   
            
            # 发送状态通知
            if self.telegram_bot:
                # 获取触发类型的友好显示
                trigger_text = strategy.reason or strategy.name or 'unknown'
                
                # 检查是否已经开仓
                if self.primary_open_price is None or self.lighter_open_price is None:
                    # 未开仓状态的通知
                    status_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 智能对冲模式\n" \
                               f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                               f"📊 持仓状态报告\n" \
                               f"🕐 报告时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                               f"📈 策略触发: {trigger_text}\n" \
                               f"⏳ 状态: 等待开仓信号\n" \
                               f"🏭 Primary({self.primary_exchange_name}): 持仓 {primary_position}\n" \
                               f"💡 Lighter: 持仓 {lighter_position}\n" \
                               f"💰 当前价差: ${current_spread:.6f}\n" \
                               f"📊 当前市价: Primary ${primary_mid:.6f} | Lighter ${lighter_mid:.6f}\n"
                else:
                    # 已开仓状态 - 计算PnL
                    if self.open_side == 'buy':
                        primary_pnl = (primary_mid - Decimal(str(self.primary_open_price))) * abs(Decimal(str(primary_position)))
                        lighter_pnl = (Decimal(str(self.lighter_open_price)) - lighter_mid) * abs(Decimal(str(lighter_position)))
                    else:
                        primary_pnl = (Decimal(str(self.primary_open_price)) - primary_mid) * abs(Decimal(str(primary_position)))
                        lighter_pnl = (lighter_mid - Decimal(str(self.lighter_open_price))) * abs(Decimal(str(lighter_position)))
                    
                    total_pnl = primary_pnl + lighter_pnl
                    
                    status_msg = f"🔄 【{self.primary_exchange_name}_{self.ticker}】 智能对冲模式\n" \
                               f"━━━━━━━━━━━━━━━━━━━━━━\n" \
                               f"📊 持仓状态报告\n" \
                               f"🕐 报告时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" \
                               f"📈 策略触发: {trigger_text}\n" \
                               f"🏭 Primary({self.primary_exchange_name}): 持仓 {primary_position}\n" \
                               f"   开仓价: ${self.primary_open_price:.6f} | 市价: ${primary_mid:.6f}\n" \
                               f"💡 Lighter: 持仓 {lighter_position}\n" \
                               f"   开仓价: ${self.lighter_open_price:.6f} | 市价: ${lighter_mid:.6f}\n" \
                               f"💰 当前价差: ${current_spread:.6f}\n" \
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
                
                if primary_pos != 0 or lighter_pos != 0:
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

    def set_stop_flag(self, stop: bool):
        """设置停止标志"""
        self.stop_flag = stop