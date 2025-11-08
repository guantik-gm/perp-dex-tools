import os
import time
import random
import asyncio
import statistics
from typing import List

from hedge.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult

class PriceVolatilityStrategy(HedgeStrategy):
    """价格波动策略 - 基于两个交易所价格波动率的独立控制策略"""
    
    def __init__(self, priority=80):
        super().__init__(open_priority=priority, close_priority=priority)
        # 复用SpreadStrategy的采样配置
        self.sample_count_range = (20, 40)
        self.cache_duration = 5 * 60
        # 价格波动率阈值配置
        # BTC 0.05%~0.08%, 山寨00.1%~0.15%
        self.volatility_threshold = 0.005
        
        # 价格波动率状态
        self.current_volatility = None
        self.primary_prices = []  # primary价格序列
        self.lighter_prices = []  # lighter价格序列
        self.last_calculation_time = 0
        
        self.logger = None
    
    async def can_open(self, hedge_bot):
        """检查价格波动率是否允许开仓"""
        self.logger = hedge_bot.logger
        
        try:
            # 计算当前价格波动率
            await self.calculate_price_volatility(hedge_bot)
            
            # 检查波动率条件
            reason = f"✅ 价格波动率正常: {self.current_volatility:.6f} " \
            f"({self.current_volatility:.4%}) 小于 {self.volatility_threshold:.4f} " \
            f"({self.volatility_threshold:.2%})"
            strategy_result = HedgeStrategyResult.PASS
            if self.current_volatility is not None and self.current_volatility > self.volatility_threshold:
                reason = f"⚠️ 价格波动率过大暂停开仓: {self.current_volatility:.6f} " \
                f"({self.current_volatility:.4%}) 大于 {self.volatility_threshold:.4f} " \
                f"({self.volatility_threshold:.2%})"
                strategy_result = HedgeStrategyResult.REJECT  # 波动率过大，禁止开仓，拦截后续策略的判断进入下一轮循环
            self._set_strategy_context(strategy_result, reason) # 只拦截开仓，不允许直接开仓，交由后续的策略判断
        except Exception as e:
            self._set_strategy_context(result=HedgeStrategyResult.PASS, reason=f"❌ 价格波动率检查失败: {e}")
    
    async def can_close(self, hedge_bot):
        """检查是否需要因波动率过大而平仓"""
        self.logger = hedge_bot.logger
        
        try:
            # 计算当前价格波动率
            await self.calculate_price_volatility(hedge_bot)
            
            # 检查波动率条件
            if self.current_volatility is not None and self.current_volatility > self.volatility_threshold:
                reason = f"⚠️ 价格波动率过大触发平仓: {self.current_volatility:.6f} " \
                        f"({self.current_volatility:.4%}) 大于 {self.volatility_threshold:.4f} " \
                        f"({self.volatility_threshold:.2%})"
                strategy_result = HedgeStrategyResult.TRIGGER  # 波动率过大，主动触发平仓
            else:
                reason = f"✅ 价格波动率正常无需平仓: {self.current_volatility:.6f} " \
                        f"({self.current_volatility:.4%}) 小于 {self.volatility_threshold:.4f} " \
                        f"({self.volatility_threshold:.2%})"
                strategy_result = HedgeStrategyResult.PASS
            
            self._set_strategy_context(strategy_result, reason)
        except Exception as e:
            self._set_strategy_context(result=HedgeStrategyResult.PASS, reason=f"❌ 价格波动率检查失败: {e}")
    
    def _get_msgs(self) -> List[str]:
        return [
            f"📈 当前波动率: {self.current_volatility:.6f} ({self.current_volatility:.4%})" if self.current_volatility is not None else "📈 当前波动率: 未知",
            f"⚖️ 波动率阈值: {self.volatility_threshold:.4f} ({self.volatility_threshold:.2%})",
            f"🏭 Primary价格均值: {statistics.mean(self.primary_prices):.6f}" if self.primary_prices else "🏭 Primary价格均值: 无数据",
            f"💡 Lighter价格均值: {statistics.mean(self.lighter_prices):.6f}" if self.lighter_prices else "💡 Lighter价格均值: 无数据",
            f"📊 采样次数: {len(self.primary_prices)}"
        ]
    
    async def calculate_price_volatility(self, hedge_bot):
        """计算两个交易所价格的波动率"""
        current_time = time.time()
        
        # 1. 先检查共享数据
        shared_data = hedge_bot.triggered_strategies_data.get('sampling_data')
        
        if not shared_data or (current_time - shared_data['sampling_time'] > self.cache_duration):
            # 没有共享数据或已过期，执行采样
            if self.logger:
                self.logger.info(f"📊 共享采样数据已过期或不存在，重新采样")
            
            samples = await self.do_price_sampling(hedge_bot, self.sample_count_range)
            
            if not samples:  # 添加错误处理
                if self.logger:
                    self.logger.error("❌ 无法计算价格波动率：所有采样都失败了")
                raise Exception("无法计算价格波动率：所有采样都失败了")
            
            # 存储到共享数据
            shared_data = {
                'samples': samples,
                'sampling_time': current_time,
                'sample_count': len(samples)
            }
            hedge_bot.triggered_strategies_data['sampling_data'] = shared_data
        else:
            # 使用有效的共享数据
            if self.logger:
                self.logger.info(f"📋 使用共享采样数据计算波动率")
        
        # 提取价格序列并计算波动率
        self.primary_prices = [sample['primary_mid'] for sample in shared_data['samples']]
        self.lighter_prices = [sample['lighter_mid'] for sample in shared_data['samples']]
        self.last_calculation_time = shared_data['sampling_time']
        
        self.current_volatility = self.calculate_volatility_from_prices()
        
        if self.logger:
            self.logger.info(f"✅ 价格波动率计算完成: {self.current_volatility:.6f} "
                           f"({self.current_volatility:.4%}) (基于{len(self.primary_prices)}个样本)")
            self.logger.info(f"📊 Primary均值: {statistics.mean(self.primary_prices):.6f}, "
                           f"Lighter均值: {statistics.mean(self.lighter_prices):.6f}")
            self.logger.info(f"📈 波动率计算方法: 价格变化率标准差 (更敏感的波动检测)")
        
        return self.current_volatility
    
    def calculate_volatility_from_prices(self):
        """方案1：基于价格变化率标准差的波动率计算"""
        if len(self.primary_prices) < 2 or len(self.lighter_prices) < 2:
            return 0
        
        def calculate_price_changes(prices):
            """计算相邻价格的变化率"""
            changes = []
            for i in range(1, len(prices)):
                if prices[i-1] != 0:  # 避免除零
                    change_rate = (prices[i] - prices[i-1]) / prices[i-1]
                    changes.append(change_rate)
            return changes
        
        primary_changes = calculate_price_changes(self.primary_prices)
        lighter_changes = calculate_price_changes(self.lighter_prices)
        
        if len(primary_changes) < 2 or len(lighter_changes) < 2:
            return 0
        
        # 计算变化率的标准差
        primary_volatility = statistics.stdev(primary_changes)
        lighter_volatility = statistics.stdev(lighter_changes)
        
        # 返回综合波动率（两个交易所波动率的平均值）
        return (primary_volatility + lighter_volatility) / 2