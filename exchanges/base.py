"""
Base exchange client interface.
All exchange implementations should inherit from this class.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple, Type, Union
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import time
import random
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt, wait_exponential


def query_retry(
    default_return: Any = None,
    exception_type: Union[Type[Exception], Tuple[Type[Exception], ...]] = (Exception,),
    max_attempts: int = 5,
    min_wait: float = 1,
    max_wait: float = 10,
    reraise: bool = False
):
    def retry_error_callback(retry_state: RetryCallState):
        exception_str = str(retry_state.outcome.exception())
        
        # 检查是否为 429 错误
        if "429" in exception_str or "too many requests" in exception_str.lower():
            # 为 429 错误使用更长的延迟
            base_delay = 2.0  # 基础延迟2秒
            attempt = retry_state.attempt_number
            exponential_delay = base_delay * (2 ** attempt)  # 指数退避
            jitter = random.uniform(0.5, 1.5)  # 添加随机抖动
            total_delay = min(exponential_delay * jitter, 15.0)  # 最大延迟15秒
            
            print(f"Operation: [{retry_state.fn.__name__}] hit rate limit (429), "
                  f"attempt {attempt}/{max_attempts}, waiting {total_delay:.1f}s before retry")
            time.sleep(total_delay)
        else:
            print(f"Operation: [{retry_state.fn.__name__}] failed after {retry_state.attempt_number} retries, "
                  f"exception: {exception_str}")
        
        return default_return

    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exception_type),
        retry_error_callback=retry_error_callback,
        reraise=reraise
    )


@dataclass
class OrderResult:
    """Standardized order result structure."""
    success: bool
    order_id: Optional[str] = None
    side: Optional[str] = None
    size: Optional[Decimal] = None
    price: Optional[Decimal] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    filled_size: Optional[Decimal] = None


@dataclass
class OrderInfo:
    """Standardized order information structure."""
    order_id: str
    side: str
    size: Decimal
    price: Decimal
    status: str
    filled_size: Decimal = 0.0
    remaining_size: Decimal = 0.0
    cancel_reason: str = ''


class BaseExchangeClient(ABC):
    """Base class for all exchange clients."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize the exchange client with configuration."""
        self.config = config
        self._validate_config()

    def round_to_tick(self, price) -> Decimal:
        price = Decimal(price)

        tick = self.config.tick_size
        # quantize forces price to be a multiple of tick
        return price.quantize(tick, rounding=ROUND_HALF_UP)

    @abstractmethod
    def _validate_config(self) -> None:
        """Validate the exchange-specific configuration."""
        pass

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the exchange (WebSocket, etc.)."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the exchange."""
        pass

    @abstractmethod
    async def place_open_order(self, contract_id: str, quantity: Decimal, direction: str) -> OrderResult:
        """Place an open order."""
        pass

    @abstractmethod
    async def place_close_order(self, contract_id: str, quantity: Decimal, price: Decimal, side: str) -> OrderResult:
        """Place a close order."""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order."""
        pass

    @abstractmethod
    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information."""
        pass

    @abstractmethod
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract."""
        pass

    @abstractmethod
    async def get_account_positions(self) -> Decimal:
        """Get account positions."""
        pass

    @abstractmethod
    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        pass

    @abstractmethod
    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        pass