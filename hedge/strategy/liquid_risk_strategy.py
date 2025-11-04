import os
import asyncio
from decimal import Decimal
from typing import List

from hedge.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult


class LiquidRiskStrategy(HedgeStrategy):
    """流动性风险策略 - 最高优先级，专注于风险控制"""
    
    def __init__(self, priority=100):
        super().__init__(open_priority=priority, close_priority=priority)
        self.risk_threshold = 0.8  # 当前价格到清算价格距离开仓价格到清算记录的80%
        self.logger = None
        self.risk_exchange = None
        self.risk_liquidation_price = None
        self.open_price = None
        self.position_side = None  # 'long' 或 'short'
        
        # 缓冲信息属性
        self.current_risk_buffer = None
        self.initial_risk_buffer = None
        self.buffer_consumed_ratio = None
        
    async def can_open(self, hedge_bot):
        """风险策略不主动触发开仓，只做被动检查"""
        self._set_strategy_context(result=HedgeStrategyResult.PASS, reason="开仓无清算风险")
        
    async def can_close(self, hedge_bot):
        """检查清算风险，如有风险则立即平仓"""
        self.logger = hedge_bot.logger
        
        try:
            # 获取当前价格数据
            current_sample = await self._get_current_price_data(hedge_bot)
            self.data['price_data'] = current_sample
            
            # 检查清算风险
            risk_triggered = await self._check_liquidation_risk(hedge_bot, current_sample)
            reason = "[清算价格策略] 未触发"
            result = HedgeStrategyResult.PASS
            if risk_triggered:
                reason = "🚨 风险控制触发：价格接近清算线，立即双边平仓"
                result = HedgeStrategyResult.TRIGGER
            self._set_strategy_context(result=result, reason=reason)
        except Exception as e:
            self._set_strategy_context(result=HedgeStrategyResult.PASS, reason="❌ 风险检查失败: {e}")
    
    def _get_msgs(self) -> List[str]:
        # 安全地获取数据，避免 None 值格式化错误
        risk_exchange_str = self.risk_exchange if self.risk_exchange is not None else "无"
        risk_liquidation_price_str = f"{self.risk_liquidation_price:.6f}" if self.risk_liquidation_price is not None else "无"
        open_price_str = f"{self.open_price:.6f}" if self.open_price is not None else "无"
        position_side_str = self.position_side if self.position_side is not None else "无"
        
        # 安全地获取 price_data
        price_data = getattr(self, 'data', {}).get('price_data', {})
        current_price = price_data.get('primary_mid', 0) if price_data else 0
        current_price_str = f"{current_price:.6f}" if current_price else "0.000000"
        
        # 获取缓冲信息
        current_buffer_str = f"{self.current_risk_buffer:.2%}" if self.current_risk_buffer is not None else "无"
        initial_buffer_str = f"{self.initial_risk_buffer:.2%}" if self.initial_risk_buffer is not None else "无"
        buffer_consumed_str = f"{self.buffer_consumed_ratio:.2%}" if self.buffer_consumed_ratio is not None else "无"
        
        return [
            f"🏦 风险交易所: {risk_exchange_str}",
            f"📊 持仓方向: {position_side_str}",
            f"📈 开仓价格: {open_price_str}",
            f"📊 当前价格: {current_price_str}",
            f"💰 清算价格: {risk_liquidation_price_str}", 
            f"🔵 当前风险缓冲: {current_buffer_str}",
            f"🟢 初始风险缓冲: {initial_buffer_str}",
            f"🔴 缓冲消耗比例: {buffer_consumed_str}",
            f"📊 风险阈值: {self.risk_threshold:.1%}",
        ]
    
    async def _check_liquidation_risk(self, hedge_bot, current_sample):
        """检查清算风险"""
        try:
            # 并行获取双边清算价格
            liquidation_results = await asyncio.gather(
                hedge_bot.primary_client.get_ticker_position_liquidation_price(),
                hedge_bot.lighter.get_ticker_position_liquidation_price(),
                return_exceptions=True
            )
            
            primary_liquidation = liquidation_results[0]
            lighter_liquidation = liquidation_results[1]
            
            # 检查获取失败的情况
            if isinstance(primary_liquidation, Exception):
                self.logger.warning(f"⚠️ 获取{hedge_bot.primary_exchange_name()}清算价格失败: {primary_liquidation}")
                primary_liquidation = None
            
            if isinstance(lighter_liquidation, Exception):
                self.logger.warning(f"⚠️ 获取Lighter清算价格失败: {lighter_liquidation}")
                lighter_liquidation = None
            
            # 如果都获取失败，则跳过检查
            if primary_liquidation is None and lighter_liquidation is None:
                return False
            
            # 当前价格
            current_primary_mid = current_sample.get('primary_mid', 0)
            current_lighter_mid = current_sample.get('lighter_mid', 0)
            
            # 检查Primary风险
            if primary_liquidation is not None:
                if self._check_single_exchange_risk(hedge_bot.primary_exchange_name(), current_primary_mid, primary_liquidation, hedge_bot):
                    return True
            
            # 检查Lighter风险
            if lighter_liquidation is not None:
                if self._check_single_exchange_risk("Lighter", current_lighter_mid, lighter_liquidation, hedge_bot):
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"❌ 风险控制检查失败: {e}")
            return False
    
    def _check_single_exchange_risk(self, exchange_name, current_price, liquidation_price, hedge_bot):
        """检查单个交易所的清算风险"""
        if liquidation_price is None or liquidation_price <= 0:
            return False
        current_price = Decimal(str(current_price))
        liquidation_price = Decimal(str(liquidation_price))
        
        # 获取开仓价格和持仓方向
        order_handler = hedge_bot.get_current_order_handler()
        
        # 如果有当前开仓价格，使用它；否则尝试获取历史开仓价格
        if order_handler.current_primary_price is not None:
            self.open_price = order_handler.current_primary_price
            # 根据当前订单方向判断持仓方向
            if order_handler.current_primary_side == 'buy':
                self.position_side = 'long'
            elif order_handler.current_primary_side == 'sell':
                self.position_side = 'short'
        
        open_price = Decimal(str(self.open_price))
        
        # 计算当前还能跌/涨多少%到清算 (a)
        current_risk_buffer = abs(current_price - liquidation_price) / current_price
        
        # 计算开仓时总共能跌/涨多少%到清算 (b) 
        initial_risk_buffer = abs(open_price - liquidation_price) / open_price
        
        # 计算风险缓冲消耗比例
        buffer_consumed_ratio = 1 - (current_risk_buffer / initial_risk_buffer) if initial_risk_buffer > 0 else 1
        
        # 保存到对象属性中
        self.current_risk_buffer = current_risk_buffer
        self.initial_risk_buffer = initial_risk_buffer
        self.buffer_consumed_ratio = buffer_consumed_ratio
        
        # 触发条件：当前风险缓冲 <= 初始风险缓冲 * risk_threshold
        risk_threshold_value = initial_risk_buffer * Decimal(self.risk_threshold)
        
        if current_risk_buffer <= risk_threshold_value:
            
            self.logger.warning(
                f"🚨 {exchange_name}清算风险警告: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, 开仓价格{open_price:.6f}, "
                f"当前风险缓冲{current_risk_buffer:.2%}, 初始风险缓冲{initial_risk_buffer:.2%}, "
                f"风险缓冲已消耗{buffer_consumed_ratio:.2%}, 触发阈值{self.risk_threshold:.2%}"
            )
            self.risk_exchange = exchange_name
            self.risk_liquidation_price = liquidation_price
            return True
        else:
            self.logger.debug(
                f"✅ {exchange_name}清算风险正常: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, 开仓价格{open_price:.6f}, "
                f"当前风险缓冲{current_risk_buffer:.2%}, 初始风险缓冲{initial_risk_buffer:.2%}"
            )
            return False
    