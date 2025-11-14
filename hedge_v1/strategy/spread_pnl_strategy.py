import asyncio
from decimal import Decimal
from typing import List, Optional, Dict

from hedge_v1.strategy.hedge_strategy import HedgeStrategyResult
from hedge_v1.strategy.spread_strategy import SpreadStrategy


class SpreadPnlStrategy(SpreadStrategy):
    """基于双边投入本金PnL收益率的平仓策略
    
    核心思路：
    1. 利用position_data.current_capital获取投入本金
    2. 使用模拟执行逻辑获取预计双边平仓价
    3. 计算预计总PnL和基于本金的收益率
    4. 当收益率超过阈值时触发平仓
    """
    
    def __init__(self, priority=10):
        super().__init__(priority=priority)
        self.profit_rate_threshold = -0.0018  # 收益率阈值 (0.1%), 20x杠杆下相当于 0.000125 磨损
        self.logger = None
        
        # 存储计算结果用于日志展示
        self.projected_pnl_data: Optional[Dict] = None
    
    async def can_close(self, hedge_bot):
        """基于双边投入本金收益率判断是否平仓"""
        self.logger = hedge_bot.logger
        self.data['side'] = 'close'
        
        try:
            # 计算预计总PnL和收益率
            pnl_result = await self.calculate_projected_total_pnl(hedge_bot)
            
            if not pnl_result:
                self._set_strategy_context(
                    HedgeStrategyResult.REJECT, 
                    "[SpreadPnl策略] PnL计算失败，数据不完整"
                )
                return
            
            profit_rate = pnl_result['profit_rate']
            total_pnl = pnl_result['total_pnl']
            capital = pnl_result['capital']
            
            # 判断收益率是否超过阈值
            if profit_rate >= self.profit_rate_threshold:
                reason = f"✅ SpreadPnl平仓: 收益率{profit_rate:.4%} >= 阈值{self.profit_rate_threshold:.4%}, " \
                        f"PnL={total_pnl:.6f}, 本金={capital:.6f}"
                self._set_strategy_context(HedgeStrategyResult.TRIGGER, reason)
                self.projected_pnl_data = pnl_result  # 保存数据用于日志
            else:
                reason = f"[SpreadPnl策略] 收益率{profit_rate:.4%} < 阈值{self.profit_rate_threshold:.4%}, " \
                        f"PnL={total_pnl:.6f}"
                self._set_strategy_context(HedgeStrategyResult.REJECT, reason)
                
        except Exception as e:
            self.logger.error(f"❌ SpreadPnl策略检查异常: {e}")
            self._set_strategy_context(
                HedgeStrategyResult.PASS, 
                f"❌ SpreadPnl策略检查失败: {e}"
            )
    
    async def calculate_projected_total_pnl(self, hedge_bot) -> Optional[Dict]:
        """计算预计总体PnL和收益率"""
        try:
            position_data = hedge_bot.position_data
            
            # 验证数据完整性
            if not self._validate_position_data(position_data):
                self.logger.warning("⚠️ 仓位数据不完整，无法计算PnL")
                return None
            
            if not position_data.current_capital or position_data.current_capital <= 0:
                self.logger.warning("⚠️ 投入本金数据无效")
                return None
            
            # 获取预计双边平仓价
            primary_close_price, lighter_close_price = await self._get_projected_close_prices(hedge_bot)
            
            if not primary_close_price or not lighter_close_price:
                self.logger.warning("⚠️ 无法获取预计平仓价格")
                return None
            
            # 计算双边PnL
            primary_pnl = self._calculate_side_pnl(
                position_data.current_primary_open_price,
                primary_close_price,
                position_data.current_primary_open_side,
                position_data.current_primary_open_quantity
            )
            
            lighter_pnl = self._calculate_side_pnl(
                position_data.current_lighter_open_price,
                lighter_close_price,
                position_data.current_lighter_open_side,
                position_data.current_lighter_open_quantity
            )
            
            # 计算开平仓费用
            close_fee = self._calculate_close_fee(
                hedge_bot, 
                primary_close_price, 
                position_data.current_primary_open_quantity
            ) * 2
            
            # 净PnL = 双边PnL - 费用
            total_pnl = primary_pnl + lighter_pnl - close_fee
            
            # 收益率 = 净PnL / 投入本金
            capital = position_data.current_capital
            profit_rate = total_pnl / capital
            
            self.logger.info(f"💰 SpreadPnl预计: Primary PnL={primary_pnl:.6f}, Lighter PnL={lighter_pnl:.6f}, "
                           f"手续费={close_fee:.6f}, 净PnL={total_pnl:.6f}, "
                           f"本金={capital:.6f}, 收益率={profit_rate:.4%}")
            
            return {
                'total_pnl': total_pnl,
                'primary_pnl': primary_pnl,
                'lighter_pnl': lighter_pnl,
                'close_fee': close_fee,
                'capital': capital,
                'profit_rate': profit_rate,
                'primary_close_price': primary_close_price,
                'lighter_close_price': lighter_close_price
            }
            
        except Exception as e:
            self.logger.error(f"❌ PnL计算失败: {e}")
            return None
    
    async def _get_projected_close_prices(self, hedge_bot):
        """获取预计双边平仓价格 - 参考spread_strategy.py的模拟执行逻辑"""
        try:
            position_data = hedge_bot.position_data
            
            # 获取Primary BBO价格
            primary_bid, primary_ask = await hedge_bot.primary_client.fetch_bbo_prices(
                hedge_bot.primary_client.config.contract_id
            )
            
            # Primary平仓价格计算（参考spread_strategy第162-188行）
            # 平仓方向与开仓方向相反
            if position_data.current_primary_open_side == 'buy':
                # 买入开仓 -> 卖出平仓，使用maker卖单价格
                primary_close_price = primary_bid + hedge_bot.primary_tick_size
            else:
                # 卖出开仓 -> 买入平仓，使用maker买单价格  
                primary_close_price = primary_ask - hedge_bot.primary_tick_size
            
            # Lighter平仓价格计算（使用taker执行价格）
            primary_close_side = 'sell' if position_data.current_primary_open_side == 'buy' else 'buy'
            lighter_close_side = 'sell' if position_data.current_lighter_open_side == 'buy' else 'buy'
            lighter_close_price = hedge_bot.lighter.calculate_execution_price(
                lighter_close_side,
                position_data.current_lighter_open_quantity
            )
            
            self.logger.info(f"📊 预计平仓价: Primary({position_data.current_primary_open_side}→{primary_close_side})={primary_close_price:.6f}, "
                           f"Lighter({position_data.current_lighter_open_side}→{lighter_close_side})={lighter_close_price:.6f}")
            
            return primary_close_price, lighter_close_price
            
        except Exception as e:
            self.logger.error(f"❌ 获取预计平仓价格失败: {e}")
            return None, None
    
    def _calculate_side_pnl(self, open_price, close_price, open_side, quantity):
        """计算单边PnL"""
        if not all([open_price, close_price, open_side, quantity]):
            return Decimal('0')
            
        open_price = Decimal(str(open_price))
        close_price = Decimal(str(close_price))
        quantity = Decimal(str(quantity))
        
        if open_side == 'buy':
            # 买入开仓，平仓价高于开仓价为盈利
            return (close_price - open_price) * quantity
        else:
            # 卖出开仓，开仓价高于平仓价为盈利
            return (open_price - close_price) * quantity
    
    def _calculate_close_fee(self, hedge_bot, close_price, quantity):
        """计算平仓费用（仅计算Primary费用）"""
        try:
            fee_rate = Decimal(str(hedge_bot.primary_fee_rate()))
            close_price = Decimal(str(close_price))
            quantity = Decimal(str(quantity))
            return close_price * quantity * fee_rate
        except Exception:
            return Decimal('0')
    
    def _validate_position_data(self, position_data):
        """验证仓位数据完整性"""
        required_fields = [
            position_data.current_primary_open_price,
            position_data.current_primary_open_side,
            position_data.current_primary_open_quantity,
            position_data.current_lighter_open_price,
            position_data.current_lighter_open_side,
            position_data.current_lighter_open_quantity
        ]
        return all(field is not None for field in required_fields)
    
    def _get_msgs(self) -> List[str]:
        """生成策略执行信息"""
        # 需要通过hedge_bot获取position_data - 这里暂时使用占位逻辑
        # 实际使用时需要传入hedge_bot或position_data的引用
        base_msg = []
        
        if self.data['side'] == 'open':
            # self.primary_open_price在父类中设置
            # primary_open_slippage = abs(primary_open_exec_price - self.primary_open_price)
            # primary_open_slippage_rate = (primary_open_slippage / primary_open_exec_price * 100) if primary_open_slippage else 0
            # primary_msg = f"[Primary] 预计开仓价: {primary_open_exec_price}, 实际开仓价: {self.primary_open_price}, 滑点: {primary_open_slippage}, 滑点率: {primary_open_slippage_rate:.6f}%"
            
            # # Lighter开仓滑点
            # lighter_open_slippage = abs(lighter_open_exec_price - self.lighter_open_price)
            # lighter_open_slippage_rate = (lighter_open_slippage / lighter_open_exec_price * 100) if lighter_open_slippage else 0
            # lighter_msg = f"[Lighter] 预计开仓价: {lighter_open_exec_price}, 实际开仓价: {self.lighter_open_price}, 滑点: {lighter_open_slippage}, 滑点率: {lighter_open_slippage_rate:.6f}%"
            
            # base_msg = [
            #     "💰 SpreadPnl策略: 基于投入本金收益率开仓",
            #     primary_msg,
            #     lighter_msg,
            #     f"📊 投入本金: {data.get('capital', 0):.6f}",
            #     f"🎯 收益率阈值: {self.profit_rate_threshold:.4%}"
            # ]
            pass
        else:  # close - 使用position_data中的完整数据
            data = self.projected_pnl_data
            
            base_msg = ["💰 SpreadPnl策略: 基于投入本金收益率平仓"]
            
            primary_open_exec_price = getattr(self, 'primary_open_exec_price', None)
            primary_open_price = self.primary_open_price
            primary_open_slippage = abs(primary_open_exec_price - primary_open_price)
            primary_open_slippage_rate = (primary_open_slippage / primary_open_exec_price * 100) if primary_open_slippage else 0
            base_msg.append(f"[Primary] 预计开仓价: {primary_open_exec_price}, 实际开仓价: {primary_open_price}, 滑点: {primary_open_slippage}, 滑点率: {primary_open_slippage_rate:.6f}%")
            
            # Primary平仓信息
            primary_close_exec_price = getattr(self, 'primary_close_exec_price', None)
            primary_close_price = self.primary_close_price
            primary_close_slippage = abs(primary_close_exec_price - primary_close_price)
            primary_close_slippage_rate = (primary_close_slippage / primary_close_exec_price * 100) if primary_close_slippage else 0
            base_msg.append(f"[Primary] 预计平仓价: {primary_close_exec_price}, 实际平仓价: {primary_close_price}, 滑点: {primary_close_slippage}, 滑点率: {primary_close_slippage_rate:.6f}%")
            
            # Lighter开仓信息（从position_data获取）
            lighter_open_exec_price = getattr(self, 'lighter_open_exec_price', None)
            lighter_open_price = self.lighter_open_price
            lighter_open_slippage = abs(lighter_open_exec_price - lighter_open_price)
            lighter_open_slippage_rate = (lighter_open_slippage / lighter_open_exec_price * 100) if lighter_open_slippage else 0
            base_msg.append(f"[Lighter] 预计开仓价: {lighter_open_exec_price}, 实际开仓价: {lighter_open_price}, 滑点: {lighter_open_slippage}, 滑点率: {lighter_open_slippage_rate:.6f}%")
            
            # Lighter平仓信息
            lighter_close_exec_price = getattr(self, 'lighter_close_exec_price', None)
            lighter_close_price = self.lighter_close_price
            lighter_close_slippage = abs(lighter_close_exec_price - lighter_close_price)
            lighter_close_slippage_rate = (lighter_close_slippage / lighter_close_exec_price * 100) if lighter_close_slippage else 0
            base_msg.append(f"[Lighter] 预计平仓价: {lighter_close_exec_price}, 实际平仓价: {lighter_close_price}, 滑点: {lighter_close_slippage}, 滑点率: {lighter_close_slippage_rate:.6f}%")
            
            # 使用position_data中的实际成交价重新计算PnL
            actual_primary_pnl = self._calculate_side_pnl(
                self.primary_open_price,
                self.primary_close_price,
                self.position_data.current_primary_open_side,
                self.position_data.current_primary_open_quantity
            )
            
            actual_lighter_pnl = self._calculate_side_pnl(
                self.lighter_open_price,
                self.lighter_close_price,
                self.position_data.current_lighter_open_side,
                self.position_data.current_lighter_open_quantity
            )
            
            actual_close_fee = data['close_fee']
            
            predicted_primary_net = data['primary_pnl'] - data['close_fee']
            actual_primary_net = actual_primary_pnl - actual_close_fee
            actual_total_pnl = actual_primary_pnl + actual_lighter_pnl - actual_close_fee
            
            # Primary PnL比较
            base_msg.append(f"[Primary] 预计PnL: {predicted_primary_net:.6f}({data['primary_pnl']:.6f}-{data['close_fee']:.6f}), 实际PnL: {actual_primary_net:.6f}({actual_primary_pnl:.6f}-{actual_close_fee:.6f})")
            
            # Lighter PnL比较
            lighter_diff_pct = ((actual_lighter_pnl - data['lighter_pnl']) / data['lighter_pnl'] * 100) if data['lighter_pnl'] != 0 else 0
            base_msg.append(f"[Lighter] 预计PnL: {data['lighter_pnl']:.6f}, 实际PnL: {actual_lighter_pnl:.6f}, 差异: {lighter_diff_pct:.4f}%")
            
            # 总计PnL比较
            total_diff_pct = ((actual_total_pnl - data['total_pnl']) / data['total_pnl'] * 100) if data['total_pnl'] != 0 else 0
            base_msg.append(f"[总计] 预计净PnL: {data['total_pnl']:.6f}, 实际净PnL: {actual_total_pnl:.6f}, 差异: {total_diff_pct:.4f}%")
            
            # 收益率比较
            actual_profit_rate = actual_total_pnl / data['capital'] if data['capital'] > 0 else 0
            base_msg.append(f"[收益率] 预计: {data['profit_rate']:.4%}, 实际: {actual_profit_rate:.4%} (阈值: {self.profit_rate_threshold:.4%})")
        
        return base_msg
    
    def order_place_timeout_retry(self):
        """支持订单重试"""
        return True