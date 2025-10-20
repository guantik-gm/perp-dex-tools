# 交易所通讯架构详解

## 概述

本项目使用 **REST API** 和 **WebSocket** 两种技术与各个交易所进行通讯，实现了双向、实时的交易功能。

---

## 技术栈架构

### 整体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      Trading Bot                            │
│                  (hedge_mode.py / runbot.py)               │
└────────────────────┬────────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        │   BaseExchangeClient    │  (统一抽象接口)
        │   (exchanges/base.py)   │
        └────────────┬────────────┘
                     │
      ┌──────────────┼──────────────┐
      │              │              │
┌─────▼──────┐ ┌────▼─────┐ ┌─────▼──────┐
│  Backpack  │ │  GRVT    │ │  Paradex   │  ... (8个交易所)
│  Client    │ │  Client  │ │  Client    │
└─────┬──────┘ └────┬─────┘ └─────┬──────┘
      │              │              │
   ┌──┴──┐        ┌──┴──┐        ┌──┴──┐
   │REST │        │REST │        │REST │  (查询/下单/取消)
   │  +  │        │  +  │        │  +  │
   │  WS │        │  WS │        │  WS │  (订单更新/实时数据)
   └──┬──┘        └──┬──┘        └──┬──┘
      │              │              │
   ┌──▼────────────────────────────▼──┐
   │     各交易所服务器                │
   └───────────────────────────────────┘
```

---

## REST API - 作用和使用场景

### 主要职责

REST API 负责 **主动请求和命令执行**，是机器人与交易所之间的"请求-响应"通道。

### 核心功能

#### 1. **下单操作** (Place Order)
```python
# 示例：Backpack 下限价单
order_result = self.account_client.execute_order(
    order_type=OrderTypeEnum.Limit,
    side='Bid' if direction == 'buy' else 'Ask',
    price=str(order_price),
    quantity=str(quantity),
    symbol=contract_id,
    post_only=True  # 仅做市商订单
)
```

**使用场景：**
- 下开仓订单 (place_open_order)
- 下平仓订单 (place_close_order)
- 下市价单/限价单

#### 2. **取消订单** (Cancel Order)
```python
# 示例：取消订单
cancel_result = self.account_client.cancel_order(
    order_id=order_id,
    symbol=contract_id
)
```

**使用场景：**
- 超时订单取消
- 策略调整时撤单
- 异常情况下的风控撤单

#### 3. **查询市场数据** (Market Data)
```python
# 获取订单簿深度
order_book = self.public_client.get_depth(contract_id)

# 获取合约信息
markets = self.public_client.get_markets()
```

**使用场景：**
- 获取当前买卖盘价格 (BBO - Best Bid/Offer)
- 查询合约信息（tick size, 最小数量等）
- 获取历史K线数据

#### 4. **查询账户信息** (Account Info)
```python
# 获取持仓
positions = self.account_client.get_open_positions()

# 获取活跃订单
active_orders = self.account_client.get_open_orders(symbol=contract_id)

# 查询订单状态
order_info = self.account_client.get_open_order(order_id=order_id)
```

**使用场景：**
- 仓位查询和验证
- 风控检查
- 订单状态轮询（备用方案）

---

## WebSocket - 作用和使用场景

### 主要职责

WebSocket 负责 **实时推送和事件监听**，是交易所向机器人的"主动通知"通道。

### 核心功能

#### 1. **订单状态实时更新** (Order Updates) ⭐ 最重要
```python
# 订阅订单更新
subscribe_message = {
    "method": "SUBSCRIBE",
    "params": [f"account.orderUpdate.{symbol}"],
    "signature": [...]
}

# 接收订单更新
async def _handle_order_update(self, order_data):
    status = order_data.get('status')  # FILLED, CANCELED, OPEN...
    if status == 'FILLED':
        # 触发对冲逻辑
        self.trigger_hedge_order()
```

**使用场景：**
- **对冲交易核心逻辑**：Maker 订单成交后立即触发 Hedge 订单
- 订单状态变化监听（NEW → OPEN → FILLED）
- 部分成交通知

**为什么不用 REST 轮询？**
- ❌ REST 轮询延迟高（100-500ms）
- ✅ WebSocket 实时推送（<10ms）
- ✅ 减少 API 请求次数（避免限流）

#### 2. **订单簿实时更新** (Order Book)
```python
# Lighter 订单簿更新
{
    "type": "update/order_book",
    "order_book": {
        "offset": 12345,
        "bids": [["50000.5", "0.1"], ...],
        "asks": [["50001.0", "0.2"], ...]
    }
}
```

**使用场景：**
- 实时获取最佳买卖价
- 快速价格发现
- 高频策略（虽然本项目不是高频）

#### 3. **账户余额和仓位更新** (Account Updates)
```python
# 实时仓位变化
{
    "type": "update/positions",
    "positions": [{
        "market": "BTC-USD-PERP",
        "size": "0.5",
        "entry_price": "50000"
    }]
}
```

**使用场景：**
- 仓位变化监控
- 风控实时检查
- 保证金不足告警

---

## 典型交互流程

### 场景 1：对冲交易完整流程

```
时间轴               Maker 端 (Backpack)              Hedge 端 (Lighter)
─────────────────────────────────────────────────────────────────────
T0: 策略启动
                ┌─ REST: 获取市场信息 ─┐
                │  (BBO, 合约信息)     │
                └──────────────────────┘
                          │
                          ▼
                ┌─ WebSocket: 连接并订阅 ─┐
                │  订阅: account.orderUpdate │
                └──────────────────────────┘

T1: 下 Maker 单
                ┌─ REST: place_open_order ─┐
                │  POST /orders            │
                │  {side: buy, qty: 0.1,   │
                │   post_only: true}       │
                └──────────────────────────┘
                          │
                          ▼
                    订单状态: NEW → OPEN

T2: 等待成交 (WebSocket 监听中...)
                          ▼
                ┌─ WebSocket: 订单更新推送 ─┐
                │  {status: "FILLED",       │
                │   filled_size: "0.1",     │
                │   price: "50000"}         │
                └──────────────────────────┘
                          │
                          ▼
                    触发 Hedge 逻辑!
                                          ┌─ REST: place_hedge_order ─┐
                                          │  立即下市价单/限价单       │
                                          │  (对冲 0.1 BTC)          │
                                          └──────────────────────────┘
                                                      │
                                                      ▼
                                          ┌─ WebSocket: 订单更新 ─┐
                                          │  {status: "FILLED"}   │
                                          └───────────────────────┘
                                                      │
                                                      ▼
                                               对冲完成 ✅

T3: 仓位验证
                ┌─ REST: 获取仓位 ─┐      ┌─ REST: 获取仓位 ─┐
                │  Backpack: +0.1  │      │  Lighter: -0.1   │
                └──────────────────┘      └──────────────────┘
                          │                         │
                          └──────────┬──────────────┘
                                     ▼
                              仓位平衡检查 ✅
```

### 场景 2：订单超时处理

```
T0: 下单
    REST: place_open_order → order_id="12345"

T1-T10: 等待成交 (WebSocket 监听...)
    10秒过去，订单未成交

T11: 超时检测
    if time.time() - order_start_time > 10:
        REST: cancel_order(order_id="12345")  ← 主动取消
        
T12: 重新下单
    REST: place_open_order (重新计算价格)
```

---

## 各交易所技术实现对比

| 交易所 | REST SDK | WebSocket 实现 | 认证方式 | 特点 |
|--------|----------|---------------|---------|------|
| **Backpack** | ✅ 官方 bpx-py | ✅ 自定义 (ED25519 签名) | Public/Secret Key | 订单更新快 |
| **Lighter** | ✅ 官方 lighter-sdk | ✅ 自定义 (JWT Token) | API Key Private Key | 订单簿推送详细 |
| **GRVT** | ✅ 官方 grvt-pysdk | ✅ 官方 GrvtCcxtWS | Trading Account ID | 完整 SDK 支持 |
| **Paradex** | ✅ 官方 paradex-py | ✅ 官方 | L2 Private Key | Starknet 链上 |
| **Extended** | ✅ x10-python | ✅ 自定义 | Stark Key | 需要签名计算 |
| **Apex** | ✅ apexomni | ✅ 官方 | Omni Key Seed | 多链支持 |
| **Aster** | ❌ 自定义实现 | ✅ 自定义 | API Key/Secret | Binance 风格 |
| **EdgeX** | ✅ edgex-sdk | ✅ 官方 | Stark Key | Post-only 支持 |

---

## WebSocket 管理器模式

### 通用 WebSocket 管理器结构

```python
class ExchangeWebSocketManager:
    """通用 WebSocket 管理器"""
    
    def __init__(self, credentials, callback):
        self.ws_url = "wss://..."
        self.callback = callback
        self.running = False
    
    async def connect(self):
        """连接到 WebSocket"""
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    # 1. 发送认证消息
                    await self._authenticate(ws)
                    
                    # 2. 订阅频道
                    await self._subscribe(ws)
                    
                    # 3. 监听消息
                    async for message in ws:
                        await self._handle_message(message)
                        
            except Exception as e:
                # 自动重连
                await asyncio.sleep(2)
    
    async def _authenticate(self, ws):
        """认证（如果需要）"""
        auth_msg = self._generate_auth_message()
        await ws.send(json.dumps(auth_msg))
    
    async def _subscribe(self, ws):
        """订阅频道"""
        subscribe_msg = {
            "method": "SUBSCRIBE",
            "params": ["orderUpdate", "positions"]
        }
        await ws.send(json.dumps(subscribe_msg))
    
    async def _handle_message(self, message):
        """处理消息"""
        data = json.loads(message)
        
        if data.get('type') == 'orderUpdate':
            await self.callback(data)
        elif data.get('type') == 'ping':
            await self._send_pong()
```

### 关键处理逻辑

#### 1. **心跳保持** (Heartbeat)
```python
# 大多数交易所需要定期发送 Ping/Pong
if data.get('type') == 'ping':
    await ws.send(json.dumps({"type": "pong"}))
```

#### 2. **自动重连** (Reconnection)
```python
while True:
    try:
        # WebSocket 逻辑
        async with websockets.connect(url) as ws:
            ...
    except Exception as e:
        self.logger.error(f"Connection lost: {e}")
        await asyncio.sleep(2)  # 等待 2 秒重连
```

#### 3. **订单簿增量更新** (Incremental Updates)
```python
# Lighter 的订单簿管理
if data.get('type') == 'subscribed/order_book':
    # 完整快照
    self.order_book['bids'] = data['bids']
    self.order_book['asks'] = data['asks']
elif data.get('type') == 'update/order_book':
    # 增量更新
    for bid in data['bids']:
        price, size = bid
        if size == 0:
            del self.order_book['bids'][price]
        else:
            self.order_book['bids'][price] = size
```

---

## 关键技术细节

### 1. 异步编程模型

```python
# 所有网络操作都是异步的
async def place_order(...):
    # REST 请求 - 异步
    result = await self.client.execute_order(...)
    
    # WebSocket 监听 - 异步
    async for message in websocket:
        await self._handle_message(message)
```

**为什么用 async/await？**
- ✅ 高并发：同时管理多个 WebSocket 连接
- ✅ 非阻塞：等待订单成交时不阻塞其他操作
- ✅ 性能：I/O 密集型任务效率高

### 2. 重试机制

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(5),      # 最多重试 5 次
    wait=wait_exponential(min=1, max=10),  # 指数退避
    retry=retry_if_exception_type(Exception)
)
async def place_order_with_retry(...):
    return await self.client.place_order(...)
```

**使用场景：**
- 网络抖动
- API 限流 (429 错误)
- 交易所服务器临时不可用

### 3. 订单状态同步

```python
# 方案 1: WebSocket 主动推送（首选）
async def _handle_order_update(self, data):
    self.order_status = data['status']
    if data['status'] == 'FILLED':
        self.trigger_hedge()

# 方案 2: REST 轮询（备用）
async def wait_for_fill(self, order_id, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        order_info = await self.get_order_info(order_id)
        if order_info.status == 'FILLED':
            return True
        await asyncio.sleep(0.5)
    return False
```

---

## 性能优化技巧

### 1. WebSocket 连接池
```python
# 单个 WebSocket 连接订阅多个频道
await ws.send(json.dumps({
    "method": "SUBSCRIBE",
    "params": [
        "account.orderUpdate.BTC-USD",
        "account.orderUpdate.ETH-USD",
        "account.positions"
    ]
}))
```

### 2. 并发请求
```python
# 同时查询多个交易所
async def get_all_positions():
    tasks = [
        maker_client.get_account_positions(),
        hedge_client.get_account_positions()
    ]
    results = await asyncio.gather(*tasks)
    return results
```

### 3. 缓存策略
```python
# 缓存合约信息（不经常变化）
if not self.contract_info_cache:
    self.contract_info_cache = await self.get_contract_attributes()
return self.contract_info_cache
```

---

## 常见问题和解决方案

### 问题 1：WebSocket 断连
**症状：** 订单更新停止接收

**解决：**
```python
# 实现自动重连
async def maintain_connection():
    while True:
        try:
            await self.connect()
        except Exception as e:
            logger.error(f"Reconnecting: {e}")
            await asyncio.sleep(2)
```

### 问题 2：订单状态不同步
**症状：** WebSocket 显示 FILLED，但 REST 查询显示 OPEN

**解决：**
```python
# 双重验证
ws_status = self.ws_order_status
rest_status = await self.get_order_info(order_id)

if ws_status != rest_status.status:
    logger.warning("Status mismatch, using REST as source of truth")
    self.order_status = rest_status.status
```

### 问题 3：API 限流
**症状：** 429 Too Many Requests

**解决：**
```python
# 优先使用 WebSocket，减少 REST 轮询
# 添加请求间隔
await asyncio.sleep(0.1)  # 每次请求间隔 100ms
```

---

## 总结

### REST API 职责
- ✅ 主动操作：下单、取消、查询
- ✅ 一次性请求
- ✅ 备用数据源

### WebSocket 职责
- ✅ 被动监听：订单更新、行情推送
- ✅ 实时性要求高
- ✅ 主要数据源

### 最佳实践
1. **主动操作用 REST，被动监听用 WebSocket**
2. **WebSocket 优先，REST 作为备份**
3. **实现完善的重连和错误处理**
4. **使用异步编程提高并发性能**
5. **添加日志记录所有关键操作**

---

## 代码示例位置

- **REST 操作示例：** `exchanges/backpack.py` L300-600
- **WebSocket 管理：** `exchanges/backpack.py` L25-150
- **对冲流程：** `hedge/hedge_mode_bp.py` L600-900
- **异步编程模式：** 所有 `exchanges/*.py` 文件
