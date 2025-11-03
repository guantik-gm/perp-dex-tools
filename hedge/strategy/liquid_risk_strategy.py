import os
import asyncio
from decimal import Decimal
from typing import List

from hedge.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult


class LiquidRiskStrategy(HedgeStrategy):
    """流动性风险策略 - 最高优先级，专注于风险控制"""
    
    def __init__(self, priority=100):
        super().__init__(open_priority=priority, close_priority=priority)
        self.risk_threshold = 0.2
        self.logger = None
        self.risk_exchange = None
        self.risk_liquidation_price = None
        
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
        return [
                    f"🏦 风险交易所: {self.risk_exchange}",
                    f"💰 清算价格: {self.risk_liquidation_price:.6f}",
                    f"📊 风险阈值: {self.risk_threshold:.1%}",
                    f"📊 当前价格: {self.data['price_data'].get('primary_mid', 0):.6f}",
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
                self.logger.warning(f"⚠️ 获取Primary清算价格失败: {primary_liquidation}")
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
                if self._check_single_exchange_risk("Primary", current_primary_mid, primary_liquidation):
                    return True
            
            # 检查Lighter风险
            if lighter_liquidation is not None:
                if self._check_single_exchange_risk("Lighter", current_lighter_mid, lighter_liquidation):
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"❌ 风险控制检查失败: {e}")
            return False
    
    def _check_single_exchange_risk(self, exchange_name, current_price, liquidation_price):
        """检查单个交易所的清算风险"""
        if liquidation_price is None or liquidation_price <= 0:
            return False
        current_price = Decimal(current_price)
        liquidation_price = Decimal(liquidation_price) 
        # 计算当前价格与清算价格的距离比例
        price_distance_ratio = abs(current_price - liquidation_price) / liquidation_price
        
        if price_distance_ratio <= self.risk_threshold:
            self.logger.warning(
                f"🚨 {exchange_name}清算风险警告: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, "
                f"距离比例{price_distance_ratio:.2%} &lt;= {self.risk_threshold:.2%}"
            )
            self.risk_exchange = exchange_name
            self.risk_liquidation_price = liquidation_price
            return True
        else:
            self.logger.debug(
                f"✅ {exchange_name}清算风险正常: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, "
                f"距离比例{price_distance_ratio:.2%}"
            )
            return False
