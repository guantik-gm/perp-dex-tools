"""
订单状态判断辅助函数

目的：兼容不同交易所的原始状态，提供统一的判断逻辑。
设计：不改变原有状态映射，在使用处提供兼容判断。
"""

from typing import Optional


def is_order_filled(status: str, cancel_reason: Optional[str] = None) -> bool:
    """
    判断订单是否完全成交
    
    兼容状态：
    - 'FILLED' (标准状态)
    - 'CLOSED' without cancel_reason (Paradex)
    
    Args:
        status: 订单状态
        cancel_reason: 取消原因（可选）
        
    Returns:
        bool: 是否完全成交
    """
    if status == 'FILLED':
        return True
    # Paradex: CLOSED without cancel_reason means FILLED
    if status == 'CLOSED' and not cancel_reason:
        return True
    return False


def is_order_canceled(status: str, cancel_reason: Optional[str] = None) -> bool:
    """
    判断订单是否被取消/拒绝
    
    兼容状态：
    - 'CANCELED' (标准状态，美式拼写)
    - 'CANCELLED' (GRVT，英式拼写)
    - 'REJECTED' (GRVT)
    - 'CLOSED' with cancel_reason (Paradex)
    
    Args:
        status: 订单状态
        cancel_reason: 取消原因（可选）
        
    Returns:
        bool: 是否被取消/拒绝
    """
    # 标准取消状态
    if status in ['CANCELED', 'CANCELLED', 'REJECTED']:
        return True
    # Paradex: CLOSED with cancel_reason means CANCELED
    if status == 'CLOSED' and cancel_reason:
        return True
    return False


def is_order_open(status: str) -> bool:
    """
    判断订单是否处于打开状态
    
    兼容状态：
    - 'OPEN' (标准状态)
    - 'NEW' (Paradex)
    - 'PENDING' (GRVT)
    
    Args:
        status: 订单状态
        
    Returns:
        bool: 是否打开
    """
    return status in ['OPEN', 'NEW', 'PENDING']


def is_order_partially_filled(status: str, filled_size: float = 0) -> bool:
    """
    判断订单是否部分成交
    
    Args:
        status: 订单状态
        filled_size: 已成交数量
        
    Returns:
        bool: 是否部分成交
    """
    if status == 'PARTIALLY_FILLED':
        return True
    # OPEN with filled_size > 0 means partially filled
    if status == 'OPEN' and filled_size > 0:
        return True
    return False


def should_retry_post_only(status: str, cancel_reason: Optional[str] = None) -> bool:
    """
    判断POST_ONLY订单是否应该重试
    
    POST_ONLY_WOULD_CROSS: 订单价格会与市场价格交叉，需要重新定价
    
    Args:
        status: 订单状态
        cancel_reason: 取消原因
        
    Returns:
        bool: 是否应该重试
    """
    if cancel_reason == 'POST_ONLY_WOULD_CROSS':
        return True
    # GRVT: REJECTED通常意味着价格问题，应该重试
    if status == 'REJECTED':
        return True
    return False
