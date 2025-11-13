import asyncio
from decimal import Decimal
from typing import List, Optional, Dict

from hedge_v1.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult


class SpreadV1Strategy(HedgeStrategy):
    """基于双边投入本金PnL收益率的平仓策略
    
    核心思路：
    1. 利用position_data.current_capital获取投入本金
    2. 使用模拟执行逻辑获取预计双边平仓价
    3. 计算预计总PnL和基于本金的收益率
    4. 当收益率超过阈值时触发平仓
    """
    
    def __init__(self, priority=10):
        super().__init__(open_priority=priority, close_priority=priority)
        self.profit_rate_threshold = 0.001  # 收益率阈值 (0.1%)
        self.logger = None
        
        # 存储计算结果用于日志展示
        self.projected_pnl_data: Optional[Dict] = None
    
    async def can_open(self, hedge_bot):
        """SpreadV1策略只负责平仓判断，不参与开仓"""
        self._set_strategy_context(HedgeStrategyResult.REJECT, "[SpreadV1策略] 不负责开仓判断")
    
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
                    "[SpreadV1策略] PnL计算失败，数据不完整"
                )
                return
            
            profit_rate = pnl_result['profit_rate']
            total_pnl = pnl_result['total_pnl']
            capital = pnl_result['capital']
            
            # 判断收益率是否超过阈值
            if profit_rate >= self.profit_rate_threshold:
                reason = f"✅ SpreadV1平仓: 收益率{profit_rate:.4%} >= 阈值{self.profit_rate_threshold:.4%}, " \
                        f"PnL={total_pnl:.6f}, 本金={capital:.6f}"
                self._set_strategy_context(HedgeStrategyResult.TRIGGER, reason)
                self.projected_pnl_data = pnl_result  # 保存数据用于日志
            else:
                reason = f"[SpreadV1策略] 收益率{profit_rate:.4%} < 阈值{self.profit_rate_threshold:.4%}, " \
                        f"PnL={total_pnl:.6f}"
                self._set_strategy_context(HedgeStrategyResult.REJECT, reason)
                
        except Exception as e:
            self.logger.error(f"❌ SpreadV1策略检查异常: {e}")
            self._set_strategy_context(
                HedgeStrategyResult.PASS, 
                f"❌ SpreadV1策略检查失败: {e}"
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
            
            # 计算平仓费用
            close_fee = self._calculate_close_fee(
                hedge_bot, 
                primary_close_price, 
                position_data.current_primary_open_quantity
            )
            
            # 净PnL = 双边PnL - 费用
            total_pnl = primary_pnl + lighter_pnl - close_fee
            
            # 收益率 = 净PnL / 投入本金
            capital = position_data.current_capital
            profit_rate = total_pnl / capital
            
            self.logger.info(f"💰 SpreadV1预计: Primary PnL={primary_pnl:.6f}, Lighter PnL={lighter_pnl:.6f}, "
                           f"费用={close_fee:.6f}, 净PnL={total_pnl:.6f}, "
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
            lighter_close_side = 'sell' if position_data.current_lighter_open_side == 'buy' else 'buy'
            lighter_close_price = hedge_bot.lighter.calculate_execution_price(
                lighter_close_side,
                position_data.current_lighter_open_quantity
            )
            
            self.logger.info(f"📊 预计平仓价: Primary({position_data.current_primary_open_side}→平仓)={primary_close_price:.6f}, "
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
        if hasattr(self, 'projected_pnl_data') and self.projected_pnl_data:
            data = self.projected_pnl_data
            return [
                "💰 SpreadV1策略: 基于投入本金收益率平仓",
                f"[Primary] 预计PnL: {data['primary_pnl']:.6f}",
                f"[Lighter] 预计PnL: {data['lighter_pnl']:.6f}",
                f"[费用] 平仓费用: {data['close_fee']:.6f}",
                f"[总计] 净PnL: {data['total_pnl']:.6f}",
                f"[本金] 投入本金: {data['capital']:.6f}",
                f"[收益率] {data['profit_rate']:.4%} (阈值: {self.profit_rate_threshold:.4%})"
            ]
        return [f"SpreadV1策略: {self.data.get('reason', '')}"]
    
    def order_place_timeout_retry(self):
        """支持订单重试"""
        return True