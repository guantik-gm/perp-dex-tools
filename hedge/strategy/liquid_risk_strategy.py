import os
import asyncio
from decimal import Decimal
from typing import List

from hedge.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult


class LiquidRiskStrategy(HedgeStrategy):
    """流动性风险策略 - 最高优先级，专注于风险控制"""
    
    def __init__(self, priority=100):
        super().__init__(open_priority=priority, close_priority=priority)
        self.risk_threshold = 0.02  # 当前价格到清算价格距离不能小于2%, 再涨/跌2%到达清算线
        self.logger = None
        self.risk_exchange = None
        self.risk_liquidation_price = None
        
        # 风险距离信息
        self.primary_risk_distance = None
        self.lighter_risk_distance = None
        
        self.primary_liquidation_price = None
        self.lighter_liquidation_price = None
        self.primary_position_side = None
        self.lighter_position_side = None
        
    async def can_open(self, hedge_bot):
        self.logger = hedge_bot.logger
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
        
        # 安全地获取 price_data
        price_data = getattr(self, 'data', {}).get('price_data', {})
        primary_price = price_data.get('primary_mid', 0) if price_data else 0
        lighter_price = price_data.get('lighter_mid', 0) if price_data else 0
        primary_price_str = f"{primary_price:.6f}" if primary_price else "0.000000"
        lighter_price_str = f"{lighter_price:.6f}" if lighter_price else "0.000000"
        
        # 获取风险距离信息
        primary_risk_str = f"{self.primary_risk_distance:.2%}" if self.primary_risk_distance is not None else "无"
        lighter_risk_str = f"{self.lighter_risk_distance:.2%}" if self.lighter_risk_distance is not None else "无"
        
        primary_liquidation_str = f"{self.primary_liquidation_price:.6f}" if self.primary_liquidation_price is not None else "无"
        lighter_liquidation_str = f"{self.lighter_liquidation_price:.6f}" if self.lighter_liquidation_price is not None else "无"
        primary_side_str = self.primary_position_side if self.primary_position_side is not None else "无"
        lighter_side_str = self.lighter_position_side if self.lighter_position_side is not None else "无"
        
        return [
            f"📊 当前价格 - Primary: {primary_price_str} | Lighter: {lighter_price_str}",
            f"💰 清算价格 - Primary: {primary_liquidation_str} ({primary_side_str}) | Lighter: {lighter_liquidation_str} ({lighter_side_str})",
            f"⚠️ 风险距离 - Primary: {primary_risk_str} | Lighter: {lighter_risk_str}",
            f"🚨 风险阈值: {self.risk_threshold:.1%} | 触发交易所: {risk_exchange_str}",
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
            
            # 记录清算价格到实例属性
            self.primary_liquidation_price = primary_liquidation
            self.lighter_liquidation_price = lighter_liquidation
            
            # 获取并记录持仓方向
            primary_position = hedge_bot.get_primary_position()
            lighter_position = hedge_bot.get_lighter_position()
            
            # 根据持仓数量判断持仓方向
            if primary_position > 0:
                self.primary_position_side = "long"
            elif primary_position < 0:
                self.primary_position_side = "short"
            else:
                self.primary_position_side = "无持仓"
                
            if lighter_position > 0:
                self.lighter_position_side = "long"
            elif lighter_position < 0:
                self.lighter_position_side = "short"
            else:
                self.lighter_position_side = "无持仓"
                
            self.logger.info(f"{hedge_bot.primary_exchange_name()} 清算价: {self.primary_liquidation_price}({self.primary_position_side}), Lighter 清算价: {self.lighter_liquidation_price}({self.lighter_position_side})")
            
            # 如果都获取失败，则跳过检查
            if primary_liquidation is None and lighter_liquidation is None:
                return False
            
            # 当前价格
            current_primary_mid = current_sample.get('primary_mid', 0)
            current_lighter_mid = current_sample.get('lighter_mid', 0)
            
            # 检查Primary风险
            if primary_liquidation is not None:
                if self._check_single_exchange_risk(hedge_bot.primary_exchange_name(), current_primary_mid, primary_liquidation):
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
        
        # 检查当前价格有效性，防止除零错误
        if current_price is None or current_price <= 0:
            self.logger.warning(f"⚠️ {exchange_name}当前价格无效: {current_price}")
            return False
        
        current_price = Decimal(str(current_price))
        liquidation_price = Decimal(str(liquidation_price))
        
        # 计算当前价格到清算价格的距离百分比
        risk_distance = abs(current_price - liquidation_price) / current_price
        
        # 保存风险距离信息
        if exchange_name == "Lighter":
            self.lighter_risk_distance = risk_distance
        else:
            # Primary 或其他主交易所
            self.primary_risk_distance = risk_distance
        
        # 风险判断：当前价格到清算价格的距离小于阈值则触发
        if risk_distance <= Decimal(self.risk_threshold):
            self.logger.warning(
                f"🚨 {exchange_name}清算风险警告: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, "
                f"风险距离{risk_distance:.2%}, 触发阈值{self.risk_threshold:.2%}"
            )
            self.risk_exchange = exchange_name
            self.risk_liquidation_price = liquidation_price
            return True
        else:
            self.logger.debug(
                f"✅ {exchange_name}清算风险正常: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, "
                f"风险距离{risk_distance:.2%}"
            )
            return False

    def after_open_hedge_position(self, hedge_bot):
        """完整的对冲仓位开仓后触发，可以拿到开仓后的价格信息"""
        try:
            self.logger = hedge_bot.logger
            
            # 安全的异步调用：检查事件循环状态
            try:
                # 尝试获取当前运行的事件循环
                loop = asyncio.get_running_loop()
                # 如果成功，说明已在事件循环中，创建异步任务
                loop.create_task(self._update_risk_info_async(hedge_bot))
                self.logger.info("✅ 开仓后风险信息更新任务已创建（异步执行）")
                # 注意：这里不等待任务完成，避免阻塞事件循环
                # 如果需要获取结果，可以在后续的 can_close() 调用中获取
            except RuntimeError:
                # 没有运行的事件循环，安全使用 asyncio.run
                asyncio.run(self._update_risk_info_async(hedge_bot))
                self.logger.info("✅ 开仓后风险信息更新完成（同步执行）")
            
        except Exception as e:
            if self.logger:
                self.logger.error(f"❌ 开仓后风险信息更新失败: {e}")
    
    async def _update_risk_info_async(self, hedge_bot):
        """异步更新开仓后的风险信息"""
        try:
            # 获取当前价格数据
            current_sample = await self._get_current_price_data(hedge_bot)
            self.data['price_data'] = current_sample
            
            # 检查清算风险（不触发平仓，只更新风险信息）
            await self._check_liquidation_risk(hedge_bot, current_sample)
            
            self.logger.info("✅ 开仓后清算风险信息已更新")
            
        except Exception as e:
            self.logger.error(f"❌ 异步更新风险信息失败: {e}")