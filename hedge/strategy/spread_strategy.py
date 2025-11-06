import time
import asyncio
import statistics
from typing import List
from decimal import Decimal

from hedge.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult

class SpreadStrategy(HedgeStrategy):
    """统一价差策略 - 自适应价差收敛与价格倒挂套利
    
    核心思路：
    1. 动态检测市场状态（Lighter>Primary 或 Primary>Lighter）
    2. 价差收敛：当Lighter更贵时，做空价差等待收敛
    3. 价格倒挂：当Primary更贵时，传统套利获取价差
    4. 统一执行逻辑：总是选择能获得正向价差的交易方向
    """
    
    def __init__(self, priority=10):
        super().__init__(open_priority=priority, close_priority=priority)
        self.current_spread_sample_count = 10  # 当前价差采样次数（默认值）
        self.profit_threshold = 0.05
        
        # trading_loop 中将会根据概述性决定primary开仓方向 
        self.open_side = None
        # 价差状态
        self.open_spread = None  # 预计开仓价差
        self.close_spread = None  # 预计平仓价差
        self.current_spread = None
        self.average_spread = None  # 基准价差（中位数）
        
        # 实际价差状态 - 复用现有架构
        self.actual_open_spread = None  # 实际开仓价差
        self.actual_close_spread = None  # 实际平仓价差
        self.primary_open_price = None
        self.lighter_open_price = None
        self.primary_close_price = None
        self.lighter_close_price = None
       
        # 采样过程中的双边预计成交价 
        self.primary_open_exec_price = None 
        self.lighter_open_exec_price = None
        self.primary_close_exec_price = None 
        self.lighter_close_exec_price = None
        self.logger = None
    
    async def can_open(self, hedge_bot):
        """检查是否可以基于价差开仓"""
        self.logger = hedge_bot.logger
        self.data['side'] = 'open'
        try:
            self.logger.info("🎯 计算基准价差和当前价差")
            
            # 1. 多次采样获得基准价差（预计价差）
            baseline_spread = await self.get_realistic_executable_spread(hedge_bot, is_closing=False, sample_count=self.current_spread_sample_count)
            self.average_spread = baseline_spread  # 更新基准价差
            
            # 2. 单次计算获得当前价差
            await asyncio.sleep(0.5)
            current_spread = await self.get_realistic_executable_spread(hedge_bot, is_closing=False, sample_count=1, use_max=False)
            self.current_spread = current_spread
            
            # 3. 价差判断
            # threshold_value = float(baseline_spread) * (1 + self.profit_threshold)
            
            # 检查价差条件
            # reason = f"[价差策略] 未触发, 当前价差: {current_spread:.6f}, 开仓价差基准: {threshold_value:.6f}, 盈利阈值: {self.profit_threshold:.1%}"
            reason = f"[价差策略] 未触发, 当前价差: {current_spread:.6f}, 开仓价差基准: {baseline_spread:.6f}"
            strategy_result = HedgeStrategyResult.REJECT
            
            if float(current_spread) > baseline_spread:
                self.open_spread = current_spread  # 记录预计开仓价差
                # reason = f"✅ 当前价差满足开仓条件：{current_spread:.6f} 大于 {threshold_value:.6f} (基准×{1+self.profit_threshold:.3f})"
                reason = f"✅ 当前价差满足开仓条件：{current_spread:.6f} 大于 {baseline_spread:.6f}"
                strategy_result = HedgeStrategyResult.PASS
                
            self._set_strategy_context(strategy_result, reason)
            
        except Exception as e:
            self._set_strategy_context(result=HedgeStrategyResult.PASS, reason=f"❌ 价差策略开仓检查失败: {e}")
    
    async def can_close(self, hedge_bot):
        """检查是否可以基于价差平仓 - 使用磨损优化逻辑"""
        self.logger = hedge_bot.logger
        self.data['side'] = 'close'
        try:
            # 获取当前真实执行价差
            # current_spread = await self.get_realistic_executable_spread(hedge_bot, is_closing=True, sample_count=1, use_max=False)
            # 使用采样区间的最小值
            current_spread = await self.get_realistic_executable_spread(hedge_bot, is_closing=True, sample_count=self.current_spread_sample_count, use_max=True)
            self.current_spread = current_spread
            self.close_spread = current_spread  # 记录预计平仓价差
            
            reason = f"[价差策略] 平仓检查: 当前平仓价差={current_spread:.6f}"
            strategy_result = HedgeStrategyResult.REJECT
            
            if self.actual_open_spread is not None:
                # 基于实际开仓价差判断：当前价差 < 开仓价差 * (1 - 盈利阈值 - 费用成本)
                primary_fee_rate = hedge_bot.primary_fee_rate() 
                fee_cost_factor = Decimal(str(float(primary_fee_rate) * 2))  # 双边交易费用
                target_threshold = self.actual_open_spread * (Decimal('1') - Decimal(str(self.profit_threshold)) - fee_cost_factor)
                
                if float(current_spread) < float(target_threshold):
                    reason = f"✅ 价差收敛平仓：当前价差{current_spread:.6f} 小于 目标阈值{target_threshold:.6f} " \
                            f"(基于实际开仓价差{self.actual_open_spread:.6f})"
                    strategy_result = HedgeStrategyResult.TRIGGER
                else:
                    reason = f"[价差策略] 未触发平仓: 当前价差{current_spread:.6f} 大于 目标阈值{target_threshold:.6f}"
            else:
                # 如果没有实际开仓价差，基于基准价差判断
                if self.average_spread is not None:
                    threshold = float(self.average_spread) * (1 - self.profit_threshold)
                    if float(current_spread) < threshold:
                        reason = f"✅ 基准价差平仓：当前价差{current_spread:.6f} 小于 基准阈值{threshold:.6f}"
                        strategy_result = HedgeStrategyResult.PASS
                    else:
                        reason = f"[价差策略] 未触发平仓: 当前价差{current_spread:.6f} 大于 基准阈值{threshold:.6f}"
                else:
                    reason = f"⚠️ 无可用价差基准，跳过价差策略平仓判断"
            
            self._set_strategy_context(strategy_result, reason)
            
        except Exception as e:
            self._set_strategy_context(result=HedgeStrategyResult.PASS, reason=f"❌ 价差策略平仓检查失败: {e}")
    
    async def get_realistic_executable_spread(self, hedge_bot, is_closing=False, sample_count=10, use_max=True):
        """计算真实可执行价差 - 自适应价差收敛与价格倒挂套利
        
        Args:
            hedge_bot: hedge bot实例
            is_closing: 是否为平仓计算
            sample_count: 采样次数，None时使用默认值
        """
        try:
            # 多次采样获取稳定的执行价差
            spreads = []
            trade_directions = []  # 记录每次采样的交易方向
            
            for i in range(sample_count):
                try:
                    # 获取Primary的BBO价格和交易数量
                    primary_bid, primary_ask = await hedge_bot.primary_client.fetch_bbo_prices(hedge_bot.primary_client.config.contract_id)
                    trade_quantity = hedge_bot.order_quantity
                    
                    # 🔍 关键逻辑：让市场数据决定最优交易方向
                    # 比较两个交易所的中间价，选择盈利更大的方向
                    primary_mid = (primary_bid + primary_ask) / Decimal('2')
                    lighter_mid = hedge_bot.lighter.get_lighter_mid_price()
                    
                    # 动态判断交易方向：总是选择能赚取价差的方向
                    if lighter_mid > primary_mid:
                        self.open_side = 'buy'
                        # 情况1：Lighter更贵 → 价差收敛策略（做空价差）
                        if not is_closing:  # 开仓：Primary买入，Lighter卖出
                            # Primary maker买单价格（参考EdgeX place_open_order逻辑）
                            primary_exec_price = primary_ask - hedge_bot.primary_tick_size
                            # Lighter taker卖单价格（基于订单簿深度）
                            lighter_exec_price = hedge_bot.lighter.calculate_execution_price('sell', trade_quantity)
                            trade_type = "价差收敛开仓"
                            self.primary_open_exec_price = primary_exec_price
                            self.lighter_open_exec_price = lighter_exec_price
                        else:  # 平仓：Primary卖出，Lighter买入
                            # Primary maker卖单价格
                            primary_exec_price = primary_bid + hedge_bot.primary_tick_size
                            # Lighter taker买单价格（基于订单簿深度）
                            lighter_exec_price = hedge_bot.lighter.calculate_execution_price('buy', trade_quantity)
                            trade_type = "价差收敛平仓"
                            self.primary_close_exec_price = primary_exec_price
                            self.lighter_close_exec_price = lighter_exec_price
                    else:
                        self.open_side = 'sell'
                        # 情况2：Primary更贵 → 传统套利策略（做多价差）
                        if not is_closing:  # 开仓：Primary卖出，Lighter买入
                            # Primary maker卖单价格
                            primary_exec_price = primary_bid + hedge_bot.primary_tick_size
                            # Lighter taker买单价格（基于订单簿深度）
                            lighter_exec_price = hedge_bot.lighter.calculate_execution_price('buy', trade_quantity)
                            trade_type = "价格倒挂开仓"
                            self.primary_open_exec_price = primary_exec_price
                            self.lighter_open_exec_price = lighter_exec_price
                        else:  # 平仓：Primary买入，Lighter卖出
                            # Primary maker买单价格
                            primary_exec_price = primary_ask - hedge_bot.primary_tick_size
                            # Lighter taker卖单价格（基于订单簿深度）
                            lighter_exec_price = hedge_bot.lighter.calculate_execution_price('sell', trade_quantity)
                            trade_type = "价格倒挂平仓"
                            self.primary_close_exec_price = primary_exec_price
                            self.lighter_close_exec_price = lighter_exec_price
                   

                    # 计算执行价差（总是正值，表示可获得的盈利）
                    raw_spread = abs(primary_exec_price - lighter_exec_price)
                    
                    # 计算成本（基于真实Primary执行价格）
                    primary_fee_rate = hedge_bot.primary_fee_rate()
                    fee_cost = primary_exec_price * primary_fee_rate
                    
                    # 净可执行价差
                    net_spread = raw_spread - fee_cost
                    spreads.append(net_spread)
                    trade_directions.append(trade_type)
                    
                    if self.logger:
                        price_relation = "Lighter>Primary" if lighter_mid > primary_mid else "Primary>Lighter"
                        self.logger.info(f"📊 采样 {i+1}/{sample_count} [{price_relation}]: "
                                       f"Primary(maker)={primary_exec_price:.6f}, "
                                       f"Lighter(taker)={lighter_exec_price:.6f}, "
                                       f"净价差={net_spread:.6f} ({trade_type}) "
                                       f"[数量:{trade_quantity}]")
                    
                    # 采样间隔
                    if i < sample_count - 1:
                        await asyncio.sleep(0.5)
                        
                except Exception as e:
                    self.logger.error(f"❌ 价差采样失败 {i+1}: {e}")
                    continue
            
            if not spreads:
                raise Exception("所有价差采样都失败了")
            
            # 取中位数作为稳定价差
            # stable_spread = statistics.median(spreads)
            # 取最大价差进行安全比较
            if use_max:
                stable_spread = max(spreads)
            else:
                stable_spread = min(spreads)
            
            # 统计交易方向分布
            direction_counts = {}
            for direction in trade_directions:
                direction_counts[direction] = direction_counts.get(direction, 0) + 1
            
            if self.logger:
                self.logger.info(f"✅ {'平仓' if is_closing else '开仓'}稳定执行价差: {stable_spread:.6f} (基于{len(spreads)}个样本)")
                self.logger.info(f"🎯 自适应策略分布: {direction_counts}")
            
            return stable_spread
            
        except Exception as e:
            self.logger.error(f"❌ 真实执行价差计算失败: {e}")

    def _get_msgs(self) -> List[str]:
        if self.data['side'] == 'open':
            base_msg = [
                f"📊 价差策略: 价差大于阈值开仓",
                f"[Primary] 预计开仓价: {self.primary_open_exec_price}, 实际开仓价: {self.primary_open_price}, 滑点: {abs(self.primary_open_exec_price - self.primary_open_price)}",
                f"[Lighter] 预计开仓价: {self.lighter_open_exec_price}, 实际开仓价: {self.lighter_open_price}, 滑点: {abs(self.lighter_open_exec_price - self.lighter_open_price)}",
            ]
            
            # 显示预计和实际开仓价差
            if self.open_spread is not None:
                base_msg.append(f"📉 预计开仓价差: {self.open_spread:.6f}")
            
            if self.actual_open_spread is not None:
                base_msg.append(f"💰 实际开仓价差: {self.actual_open_spread:.6f}")
                if self.open_spread is not None:
                    diff = abs(self.actual_open_spread - Decimal(str(self.open_spread)))
                    base_msg.append(f"📈 价差差异: {diff:.6f}")
            else:
                base_msg.append(f"💰 实际开仓价差: 待更新")
            
            # 添加基准信息
            base_msg.extend([
                f"📈 中位数价差: {self.average_spread:.6f}",
                f"🎯 盈利阈值: {self.profit_threshold:.1%}"
            ])
            
        else:  # close
            base_msg = [
                f"📊 价差策略: 价差收敛触发平仓",
                # f"[Primary] 预计开仓价: {self.primary_open_exec_price}, 实际开仓价: {self.primary_open_price}, 滑点: {abs(self.primary_open_exec_price - self.primary_open_price)}",
                # f"[Primary] 预计平仓价: {self.primary_close_exec_price}, 实际平仓价: {self.primary_close_price}, 滑点: {abs(self.primary_close_exec_price - self.primary_close_price)}",
                # f"[Lighter] 预计开仓价: {self.lighter_open_exec_price}, 实际开仓价: {self.lighter_open_price}, 滑点: {abs(self.lighter_open_exec_price - self.lighter_open_price)}",
                # f"[Lighter] 预计平仓价: {self.lighter_close_exec_price}, 实际平仓价: {self.lighter_close_price}, 滑点: {abs(self.lighter_close_exec_price - self.lighter_close_price)}",
            ]
            
            if self.primary_open_exec_price is None:
                base_msg.append(f"[Primary] 预计开仓价: {self.primary_open_exec_price}, 实际开仓价: {self.primary_open_price}, 滑点: -")
            else:
                base_msg.append(f"[Primary] 预计开仓价: {self.primary_open_exec_price}, 实际开仓价: {self.primary_open_price}, 滑点: {abs(self.primary_open_exec_price - self.primary_open_price)}")
            if self.primary_close_exec_price is None:
                base_msg.append(f"[Primary] 预计平仓价: {self.primary_close_exec_price}, 实际平仓价: {self.primary_close_price}, 滑点: -")
            else:
                base_msg.append(f"[Primary] 预计平仓价: {self.primary_close_exec_price}, 实际平仓价: {self.primary_close_price}, 滑点: {abs(self.primary_close_exec_price - self.primary_close_price)}")
            if self.lighter_open_exec_price is None:
                base_msg.append(f"[Lighter] 预计开仓价: {self.lighter_open_exec_price}, 实际开仓价: {self.lighter_open_price}, 滑点: -")
            else:
                base_msg.append(f"[Lighter] 预计开仓价: {self.lighter_open_exec_price}, 实际开仓价: {self.lighter_open_price}, 滑点: {abs(self.lighter_open_exec_price - self.lighter_open_price)}")
            if self.lighter_close_exec_price is None:
                base_msg.append(f"[Lighter] 预计平仓价: {self.lighter_close_exec_price}, 实际平仓价: {self.lighter_close_price}, 滑点: -")
            else:
                base_msg.append(f"[Lighter] 预计平仓价: {self.lighter_close_exec_price}, 实际平仓价: {self.lighter_close_price}, 滑点: {abs(self.lighter_close_exec_price - self.lighter_close_price)}")
            
            # 显示预计和实际平仓价差
            predict_spread_msg = ""
            if self.open_spread is not None:
                predict_spread_msg += f"📉 预计开仓价差: {self.open_spread:.6f}"
            if self.actual_open_spread is not None:
                predict_spread_msg += f" 💰 实际开仓价差: {self.actual_open_spread:.6f}"
            base_msg.append(predict_spread_msg)
                
            actual_spread_msg = ""
            if self.close_spread is not None:
                actual_spread_msg += f"📉 预计平仓价差: {self.close_spread:.6f}"
            if self.actual_close_spread is not None:
                actual_spread_msg += f" 💰 实际平仓价差: {self.actual_close_spread:.6f}"
            else:
                actual_spread_msg += " 💰 实际平仓价差: 待更新"
            base_msg.append(actual_spread_msg)
            
            spread_diff_msg = ""
            if self.actual_close_spread is not None and self.actual_open_spread is not None:
                spread_profit = abs(self.actual_open_spread - self.actual_close_spread)
                spread_predict = abs(self.open_spread - self.close_spread)
                spread_diff_msg += f"💸 预计开平仓价差: {spread_predict} 💰 实际开平仓价差: {spread_profit:.6f}"
            base_msg.append(spread_diff_msg)
            
            # 添加基准信息
            base_msg.extend([
                f"💰 盈利阈值: {self.profit_threshold:.1%}"
            ])
            
        return base_msg
    
    def after_open_hedge_position(self, hedge_bot):
        # 如果开仓策略不是价差，那么这里的值会是空的，can_close将会回退到传统价差判断的方式
        order_handler = hedge_bot.get_current_order_handler()
        self.primary_open_price = order_handler.current_primary_price
        self.lighter_open_price = order_handler.current_lighter_price
        # 修复: 确保两个价格都是Decimal类型再进行运算
        self.actual_open_spread = abs(Decimal(str(self.primary_open_price)) - Decimal(str(self.lighter_open_price)))
    
    def after_close_hedge_position(self, hedge_bot):
        order_handler = hedge_bot.get_current_order_handler()
        self.primary_close_price = order_handler.current_primary_price
        self.lighter_close_price = order_handler.current_lighter_price
        # 修复: 使用平仓价格计算平仓价差，并确保类型一致
        self.actual_close_spread = abs(Decimal(str(self.primary_close_price)) - Decimal(str(self.lighter_close_price)))

    def order_place_timeout_retry(self):
        return True
    