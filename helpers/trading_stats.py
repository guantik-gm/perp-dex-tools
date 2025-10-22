"""
Trading statistics tracker for boost mode monitoring.
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict
from collections import deque


@dataclass
class TradingStats:
    """
    Comprehensive trading statistics for boost mode.
    Tracks all metrics for reporting and monitoring.
    """
    
    # Initialization
    start_time: float = field(default_factory=time.time)

    # Basic trade statistics
    cumulative_trade_count: int = 0
    cumulative_base_volume: Decimal = field(default_factory=lambda: Decimal('0'))
    cumulative_quote_volume: Decimal = field(default_factory=lambda: Decimal('0'))

    # IOC optimization statistics
    ioc_attempt_count: int = 0
    ioc_full_fill_count: int = 0
    ioc_partial_fill_count: int = 0
    ioc_failed_count: int = 0
    market_fallback_count: int = 0
    total_ioc_fill_size: Decimal = field(default_factory=lambda: Decimal('0'))
    total_ioc_order_size: Decimal = field(default_factory=lambda: Decimal('0'))

    # Price tracking
    price_samples: deque = field(default_factory=lambda: deque(maxlen=1000))

    # Fee tracking (actual data from exchange)
    actual_total_fee: Decimal = field(default_factory=lambda: Decimal('0'))
    last_fee_query_time: float = 0
    
    def get_runtime_seconds(self) -> float:
        """Get total runtime in seconds"""
        return time.time() - self.start_time
    
    def get_runtime_formatted(self) -> str:
        """Get formatted runtime string"""
        seconds = int(self.get_runtime_seconds())
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        elif minutes > 0:
            return f"{minutes}分钟{secs}秒"
        else:
            return f"{secs}秒"
    
    def record_trade(self, size: Decimal, price: Decimal):
        """Record a completed trade"""
        self.cumulative_trade_count += 1
        self.cumulative_base_volume += size
        self.cumulative_quote_volume += size * price
    
    def record_ioc_attempt(self, order_size: Decimal):
        """Record an IOC order attempt"""
        self.ioc_attempt_count += 1
        self.total_ioc_order_size += order_size
    
    def record_ioc_result(self, filled_size: Decimal, total_size: Decimal, fell_back_to_market: bool = False):
        """
        Record IOC order result.
        
        Args:
            filled_size: Size filled by IOC
            total_size: Total order size
            fell_back_to_market: Whether market order was used for remainder
        """
        self.total_ioc_fill_size += filled_size
        
        if filled_size >= total_size:
            # Fully filled by IOC
            self.ioc_full_fill_count += 1
        elif filled_size > 0:
            # Partially filled by IOC
            self.ioc_partial_fill_count += 1
            if fell_back_to_market:
                self.market_fallback_count += 1
        else:
            # IOC failed completely
            self.ioc_failed_count += 1
            if fell_back_to_market:
                self.market_fallback_count += 1
    
    def record_price_sample(self, bid: Decimal, ask: Decimal):
        """Record a price sample for spread tracking"""
        self.price_samples.append({
            'timestamp': time.time(),
            'bid': bid,
            'ask': ask,
            'mid': (bid + ask) / 2,
            'spread': ask - bid
        })

    def get_ioc_success_rate(self) -> float:
        """Get IOC success rate (full + partial fills)"""
        if self.ioc_attempt_count == 0:
            return 0.0
        successful = self.ioc_full_fill_count + self.ioc_partial_fill_count
        return (successful / self.ioc_attempt_count) * 100
    
    def get_ioc_full_fill_rate(self) -> float:
        """Get IOC full fill rate"""
        if self.ioc_attempt_count == 0:
            return 0.0
        return (self.ioc_full_fill_count / self.ioc_attempt_count) * 100
    
    def get_ioc_avg_fill_rate(self) -> float:
        """Get average IOC fill rate (% of order size filled)"""
        if self.total_ioc_order_size == 0:
            return 0.0
        return float((self.total_ioc_fill_size / self.total_ioc_order_size) * 100)

    def get_trades_per_hour(self) -> float:
        """Get average trades per hour"""
        runtime_hours = self.get_runtime_seconds() / 3600
        if runtime_hours == 0:
            return 0.0
        return self.cumulative_trade_count / runtime_hours
    
    def get_avg_trade_size(self) -> Decimal:
        """Get average trade size"""
        if self.cumulative_trade_count == 0:
            return Decimal('0')
        return self.cumulative_base_volume / self.cumulative_trade_count

    def record_actual_fee(self, fee: Decimal):
        """Record actual fee from exchange API"""
        self.actual_total_fee += fee
    
    def get_wear_rate(self, total_quote_volume: Decimal) -> Decimal:
        """
        Calculate wear rate (磨损率) using actual fee data.
        
        Args:
            total_quote_volume: Total trading volume in quote currency
            
        Returns:
            Wear rate as percentage (e.g., 0.035 means 0.035% = 万3.5)
        """
        if total_quote_volume == 0:
            return Decimal('0')
        return (self.actual_total_fee / total_quote_volume) * 100
    
    def get_avg_fee_per_trade(self) -> Decimal:
        """Get average fee per trade"""
        if self.cumulative_trade_count == 0:
            return Decimal('0')
        return self.actual_total_fee / self.cumulative_trade_count
