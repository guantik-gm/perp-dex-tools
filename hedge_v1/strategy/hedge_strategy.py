from abc import ABC, abstractmethod
import time
import asyncio
import random
from typing import Dict, Any, List
from decimal import Decimal
from enum import Enum

class HedgeStrategyResult(Enum):
    # 立即触发，不再判断下一个策略
    TRIGGER = "trigger"
    # 检查通过，继续判断下一个策略
    PASS = "pass"
    # 策略拒绝，终止流程
    REJECT = "reject"

class HedgeStrategy(ABC):
    """极简策略基类 - 提供统一的等待逻辑"""
    
    def __init__(self, open_priority: int = 0, close_priority: int = 0):
        """
        初始化策略基类
        
        Args:
            open_priority: 开仓决策优先级，值越大优先级越高
            close_priority: 平仓决策优先级，值越大优先级越高
        """
        self.open_priority = open_priority
        self.close_priority = close_priority
        self.name = self.__class__.__name__
        # 开仓方向
        self.side: str = None
        self.result: HedgeStrategyResult = None
        self.reason: str = None
        self.data: Dict[str, Any] = {}

        if not self._verify_env():
            raise Exception(f"env not found, please check your env file")
   
    def _verify_env(self) -> bool:
        """验证策略所需的环境变量是否齐全"""
        return True

    @abstractmethod
    async def can_open(self, hedge_bot):
        """检查是否可以开仓 - 多个策略的组合将使用 and 逻辑判断"""
        pass

    @abstractmethod
    async def can_close(self, hedge_bot):
        """检查是否可以平仓 - 多个策略的组合将使用 and 逻辑判断"""
        pass
    
    def order_place_timeout_retry(self):
        """主交易所订单执行超时是否重试""" 
        return False

    def after_open_hedge_position(self, hedge_bot):
        """完整的对冲仓位开仓后触发，可以拿到开仓后的价格信息"""
        pass
    
    def after_close_hedge_position(self, hedge_bot):
        """完整的对冲仓位平仓后触发，可以拿到平仓后的价格信息"""
        pass
    
    def get_msgs(self):
        msgs = [f"策略原因: {self.reason or '未设置'}"]
        try:
            msgs.extend(self._get_msgs())
        except Exception as e:
            if hasattr(self, "logger") and self.logger is not None:
                self.logger.error(f"get msgs error: {e}")
            return [f"{self.name} get msgs error: {e}"]
        return msgs
    
    def _get_msgs(self) -> List[str]:
        return []
    
    async def _get_current_price_data(self, hedge_bot):
        """获取当前价格数据的简化版本"""
        retry_times = 10
        current = 0
        while current < retry_times:
            try:
                # 获取双边价格
                result = await asyncio.gather(
                    hedge_bot.fetch_primary_bbo_prices(),
                    hedge_bot.lighter.fetch_bbo_prices()
                )
                primary_bid, primary_ask = result[0]
                lighter_bid, lighter_ask = result[1]

                primary_mid = (Decimal(str(primary_bid)) + Decimal(str(primary_ask))) / Decimal('2')
                lighter_mid = (Decimal(str(lighter_bid)) + Decimal(str(lighter_ask))) / Decimal('2')
                spread = float(abs(primary_mid - lighter_mid))

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
                self.logger.error(f"获取价格数据失败: {e}, 等待 10s 后重试, 重试次数: {current}/{retry_times}")
                current += 1
                await asyncio.sleep(10)
        return {}
        
    async def do_price_sampling(self, hedge_bot, sample_count_range=(7, 11)):
        """通用的价格采样方法，包含价格变化检测"""
        sample_count = random.randint(*sample_count_range)
        if hasattr(self, 'logger') and self.logger:
            self.logger.info(f"📊 开始价格采样，计划采样 {sample_count} 次")
        
        samples = []
        unchanged_count = 0  # 连续未变化次数
        
        for i in range(sample_count):
            try:
                sample = await self._get_current_price_data(hedge_bot)
                
                # 检查价格是否与前一次相同
                if samples and len(samples) > 0:
                    last_sample = samples[-1]
                    if (sample['primary_mid'] == last_sample['primary_mid'] and 
                        sample['lighter_mid'] == last_sample['lighter_mid']):
                        unchanged_count += 1
                        if hasattr(self, 'logger') and self.logger:
                            self.logger.warning(f"⚠️ 采样 {i+1}: 价格未变化 (连续{unchanged_count}次)")
                    else:
                        unchanged_count = 0
                else:
                    unchanged_count = 0
                
                samples.append({
                    'timestamp': time.time(),
                    'primary_mid': sample['primary_mid'],
                    'lighter_mid': sample['lighter_mid'],
                    'spread': sample['spread']
                })
                
                if hasattr(self, 'logger') and self.logger:
                    self.logger.info(f"📈 采样 {i+1}/{sample_count}: "
                                   f"Primary={sample['primary_mid']:.6f}, "
                                   f"Lighter={sample['lighter_mid']:.6f}, "
                                   f"Spread={sample['spread']:.6f}")
                
                # 采样间隔
                if i < sample_count - 1:
                    await asyncio.sleep(1)
                    
            except Exception as e:
                if hasattr(self, 'logger') and self.logger:
                    self.logger.error(f"❌ 价格采样失败 {i+1}/{sample_count}: {e}")
                continue
        
        # 检查采样质量并给出提示
        if len(samples) > 1:
            unique_primary = len(set(s['primary_mid'] for s in samples))
            unique_lighter = len(set(s['lighter_mid'] for s in samples))
            if unique_primary == 1 and unique_lighter == 1:
                if hasattr(self, 'logger') and self.logger:
                    self.logger.warning(f"🚨 采样完成但所有价格相同，建议检查WebSocket连接状态")
        
        if hasattr(self, 'logger') and self.logger:
            self.logger.info(f"✅ 价格采样完成，成功采样 {len(samples)}/{sample_count} 次")
        
        return samples
    
    def _format_time(self, timestamp: float) -> str:
        """格式化时间戳为可读字符串"""
        if timestamp is None:
            return "未设置"
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))
    
    def _set_strategy_context(self, result, reason):
        self.result = result
        self.reason = reason




