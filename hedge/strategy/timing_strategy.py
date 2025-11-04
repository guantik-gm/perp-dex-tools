import os
import time
import random
from typing import List

from hedge.strategy.hedge_strategy import HedgeStrategy, HedgeStrategyResult

class TimingStrategy(HedgeStrategy):
    """时间策略 - 中等优先级，集成TimingController功能"""
    
    def __init__(self, priority=20):
        super().__init__(open_priority=priority, close_priority=priority)
        self.open_wait_range = (0.5, 1.5)
        self.close_wait_range = (10, 20)
        # self.close_wait_range = (2, 3)
        
        # 时间控制状态
        self.next_open_time = self.schedule_next_open(*self.open_wait_range)
        self.next_close_time = self.schedule_next_close(*self.close_wait_range)
        
        # 决策时间记录
        self.open_decision_start_time = time.time()
        self.close_decision_start_time = time.time()
        
        self.logger = None
        
        # 内部管理开仓时间
        self.position_open_time = None
   
    async def can_open(self, hedge_bot):
        """检查是否可以基于时间开仓"""
        self.logger = hedge_bot.logger
        self.open_decision_start_time = time.time()
        self.data['side'] = 'open'
        
        try:
            # 检查时间条件
            reason = f"[时间策略] 未触发, 下次开仓时间: {self._format_time(self.next_open_time)}"
            strategy_result = HedgeStrategyResult.REJECT
            if self.can_open_by_time():
                reason = f"⏰ 时间维度满足：到达预定开仓时间 {self._format_time(self.next_open_time)}"
                strategy_result = HedgeStrategyResult.PASS
                
                # 获取当前价格确定开仓方向
                current_sample = await self._get_current_price_data(hedge_bot)
                self.data['price_data'] = current_sample
                if current_sample['primary_mid'] < current_sample['lighter_mid']:
                    self.open_side = 'buy'
                else:
                    self.open_side = 'sell'
                
                self._reset_open_decision_time()
            self._set_strategy_context(strategy_result, reason)
            
        except Exception as e:
            self._set_strategy_context(result=HedgeStrategyResult.PASS, reason=f"❌ 时间策略开仓检查失败: {e}")
    
    async def can_close(self, hedge_bot):
        """检查是否可以基于时间平仓"""
        self.logger = hedge_bot.logger
        self.close_decision_start_time = time.time()
        self.data['side'] = 'close'
        
        try:
            # 检查时间条件
            reason = f"[时间策略] 未触发, 下次平仓时间: {self._format_time(self.next_close_time)}"
            strategy_result = HedgeStrategyResult.REJECT
            if self.can_close_by_time():
                reason = f"⏰ 时间维度满足：到达预定平仓时间: {self._format_time(self.next_close_time)}"
                strategy_result = HedgeStrategyResult.PASS

                # 获取当前价格数据
                current_sample = await self._get_current_price_data(hedge_bot)
                self.data['price_data'] = current_sample
                
                self._reset_close_decision_time()
            self._set_strategy_context(strategy_result, reason)
            
        except Exception as e:
            self._set_strategy_context(result=HedgeStrategyResult.PASS, reason=f"❌ 时间策略平仓检查失败: {e}")
    
    def _get_msgs(self) -> List[str]:
        if self.data['side'] == 'open':
            msgs = [
                f"📅 到达开仓时间: {self._format_time(self.next_open_time)}",
                f"📅 预计平仓: {self._format_time(self.next_close_time)}",
                f"📅 持仓时间周期: {self.close_wait_range[0]} - {self.close_wait_range[1]} 分钟",
            ]
        else:  # close
            msgs = [
                f"📅 到达平仓时间: {self._format_time(self.next_close_time)}",
                f"📅 下次开仓: {self._format_time(self.next_open_time)}",
                f"📅 开仓时间周期: {self.open_wait_range[0]} - {self.open_wait_range[1]} 分钟",
            ]
            
            # 添加本次开仓时间和持仓时长信息（仅在平仓时显示）
            if self.position_open_time:
                try:
                    current_time = time.time()
                    holding_duration = current_time - self.position_open_time
                    holding_minutes = holding_duration / 60
                    
                    msgs.extend([
                        f"📅 本次开仓时间: {self._format_time(self.position_open_time)}",
                        f"⏱️ 本次持仓时长: {holding_minutes:.1f} 分钟"
                    ])
                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"获取开仓时间信息失败: {e}")
                    
        return msgs
    
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
        if hasattr(self, "logger") and self.logger:
            self.logger.info(f"⏰ 调度平仓时间：{wait_minutes:.1f}分钟后, 时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.next_close_time))}")
        return self.next_close_time
    
    def schedule_next_open(self, min_minutes: int, max_minutes: int):
        """调度下次开仓时间"""
        wait_minutes = random.uniform(min_minutes, max_minutes)
        self.next_open_time = time.time() + (wait_minutes * 60)
        if hasattr(self, "logger") and self.logger:
            self.logger.info(f"⏰ 调度开仓时间：{wait_minutes:.1f}分钟后, 时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.next_open_time))}")
        return self.next_open_time
    
    def _reset_open_decision_time(self):
        """重置开仓决策时间"""
        self.open_decision_start_time = None
    
    def _reset_close_decision_time(self):
        """重置平仓决策时间"""
        self.close_decision_start_time = None
        
    def after_open_hedge_position(self, hedge_bot):
        """任何策略开仓后，设置时间策略的平仓时间"""
        # 记录开仓时间
        self.position_open_time = time.time()
        self.schedule_next_close(*self.close_wait_range)
        if hasattr(self, "logger") and self.logger:
            self.logger.info(f"⏰ [时间策略] 开仓后统一设置平仓时间，记录开仓时间: {self._format_time(self.position_open_time)}")

    def after_close_hedge_position(self, hedge_bot):
        """任何策略平仓后，设置时间策略的下次开仓时间"""
        # 清除开仓时间记录
        self.position_open_time = None
        self.schedule_next_open(*self.open_wait_range)
        if hasattr(self, "logger") and self.logger:
            self.logger.info(f"⏰ [时间策略] 平仓后统一设置下次开仓时间，清除开仓时间记录")