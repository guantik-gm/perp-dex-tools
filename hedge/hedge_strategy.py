from abc import ABC, abstractmethod
import time
import random
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any, Optional
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
    estimated_close_minutes: int               # 预计平仓时间（分钟）
    trigger: DecisionTrigger                   # 决策触发原因（枚举）
    next_open_minutes: Optional[float] = None  # 下次开仓预计时间（分钟），仅平仓时有值
    
    # 关键度量指标
    current_spread: Optional[float] = None     # 当前价差
    average_spread: Optional[float] = None     # 平均价差
    profit_threshold: Optional[float] = None   # 盈利阈值
    
    @classmethod
    def create_open_context(cls, reason: str, side: str, price_data: Dict[str, Any], 
                           estimated_close_minutes: int, trigger: DecisionTrigger = DecisionTrigger.SPREAD_THRESHOLD,
                           **kwargs) -> 'StrategyExecutionContext':
        """创建开仓执行上下文"""
        return cls(
            reason=reason,
            decision_type='open',
            side=side,
            timestamp=time.time(),
            price_data=price_data,
            estimated_close_minutes=estimated_close_minutes,
            trigger=trigger,
            current_spread=kwargs.get('current_spread'),
            average_spread=kwargs.get('average_spread'),
            profit_threshold=kwargs.get('profit_threshold')
        )
    
    @classmethod  
    def create_close_context(cls, reason: str, side: str, price_data: Dict[str, Any],
                           estimated_close_minutes: int, trigger: DecisionTrigger,
                           next_open_minutes: Optional[float] = None, **kwargs) -> 'StrategyExecutionContext':
        """创建平仓执行上下文"""
        return cls(
            reason=reason,
            decision_type='close', 
            side=side,
            timestamp=time.time(),
            price_data=price_data,
            estimated_close_minutes=estimated_close_minutes,
            trigger=trigger,
            next_open_minutes=next_open_minutes,
            current_spread=kwargs.get('current_spread'),
            average_spread=kwargs.get('average_spread'),
            profit_threshold=kwargs.get('profit_threshold')
        )

class HedgeStrategy(ABC):
    """Abstract base class for hedge strategies."""
        
    def __init__(self):    
        self.open_side = 'buy'
    
    @abstractmethod
    async def wait_open(self, hedge_bot):
        """Wait until hedge strategy conditions are met to open a position."""
        pass

    @abstractmethod
    async def wait_close(self, hedge_bot):
        """Wait until hedge strategy conditions are met to close a position."""
        pass


class SpreadSampler:
    """价差采样和分析器"""
    
    def __init__(self, sample_count_range=(10, 20), cache_duration=300):
        self.sample_count_range = sample_count_range
        self.cache_duration = cache_duration  # 缓存5分钟
        self.spread_history = []
        self.average_spread = None
        self.last_update_time = None
        self.logger = None
    
    async def sample_current_spread(self, primary_client, lighter_proxy):
        """采样当前双边价差"""
        try:
            results = await asyncio.gather(
                # 获取EdgeX最优买卖价 - 需要传入contract_id
                primary_client.fetch_bbo_prices(primary_client.config.contract_id),
                # 获取Lighter最优买卖价 - 通过lighter_proxy获取
                lighter_proxy.fetch_bbo_prices()
            )
            primary_bid, primary_ask = results[0]
            lighter_bid, lighter_ask = results[1]
            
            # 计算价差：价格低的开多，价格高的开空
            primary_mid = (primary_bid + primary_ask) / 2
            lighter_mid = (lighter_bid + lighter_ask) / 2
            
            spread = abs(primary_mid - lighter_mid)
            
            return {
                'spread': spread,
                'primary_mid': primary_mid,
                'lighter_mid': lighter_mid,
                'primary_bid': primary_bid,
                'primary_ask': primary_ask,
                'lighter_bid': lighter_bid,
                'lighter_ask': lighter_ask,
                'timestamp': time.time()
            }
        except Exception as e:
            if self.logger:
                self.logger.error(f"采样价差失败: {e}")
            raise
    
    async def calculate_average_spread(self, primary_client, lighter_proxy, force_refresh=False):
        """计算平均价差，支持缓存"""
        current_time = time.time()
        
        # 检查缓存是否有效
        if not force_refresh and self.average_spread is not None:
            if self.last_update_time and (current_time - self.last_update_time) < self.cache_duration:
                return self.average_spread
        
        # 确定采样次数
        sample_count = random.randint(*self.sample_count_range)
        self.spread_history = []
        
        if self.logger:
            self.logger.info(f"🔍 开始价差采样，目标样本数: {sample_count}")
        
        # 采样循环
        for i in range(sample_count):
            try:
                sample = await self.sample_current_spread(primary_client, lighter_proxy)
                self.spread_history.append(sample)
                
                # 采样间隔随机化，避免固定模式
                await asyncio.sleep(random.uniform(0.5, 2.0))
                
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"采样失败 {i+1}/{sample_count}: {e}")
                continue
        
        if len(self.spread_history) >= 3:  # 最少3个有效样本
            spreads = [s['spread'] for s in self.spread_history]
            self.average_spread = sum(spreads) / len(spreads)
            self.last_update_time = current_time
            
            if self.logger:
                self.logger.info(f"📊 价差采样完成: {len(spreads)}个样本, 平均价差: {self.average_spread:.6f}")
            return self.average_spread
        else:
            raise Exception("采样失败：有效样本不足")
    
    def should_open_by_spread(self, current_spread):
        """基于价差判断是否应该开仓"""
        if self.average_spread is None:
            return False
        return current_spread > self.average_spread
    
    def should_close_by_spread(self, current_spread, profit_threshold=0.05):
        """基于价差判断是否应该平仓"""
        if self.average_spread is None:
            return False
        
        # 确保profit_threshold是Decimal类型
        from decimal import Decimal
        if not isinstance(profit_threshold, Decimal):
            profit_threshold = Decimal(str(profit_threshold))
        
        # 价差缩小到平均价差内，且满足盈利阈值
        spread_condition = current_spread <= self.average_spread
        profit_condition = current_spread > self.average_spread * (Decimal('1') - profit_threshold)
        
        return spread_condition and profit_condition


class TimingController:
    """智能时间控制器"""
    
    def __init__(self):
        self.last_close_time = None
        self.next_open_time = None
        self.next_close_time = None
        self.current_close_wait_minutes = None  # 当前平仓等待时间
        self.is_first_trade = True
        self.logger = None
    
    def schedule_next_open(self, min_minutes=5, max_minutes=20):
        """调度下次开仓时间，返回是否立即可开仓"""
        if self.last_close_time is None:
            return True  # 第一次交易立即执行
        
        wait_minutes = random.uniform(min_minutes, max_minutes)
        self.next_open_time = self.last_close_time + (wait_minutes * 60)
        
        if self.logger:
            self.logger.info(f"⏰ 下次开仓时间: {wait_minutes:.1f}分钟后")
        return False
    
    def schedule_next_close(self, min_minutes=30, max_minutes=240):
        """调度下次平仓时间"""
        wait_minutes = random.uniform(min_minutes, max_minutes)
        self.next_close_time = time.time() + (wait_minutes * 60)
        self.current_close_wait_minutes = int(wait_minutes)  # 保存实际等待时间
        
        if self.logger:
            self.logger.info(f"⏰ 预计平仓时间: {wait_minutes:.1f}分钟后")
    
    def can_open_by_time(self):
        """基于时间判断是否可以开仓"""
        if self.is_first_trade:
            return True
        
        if self.next_open_time is None:
            return True
        
        return time.time() >= self.next_open_time
    
    def should_close_by_time(self):
        """基于时间判断是否应该平仓"""
        if self.next_close_time is None:
            return False
        
        return time.time() >= self.next_close_time
    
    def record_close(self):
        """记录平仓时间"""
        self.last_close_time = time.time()
        self.is_first_trade = False
        self.next_close_time = None


class SmartHedgeStrategy(HedgeStrategy):
    """EdgeX-Lighter智能对冲策略"""
    
    def __init__(self, 
                 sample_count_range=(5, 10),
                 cache_duration=60,
                 profit_threshold=0.05,
                 open_wait_range=(5, 20),
                 close_wait_range=(30, 240),
                 sleep_time=30,
                 risk_threshold=0.20,
                 max_open_wait_minutes=30,
                 max_close_wait_minutes=60):
        super().__init__() 
        
        # 初始化核心组件
        self.spread_sampler = SpreadSampler(sample_count_range, cache_duration)
        self.timing_controller = TimingController()
        self.sleep_time = sleep_time
        
        self.risk_threshold = risk_threshold
        # 配置参数
        self.profit_threshold = profit_threshold
        self.open_wait_range = open_wait_range
        self.close_wait_range = close_wait_range
        self.max_open_wait_minutes = max_open_wait_minutes    # 最大开仓等待时间
        self.max_close_wait_minutes = max_close_wait_minutes  # 最大平仓等待时间
        
        # 状态记录
        self.open_decision_start_time = None    # 开仓决策开始时间
        self.close_decision_start_time = None   # 平仓决策开始时间
        
        # 执行上下文管理 - 使用数据类管理策略决策信息
        self.last_execution_context: Optional[StrategyExecutionContext] = None
    
    def _setup_logger(self, hedge_bot):
        """设置logger引用"""
        self.spread_sampler.logger = hedge_bot.logger
        self.timing_controller.logger = hedge_bot.logger
    
    def get_execution_context(self) -> Optional['StrategyExecutionContext']:
        """获取最后的策略执行上下文"""
        return self.last_execution_context
    
    def _set_open_execution_context(self, reason: str, price_data: Dict[str, Any], side: str, trigger: DecisionTrigger = DecisionTrigger.SPREAD_THRESHOLD, additional_metrics: Dict[str, Any] = None):
        """设置开仓策略执行上下文"""
        # 从additional_metrics中提取关键指标，如果没有则使用默认值
        kwargs = {}
        if additional_metrics:
            kwargs.update({
                'current_spread': additional_metrics.get('current_spread'),
                'average_spread': additional_metrics.get('average_spread'),
                'profit_threshold': additional_metrics.get('profit_threshold')
            })
        
        # 使用TimingController中实际计算的等待时间
        estimated_minutes = self.timing_controller.current_close_wait_minutes or self.max_close_wait_minutes
        
        self.last_execution_context = StrategyExecutionContext.create_open_context(
            reason=reason,
            side=side,
            price_data=price_data,
            estimated_close_minutes=estimated_minutes,
            trigger=trigger,
            **kwargs
        )
    
    def _set_close_execution_context(self, reason: str, price_data: Dict[str, Any], side: str, trigger: DecisionTrigger, next_open_minutes: float = None, additional_metrics: Dict[str, Any] = None):
        """设置平仓策略执行上下文"""
        # 从additional_metrics中提取关键指标，如果没有则使用默认值
        kwargs = {}
        if additional_metrics:
            kwargs.update({
                'current_spread': additional_metrics.get('current_spread'),
                'average_spread': additional_metrics.get('average_spread'),
                'profit_threshold': additional_metrics.get('profit_threshold')
            })
        
        # 使用TimingController中实际计算的等待时间
        estimated_minutes = self.timing_controller.current_close_wait_minutes or self.max_close_wait_minutes
        
        self.last_execution_context = StrategyExecutionContext.create_close_context(
            reason=reason,
            side=side,
            price_data=price_data,
            estimated_close_minutes=estimated_minutes,
            trigger=trigger,
            next_open_minutes=next_open_minutes or 15.0,  # 默认15分钟后开仓
            **kwargs
        )
    
    async def wait_open(self, hedge_bot):
        """智能开仓决策：等待价差+时间双维度条件满足"""
        self._setup_logger(hedge_bot)
        logger = hedge_bot.logger
        
        # 初始化开仓决策开始时间
        if self.open_decision_start_time is None:
            self.open_decision_start_time = time.time()
        
        logger.info("⏳ 开始开仓决策...")
        
        while not hedge_bot.stop_flag:
            try:
                # 第一次开仓：初始化平均价差
                if self.timing_controller.is_first_trade:
                    logger.info("🎯 第一次开仓：初始化价差基准")
                    await self.spread_sampler.calculate_average_spread(
                        hedge_bot.primary_client, hedge_bot.lighter, force_refresh=True
                    )
                
                # 统一的开仓逻辑：价差+时间双维度判断
                # 获取当前价差
                current_sample = await self.spread_sampler.sample_current_spread(
                    hedge_bot.primary_client, hedge_bot.lighter
                )
                current_spread = current_sample['spread']
                
                # 维度1：价差判断
                spread_favorable = self.spread_sampler.should_open_by_spread(current_spread)
                
                if spread_favorable:
                    reason = f"✅ 价差维度满足：当前{current_spread:.6f} > 平均{self.spread_sampler.average_spread:.6f}"
                    logger.info(reason)
                    
                    # 确定开仓方向
                    if current_sample['primary_mid'] < current_sample['lighter_mid']:
                        self.open_side = 'buy'
                    else:
                        self.open_side = 'sell'

                    # 记录执行上下文
                    self._set_open_execution_context(
                        reason=reason,
                        price_data=current_sample,
                        side=self.open_side,
                        trigger=DecisionTrigger.SPREAD_THRESHOLD,
                        additional_metrics={
                            'current_spread': current_spread,
                            'average_spread': self.spread_sampler.average_spread
                        }
                    )

                    self.timing_controller.schedule_next_close(*self.close_wait_range)
                    self._reset_open_decision_time()
                    return  # 条件满足，退出等待
                
                # 维度2：时间判断
                elif self.timing_controller.can_open_by_time():
                    reason = "⏰ 时间维度满足：到达预定开仓时间"
                    logger.info(reason)
                    
                    # 时间驱动的开仓也需要确定方向
                    if current_sample['primary_mid'] < current_sample['lighter_mid']:
                        self.open_side = 'buy'
                    else:
                        self.open_side = 'sell'
                    
                    # 记录执行上下文
                    self._set_open_execution_context(
                        reason=reason,
                        price_data=current_sample,
                        side=self.open_side,
                        trigger=DecisionTrigger.TIME_DRIVEN,
                        additional_metrics={
                            'current_spread': current_spread,
                            'average_spread': self.spread_sampler.average_spread
                        }
                    )
                    
                    self.timing_controller.schedule_next_close(*self.close_wait_range)
                    self._reset_open_decision_time()
                    return  # 条件满足，退出等待
                
                # 维度3：超时保护 - 策略内部处理最大等待时间
                elif self._is_open_timeout():
                    wait_minutes = (time.time() - self.open_decision_start_time) / 60
                    reason = f"⏰ 超时保护触发：已等待{wait_minutes:.1f}分钟，强制开仓"
                    logger.warning(reason)
                    
                    # 超时情况下也需要确定方向
                    if current_sample['primary_mid'] < current_sample['lighter_mid']:
                        self.open_side = 'buy'
                    else:
                        self.open_side = 'sell'
                    
                    # 记录执行上下文
                    self._set_open_execution_context(
                        reason=reason,
                        price_data=current_sample,
                        side=self.open_side,
                        trigger=DecisionTrigger.TIMEOUT,
                        additional_metrics={
                            'current_spread': current_spread,
                            'average_spread': self.spread_sampler.average_spread
                        }
                    )
                    
                    self.timing_controller.schedule_next_close(*self.close_wait_range)
                    self._reset_open_decision_time()
                    return  # 超时保护，退出等待
                
                else:
                    time_remaining = max(0, self.timing_controller.next_open_time - time.time()) if self.timing_controller.next_open_time else 0
                    wait_elapsed = (time.time() - self.open_decision_start_time) / 60
                    logger.info(f"⏸️ 等待中：价差不利且时间未到 (已等待{wait_elapsed:.1f}分钟，还需{time_remaining/60:.1f}分钟)")
                    
                    await asyncio.sleep(self.sleep_time)
                        
            except Exception as e:
                logger.error(f"❌ 开仓策略执行失败: {e}")
                # 出错时也触发超时保护
                if self._is_open_timeout():
                    wait_minutes = (time.time() - self.open_decision_start_time) / 60
                    reason = f"⚠️ 策略执行失败且超时：已等待{wait_minutes:.1f}分钟，强制开仓"
                    logger.warning(reason)
                    
                    # 设置默认方向和上下文
                    self.open_side = 'buy'  # 默认开多
                    self._set_open_execution_context(
                        reason=reason,
                        price_data={},  # 异常情况下没有完整价格数据
                        side=self.open_side,
                        trigger=DecisionTrigger.ERROR_TIMEOUT
                    )
                    
                    self._reset_open_decision_time()
                    return  # 超时保护，退出等待
                # 出错时等待后重试
                await asyncio.sleep(self.sleep_time)
        
        # 如果stop_flag被设置，抛出异常通知调用者停止
        raise asyncio.CancelledError("开仓等待被中断")
    
    def _is_open_timeout(self) -> bool:
        """检查是否超过最大开仓等待时间"""
        if self.open_decision_start_time is None:
            return False
        elapsed_minutes = (time.time() - self.open_decision_start_time) / 60
        return elapsed_minutes >= self.max_open_wait_minutes
    
    def _reset_open_decision_time(self):
        """重置开仓决策时间"""
        self.open_decision_start_time = None
    
    async def wait_close(self, hedge_bot):
        """智能平仓决策：等待价差+时间+盈利双维度条件满足，并实施风险控制"""
        self._setup_logger(hedge_bot)
        logger = hedge_bot.logger
        
        # 初始化平仓决策开始时间
        if self.close_decision_start_time is None:
            self.close_decision_start_time = time.time()
        
        logger.info("⏳ 开始平仓决策...")
        
        while not hedge_bot.stop_flag:
            try:
                # 获取当前价差
                current_sample = await self.spread_sampler.sample_current_spread(
                    hedge_bot.primary_client, hedge_bot.lighter
                )
                current_spread = current_sample['spread']
                
                # 🚨 风险控制检查：优先级最高，先检查爆仓风险
                risk_control_triggered = await self._check_liquidation_risk(
                    hedge_bot, current_sample
                )
                
                if risk_control_triggered:
                    reason = "🚨 风险控制触发：价格接近清算线，立即双边平仓"
                    logger.warning(reason)
                    
                    # 记录执行上下文
                    self._set_close_execution_context(
                        reason=reason,
                        price_data=current_sample,
                        side='risk_control',  # 风险控制平仓
                        trigger=DecisionTrigger.RISK_CONTROL,
                        next_open_minutes=random.uniform(*self.open_wait_range),
                        additional_metrics={
                            'current_spread': current_spread
                        }
                    )
                    
                    self.timing_controller.record_close()
                    self.timing_controller.schedule_next_open(*self.open_wait_range)
                    self._reset_close_decision_time()
                    return  # 风险控制优先，立即退出
                
                # 维度1：价差+盈利判断
                spread_should_close = self.spread_sampler.should_close_by_spread(
                    current_spread, self.profit_threshold
                )
                
                if spread_should_close:
                    reason = f"✅ 价差维度满足平仓：当前{current_spread:.6f} <= 平均{self.spread_sampler.average_spread:.6f}且满足盈利阈值"
                    logger.info(reason)
                    
                    # 记录执行上下文
                    self._set_close_execution_context(
                        reason=reason,
                        price_data=current_sample,
                        side='spread_profit',  # 价差+盈利平仓
                        trigger=DecisionTrigger.SPREAD_CLOSE,
                        next_open_minutes=random.uniform(*self.open_wait_range),
                        additional_metrics={
                            'current_spread': current_spread,
                            'average_spread': self.spread_sampler.average_spread,
                            'profit_threshold': self.profit_threshold
                        }
                    )
                    
                    self.timing_controller.record_close()
                    self.timing_controller.schedule_next_open(*self.open_wait_range)
                    self._reset_close_decision_time()
                    return  # 条件满足，退出等待
                
                # 维度2：时间判断
                elif self.timing_controller.should_close_by_time():
                    reason = "⏰ 时间维度满足：到达预定平仓时间"
                    logger.info(reason)
                    
                    # 记录执行上下文
                    self._set_close_execution_context(
                        reason=reason,
                        price_data=current_sample,
                        side='time_driven',  # 时间驱动平仓
                        trigger=DecisionTrigger.TIME_CLOSE,
                        next_open_minutes=random.uniform(*self.open_wait_range),
                        additional_metrics={
                            'current_spread': current_spread,
                            'average_spread': self.spread_sampler.average_spread
                        }
                    )
                    
                    self.timing_controller.record_close()
                    self.timing_controller.schedule_next_open(*self.open_wait_range)
                    self._reset_close_decision_time()
                    return  # 条件满足，退出等待
                
                # 维度3：超时保护 - 策略内部处理最大等待时间
                elif self._is_close_timeout():
                    wait_minutes = (time.time() - self.close_decision_start_time) / 60
                    reason = f"⏰ 超时保护触发：已等待{wait_minutes:.1f}分钟，强制平仓"
                    logger.warning(reason)
                    
                    # 记录执行上下文
                    self._set_close_execution_context(
                        reason=reason,
                        price_data=current_sample,
                        side='timeout',  # 超时平仓
                        trigger=DecisionTrigger.TIMEOUT,
                        next_open_minutes=random.uniform(*self.open_wait_range),
                        additional_metrics={
                            'current_spread': current_spread,
                            'average_spread': self.spread_sampler.average_spread
                        }
                    )
                    
                    self.timing_controller.record_close()
                    self.timing_controller.schedule_next_open(*self.open_wait_range)
                    self._reset_close_decision_time()
                    return  # 超时保护，退出等待
                
                else:
                    time_remaining = max(0, self.timing_controller.next_close_time - time.time()) if self.timing_controller.next_close_time else 0
                    wait_elapsed = (time.time() - self.close_decision_start_time) / 60
                    logger.info(f"⏸️ 继续持仓：价差不利且时间未到 (已等待{wait_elapsed:.1f}分钟，还需{time_remaining/60:.1f}分钟)")

                    await asyncio.sleep(self.sleep_time)

            except Exception as e:
                logger.error(f"❌ 平仓策略执行失败: {e}")
                # 出错时也触发超时保护
                if self._is_close_timeout():
                    wait_minutes = (time.time() - self.close_decision_start_time) / 60
                    reason = f"⚠️ 策略执行失败且超时：已等待{wait_minutes:.1f}分钟，强制平仓"
                    logger.warning(reason)
                    
                    # 记录执行上下文
                    self._set_close_execution_context(
                        reason=reason,
                        price_data={},  # 异常情况下没有完整价格数据
                        side='error_timeout',
                        trigger=DecisionTrigger.ERROR_TIMEOUT,
                        next_open_minutes=random.uniform(*self.open_wait_range)
                    )
                    
                    self._reset_close_decision_time()
                    return  # 超时保护，退出等待
                # 出错时等待后重试
                await asyncio.sleep(self.sleep_time)
        
        # 如果stop_flag被设置，抛出异常通知调用者停止
        raise asyncio.CancelledError("平仓等待被中断")
    
    async def _check_liquidation_risk(self, hedge_bot, current_sample):
        """检查清算风险：当盘口价格接近任意一边清算价格的80%时触发风险控制"""
        try:
            # 并行获取双边清算价格
            liquidation_results = await asyncio.gather(
                hedge_bot.primary_client.get_ticker_position_liquidation_price(),
                hedge_bot.lighter.get_ticker_position_liquidation_price(),
                return_exceptions=True
            )
            
            primary_liquidation = liquidation_results[0]
            lighter_liquidation = liquidation_results[1]
            
            # 检查是否有获取清算价格失败的情况
            if isinstance(primary_liquidation, Exception):
                hedge_bot.logger.warning(f"⚠️ 获取Primary清算价格失败: {primary_liquidation}")
                primary_liquidation = None
            
            if isinstance(lighter_liquidation, Exception):
                hedge_bot.logger.warning(f"⚠️ 获取Lighter清算价格失败: {lighter_liquidation}")
                lighter_liquidation = None
            
            # 如果两边的清算价格都获取失败，则跳过风险检查
            if primary_liquidation is None and lighter_liquidation is None:
                hedge_bot.logger.warning("⚠️ 无法获取任何清算价格，跳过风险控制检查")
                return False
            
            # 当前盘口价格
            current_primary_mid = current_sample['primary_mid']
            current_lighter_mid = current_sample['lighter_mid']
            
            # Primary风险检查
            if primary_liquidation is not None:
                primary_risk = self._check_single_exchange_risk(
                    "Primary", current_primary_mid, primary_liquidation, hedge_bot.logger
                )
                if primary_risk:
                    return True
            
            # Lighter风险检查  
            if lighter_liquidation is not None:
                lighter_risk = self._check_single_exchange_risk(
                    "Lighter", current_lighter_mid, lighter_liquidation, hedge_bot.logger
                )
                if lighter_risk:
                    return True
            
            return False
            
        except Exception as e:
            hedge_bot.logger.error(f"❌ 风险控制检查失败: {e}")
            return False  # 检查失败时保守处理，不触发风险控制
    
    def _check_single_exchange_risk(self, exchange_name, current_price, liquidation_price, logger):
        """检查单个交易所的清算风险"""
        if liquidation_price is None or liquidation_price <= 0:
            return False
        
        # 计算当前价格与清算价格的距离比例
        price_distance_ratio = abs(current_price - liquidation_price) / liquidation_price
        
        if price_distance_ratio <= self.risk_threshold:
            logger.warning(
                f"🚨 {exchange_name}清算风险警告: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, "
                f"距离比例{price_distance_ratio:.2%} <= {self.risk_threshold:.2%}"
            )
            return True
        else:
            logger.debug(
                f"✅ {exchange_name}清算风险正常: "
                f"当前价格{current_price:.6f}, 清算价格{liquidation_price:.6f}, "
                f"距离比例{price_distance_ratio:.2%}"
            )
            return False
    
    def _is_close_timeout(self) -> bool:
        """检查是否超过最大平仓等待时间"""
        if self.close_decision_start_time is None:
            return False
        elapsed_minutes = (time.time() - self.close_decision_start_time) / 60
        return elapsed_minutes >= self.max_close_wait_minutes
    
    def _reset_close_decision_time(self):
        """重置平仓决策时间"""
        self.close_decision_start_time = None

