from abc import ABC, abstractmethod
import os
import time
import random
import asyncio
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from decimal import Decimal
from enum import Enum


# 策略决策触发原因枚举
class DecisionTrigger(Enum):
    """策略决策触发原因"""
    SPREAD_THRESHOLD = "spread_threshold"      # 价差阈值满足
    TIME_DRIVEN = "time_driven"               # 时间驱动决策
    TIMEOUT = "timeout"                       # 超时触发
    RISK_CONTROL = "risk_control"             # 风险控制触发
    SPREAD_CLOSE = "spread_close"             # 价差平仓
    TIME_CLOSE = "time_close"                 # 时间平仓
    ERROR_TIMEOUT = "error_timeout"           # 错误超时
    MANUAL = "manual"                         # 手动触发
    PROFIT_TARGET = "profit_target"           # 盈利目标达成

@dataclass
class StrategyExecutionContext:
    """策略执行上下文数据类 - 简化版本"""
    reason: str                                 # 具体决策原因描述
    decision_type: str                          # 'open' 或 'close'
    side: str                                  # 开仓/平仓方向 ('buy'/'sell')
    timestamp: float                           # 决策时间戳
    price_data: Dict[str, Any]                 # 决策时的完整价格数据
    trigger: DecisionTrigger                   # 决策触发原因（枚举）
    
    @classmethod
    def create_open_context(cls, reason: str, side: str, price_data: Dict[str, Any], 
                            trigger: DecisionTrigger = DecisionTrigger.SPREAD_THRESHOLD,
                           **kwargs) -> 'StrategyExecutionContext':
        """创建开仓执行上下文"""
        return cls(
            reason=reason,
            decision_type='open',
            side=side,
            timestamp=time.time(),
            price_data=price_data,
            trigger=trigger
        )
    
    @classmethod  
    def create_close_context(cls, reason: str, side: str, price_data: Dict[str, Any],
                            trigger: DecisionTrigger,
                            **kwargs) -> 'StrategyExecutionContext':
        """创建平仓执行上下文"""
        return cls(
            reason=reason,
            decision_type='close', 
            side=side,
            timestamp=time.time(),
            price_data=price_data,
            trigger=trigger
        )

class HedgeStrategy(ABC):
    """极简策略基类 - 提供统一的等待逻辑"""
    
    def __init__(self, open_priority: int = 0, close_priority: int = 0):
        """
        初始化策略基类
        
        Args:
            open_priority: 开仓决策优先级，值越大优先级越高
            close_priority: 平仓决策优先级，值越大优先级越高
        """
        # 必须在can_open中设置具体开仓方向
        self.open_side = 'buy'
        self.open_priority = open_priority
        self.close_priority = close_priority
        self.last_execution_context: Optional[StrategyExecutionContext] = None

        self._verify_env()

    @abstractmethod
    async def can_open(self, hedge_bot) -> bool:
        """检查是否可以开仓 - 子类实现具体逻辑"""
        pass

    @abstractmethod
    async def can_close(self, hedge_bot) -> bool:
        """检查是否可以平仓 - 子类实现具体逻辑"""
        pass
    
    def get_content(self) -> Dict[str, Any]:
        """
        获取策略的完整数据内容
        
        Returns:
            Dict包含以下字段:
            - strategy_name: 策略名称
            - reason: 触发原因
            - side: 开仓/平仓方向
            - price_data: 价格数据字典
            - tg_msg: 策略特定的tg消息
            - trigger_type: 触发类型
        """
        if not self.last_execution_context:
            return {}
        
        context = self.last_execution_context
        
        base_content = {
            'strategy_name': self.__class__.__name__,
            'reason': context.reason,
            'side': context.side,
            'price_data': context.price_data or {},
            'trigger_type': context.trigger.value,
            'tg_msg': self._gen_tg_msg()
        }
        
        return base_content
    
    @abstractmethod  
    def _gen_tg_msg(self) -> List[str]:
        """获取策略特定的详细信息用于tg通知 - 子类实现"""
        pass
    
    @abstractmethod
    def _verify_env(self) -> bool:
        """验证策略所需的环境变量是否齐全 - 子类实现"""
        pass
    
    async def _get_current_price_data(self, hedge_bot):
        """获取当前价格数据的简化版本"""
        try:
            # 获取双边价格
            result = await asyncio.gather(
                hedge_bot.fetch_primary_bbo_prices(),
                hedge_bot.lighter.fetch_bbo_prices()
            )
            primary_bid, primary_ask = result[0]
            lighter_bid, lighter_ask = result[1]
            
            primary_mid = (primary_bid + primary_ask) / 2
            lighter_mid = (lighter_bid + lighter_ask) / 2
            spread = abs(float(primary_mid - lighter_mid))
            
            return {
                'primary_mid': float(primary_mid),
                'lighter_mid': float(lighter_mid),
                'spread': spread,
                'primary_bid': float(primary_bid),
                'primary_ask': float(primary_ask),
                'lighter_bid': float(lighter_bid),
                'lighter_ask': float(lighter_ask)
            }
        except Exception as e:
            self.logger.error(f"获取价格数据失败: {e}")
            return {}
        
    def format_time(self, timestamp: float) -> str:
        """格式化时间戳为可读字符串"""
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))

class LiquidRiskStrategy(HedgeStrategy):
    """流动性风险策略 - 最高优先级，专注于风险控制"""
    
    def __init__(self):
        super().__init__(open_priority=100, close_priority=100)
        self.risk_threshold = Decimal(os.getenv('RISK_THRESHOLD', '0.2'))
        self.logger = None
        self.risk_exchange = None
        self.risk_liquidation_price = None
        
    def _verify_env(self) -> bool:
        """验证策略所需的环境变量是否齐全"""
        return os.getenv('RISK_THRESHOLD') is not None
    
    async def can_open(self, hedge_bot) -> bool:
        """风险策略不主动触发开仓，只做被动检查"""
        return False
        
    async def can_close(self, hedge_bot) -> bool:
        """检查清算风险，如有风险则立即平仓"""
        self.logger = hedge_bot.logger
        
        try:
            # 获取当前价格数据
            current_sample = await self._get_current_price_data(hedge_bot)
            
            # 检查清算风险
            risk_triggered = await self._check_liquidation_risk(hedge_bot, current_sample)
            
            if risk_triggered:
                reason = "🚨 风险控制触发：价格接近清算线，立即双边平仓"
                self.logger.warning(reason)
                
                # 记录执行上下文
                self.last_execution_context = StrategyExecutionContext.create_close_context(
                    reason=reason,
                    side='sell' if self.open_side == 'buy' else 'buy',
                    price_data=current_sample,
                    trigger=DecisionTrigger.RISK_CONTROL,
                )
                return True  # 立即触发平仓
            self.logger.info(f"[清算价格策略] 未触发")
            return False
            
        except Exception as e:
            self.logger.error(f"❌ 风险检查失败: {e}")
            return False
    
    def _gen_tg_msg(self) -> List[str]:
        """获取风险策略的特定信息"""
        if self.last_execution_context:
            if self.last_execution_context.decision_type == 'close':
                return [
                    f"⚠️ 风险控制: 触发清算保护机制",
                    f"🏦 风险交易所: {self.risk_exchange}",
                    f"💰 清算价格: {self.risk_liquidation_price:.6f}",
                    f"📊 当前价格: {self.last_execution_context.price_data.get('primary_mid', 0):.6f}",
                    f"📊 风险阈值: {self.risk_threshold:.1%}",
                    f"🕒 执行时间: 立即"
                ]
        return []
    
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
                f"距离比例{price_distance_ratio:.2%} <= {self.risk_threshold:.2%}"
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


class TimingStrategy(HedgeStrategy):
    """时间策略 - 中等优先级，集成TimingController功能"""
    
    def __init__(self):
        super().__init__(open_priority=10, close_priority=10)
        self.open_wait_range = os.getenv('TIME_STRATEGY_OPEN_WAIT_RANGE', '5,20')
        self.open_wait_range = tuple(map(int, self.open_wait_range.split(',')))
        self.close_wait_range = os.getenv('TIME_STRATEGY_CLOSE_WAIT_RANGE', '30,30')
        self.close_wait_range = tuple(map(int, self.close_wait_range.split(',')))
        
        # 时间控制状态
        self.next_open_time = self.schedule_next_open(*self.open_wait_range)
        self.next_close_time = self.schedule_next_close(*self.close_wait_range)
        
        # 决策时间记录
        self.open_decision_start_time = time.time()
        self.close_decision_start_time = time.time()
        
        self.logger = None
   
    def _verify_env(self):
        return os.getenv('TIME_STRATEGY_OPEN_WAIT_RANGE') is not None and \
               os.getenv('TIME_STRATEGY_CLOSE_WAIT_RANGE') is not None
        
    async def can_open(self, hedge_bot) -> bool:
        """检查是否可以基于时间开仓"""
        self.logger = hedge_bot.logger
        self.open_decision_start_time = time.time()
        
        try:
            # 检查时间条件
            if self.can_open_by_time():
                reason = "⏰ 时间维度满足：到达预定开仓时间"
                self.logger.info(reason)
                
                # 获取当前价格确定开仓方向
                current_sample = await self._get_current_price_data(hedge_bot)
                if current_sample['primary_mid'] < current_sample['lighter_mid']:
                    self.open_side = 'buy'
                else:
                    self.open_side = 'sell'
                
                # 调度下次平仓时间
                self.schedule_next_close(*self.close_wait_range)
                
                # 记录执行上下文
                self.last_execution_context = StrategyExecutionContext.create_open_context(
                    reason=reason,
                    side=self.open_side,
                    price_data=current_sample,
                    trigger=DecisionTrigger.TIME_DRIVEN,
                )
                
                self._reset_open_decision_time()
                return True
            self.logger.info(f"[时间策略] 未触发")
            return False
            
        except Exception as e:
            self.logger.error(f"❌ 时间策略开仓检查失败: {e}")
            return False
    
    async def can_close(self, hedge_bot) -> bool:
        """检查是否可以基于时间平仓"""
        self.logger = hedge_bot.logger
        self.close_decision_start_time = time.time()
        
        try:
            # 检查时间条件
            if self.can_close_by_time():
                reason = "⏰ 时间维度满足：到达预定平仓时间"
                self.logger.info(reason)
                
                # 获取当前价格数据
                current_sample = await self._get_current_price_data(hedge_bot)
                
                self.schedule_next_open(*self.open_wait_range)
                
                # 记录执行上下文
                self.last_execution_context = StrategyExecutionContext.create_close_context(
                    reason=reason,
                    side='sell' if self.open_side == 'buy' else 'buy',
                    price_data=current_sample,
                    trigger=DecisionTrigger.TIME_CLOSE,
                )
                
                self.record_close()
                self._reset_close_decision_time()
                return True
            self.logger.info(f"[时间策略] 未触发")
            return False
            
        except Exception as e:
            self.logger.error(f"❌ 时间策略平仓检查失败: {e}")
            return False
    
    def _gen_tg_msg(self) -> List[str]:
        """获取时间策略的特定信息"""
        if not self.last_execution_context:
            return []
        
        context = self.last_execution_context
        if context.decision_type == 'open':
            return [
                f"⏰ 时间策略: {context.trigger.value}",
                f"📅 到达开仓时间: {self.format_time(self.next_open_time)}",
                f"📅 预计平仓: {self.format_time(self.next_close_time)}",
            ]
        else:  # close
            return [
                f"⏰ 时间策略: {context.trigger.value}",
                f"📅 到达平仓时间: {self.format_time(self.next_close_time)}",
                f"📅 下次开仓: {self.format_time(self.next_open_time)}"
            ]
    
    def can_open_by_time(self) -> bool:
        """检查是否可以基于时间开仓"""
        if self.next_open_time is None:
            return True
        
        return time.time() >= self.next_open_time
    
    def can_close_by_time(self) -> bool:
        """检查是否应该基于时间平仓"""
        if self.next_close_time is None:
            return False
        
        return time.time() >= self.next_close_time
    
    def schedule_next_close(self, min_minutes: int, max_minutes: int):
        """调度下次平仓时间"""
        wait_minutes = random.uniform(min_minutes, max_minutes)
        self.next_close_time = time.time() + (wait_minutes * 60)
        if self.logger:
            self.logger.info(f"⏰ 调度平仓时间：{wait_minutes:.1f}分钟后, 时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.next_close_time))}")
    
    def schedule_next_open(self, min_minutes: int, max_minutes: int):
        """调度下次开仓时间"""
        wait_minutes = random.uniform(min_minutes, max_minutes)
        self.next_open_time = time.time() + (wait_minutes * 60)
        if self.logger:
            self.logger.info(f"⏰ 调度开仓时间：{wait_minutes:.1f}分钟后, 时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.next_open_time))}")
    
    def record_close(self):
        """记录平仓，标记非首次交易"""
        self.next_close_time = None

    def _reset_open_decision_time(self):
        """重置开仓决策时间"""
        self.open_decision_start_time = None
    
    def _reset_close_decision_time(self):
        """重置平仓决策时间"""
        self.close_decision_start_time = None
    
class SpreadStrategy(HedgeStrategy):
    """价差策略 - 集成SpreadSampler功能"""
    
    def __init__(self):
        super().__init__(open_priority=20, close_priority=20)
        self.sample_count_range = os.getenv('SPREAD_STRATEGY_SAMPLE_COUNT_RANGE', '5,10')
        self.sample_count_range = tuple(map(int, self.sample_count_range.split(',')))
        self.cache_duration = int(os.getenv('SPREAD_STRATEGY_CACHE_DURATION', '60'))
        self.profit_threshold = float(os.getenv('SPREAD_STRATEGY_PROFIT_THRESHOLD', '0.1'))
        
        # 价差采样状态
        self.open_spread = None
        self.close_spread = None
        self.current_spread = None
        self.average_spread = None
        self.spread_cache = []
        self.last_calculation_time = 0
        
        self.logger = None
    
    def _verify_env(self):
        return os.getenv('SPREAD_STRATEGY_SAMPLE_COUNT_RANGE') is not None and \
               os.getenv('SPREAD_STRATEGY_CACHE_DURATION') is not None and \
               os.getenv('SPREAD_STRATEGY_PROFIT_THRESHOLD') is not None

    async def can_open(self, hedge_bot) -> bool:
        """检查是否可以基于价差开仓"""
        self.logger = hedge_bot.logger
        
        try:
            # todo: 波动率检查: btc2%，sol/eth/bnb4%，山寨6%
            # 在0.05%，刷大币磨损在0.01%
            # 第一次开仓：初始化平均价差
            if self.average_spread is None:
                self.logger.info("🎯 第一次开仓：初始化价差基准")
                await self.calculate_average_spread(hedge_bot, force_refresh=True)
            
            # 获取当前价差
            current_sample = await self._get_current_price_data(hedge_bot)
            self.current_spread = current_sample['spread']
            
            # 检查价差条件
            if self.should_open_by_spread():
                reason = f"✅ 价差维度满足：当前{self.current_spread:.6f} > {self.average_spread * (1 + self.profit_threshold):.6f}"
                self.logger.info(reason)
                self.open_spread = self.current_spread
                
                # 确定开仓方向
                if current_sample['primary_mid'] < current_sample['lighter_mid']:
                    self.open_side = 'buy'
                else:
                    self.open_side = 'sell'
                
                # 记录执行上下文
                self.last_execution_context = StrategyExecutionContext.create_open_context(
                    reason=reason,
                    side=self.open_side,
                    price_data=current_sample,
                    trigger=DecisionTrigger.SPREAD_THRESHOLD,
                )
                return True
            
            self.logger.info(f"[价差策略] 未触发")
            return False
            
        except Exception as e:
            self.logger.error(f"❌ 价差策略开仓检查失败: {e}")
            return False
    
    async def can_close(self, hedge_bot) -> bool:
        """检查是否可以基于价差平仓"""
        self.logger = hedge_bot.logger
        
        try:
            # 获取当前价差
            current_sample = await self._get_current_price_data(hedge_bot)
            self.current_spread = current_sample['spread']
            
            # 检查价差平仓条件
            if self.should_close_by_spread():
                reason = f"✅ 价差维度满足平仓：当前{self.current_spread:.6f} < {self.average_spread * (1 - self.profit_threshold):.6f}"
                self.logger.info(reason)
                
                # 记录执行上下文
                self.last_execution_context = StrategyExecutionContext.create_close_context(
                    reason=reason,
                    side='sell' if self.open_side == 'buy' else 'buy',
                    price_data=current_sample,
                    trigger=DecisionTrigger.SPREAD_CLOSE,
                )
                return True
            
            self.logger.info(f"[价差策略] 未触发")
            return False
            
        except Exception as e:
            self.logger.error(f"❌ 价差策略平仓检查失败: {e}")
            return False
    
    def _gen_tg_msg(self) -> List[str]:
        """获取价差策略的特定信息"""
        if not self.last_execution_context:
            return []
        
        context = self.last_execution_context
        if context.decision_type == 'open':
            return [
                f"📊 价差策略: 价差大于阈值开仓",
                f"📉 开仓价差: {self.open_spread:.6f}",
                f"📉 当前价差: {self.current_spread:.6f}",
                f"📈 平均价差: {self.average_spread:.6f}",
                f"🎯 盈利阈值: {self.profit_threshold:.1%}"
            ]
        else:  # close
            return [
                f"📊 价差策略: 价差收敛触发平仓",
                f"📉 开仓价差: {self.open_spread:.6f}",
                f"📉 平仓价差: {self.close_spread:.6f}",
                f"📉 当前价差: {self.current_spread:.6f}",
                f"📈 平均价差: {self.average_spread:.6f}",
                f"💰 预期盈利: {self.profit_threshold:.1%}"
            ]
    
    # 价差采样方法
    async def calculate_average_spread(self, hedge_bot, force_refresh=False):
        """计算平均价差"""
        current_time = time.time()
        
        # 检查缓存是否有效
        if not force_refresh and self.average_spread is not None:
            if current_time - self.last_calculation_time < self.cache_duration:
                if self.logger:
                    self.logger.debug(f"📋 使用缓存的平均价差: {self.average_spread:.6f}")
                return self.average_spread
        
        # 生成采样次数
        sample_count = random.randint(*self.sample_count_range)
        if self.logger:
            self.logger.info(f"📊 开始价差采样，计划采样 {sample_count} 次")
        
        spreads = []
        
        for i in range(sample_count):
            try:
                sample = await self._get_current_price_data(hedge_bot)
                spreads.append(sample['spread'])
                
                if self.logger:
                    self.logger.debug(f"📈 采样 {i+1}/{sample_count}: {sample['spread']:.6f}")
                
                # 采样间隔
                if i < sample_count - 1:
                    await asyncio.sleep(2)
                    
            except Exception as e:
                if self.logger:
                    self.logger.error(f"❌ 价差采样失败 {i+1}/{sample_count}: {e}")
                continue
        
        if spreads:
            self.average_spread = sum(spreads) / len(spreads)
            self.last_calculation_time = current_time
            if self.logger:
                self.logger.info(f"✅ 平均价差计算完成: {self.average_spread:.6f} (基于{len(spreads)}个样本)")
            return self.average_spread
        else:
            if self.logger:
                self.logger.error("❌ 无法计算平均价差：所有采样都失败了")
            raise Exception("无法计算平均价差：所有采样都失败了")
    
    def should_open_by_spread(self) -> bool:
        """检查价差是否满足开仓条件"""
        if self.average_spread is None or self.current_spread is None:
            return False

        threshold = self.average_spread * (1 + self.profit_threshold)
        return self.current_spread > threshold

    def should_close_by_spread(self) -> bool:
        """检查价差是否满足平仓条件"""
        if self.average_spread is None or self.current_spread is None:
            return False

        threshold = self.average_spread * (1 - self.profit_threshold)
        return self.current_spread < threshold


