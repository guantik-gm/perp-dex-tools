# Paradex WebSocket 延迟导致的 SELF_TRADE 问题修复

## 问题描述

**现象**：Paradex Boost 模式下出现 SELF_TRADE 错误，导致平仓失败

```
22:23:31.722 - [CLOSE_MARKET] Placing market order
22:23:32.102 - ❌ Market order failed: Market order canceled: SELF_TRADE
```

## 根本原因

**WebSocket 事件延迟导致重复下单**：

```
22:23:30.900 - [OPEN] [***7790] OPEN 0.001 @ 107681.2  ← 订单 A 下单
22:23:30.903 - [OPEN] [***0780] OPEN 0.001 @ 107693.2  ← 订单 B（仅 3ms 后！）
22:23:31.146 - [OPEN] [***0780] FILLED 0.001           ← B 先成交
22:23:31.722 - [CLOSE_MARKET] Placing market order      ← 市价卖单尝试平 B
22:23:32.102 - ❌ SELF_TRADE                            ← 撞到自己的开仓单 A
```

**问题流程**：
1. 下开仓订单 A
2. WebSocket 事件还在传输中（50-300ms 延迟）
3. 下一轮循环检查活动订单：看不到订单 A（WebSocket 未到）
4. 系统以为需要开仓，创建订单 B
5. 订单 B 先成交，用 MARKET 平仓
6. MARKET 卖单匹配到自己的订单 A（买单）→ SELF_TRADE 错误

## 修复方案

**在下开仓订单前添加 WebSocket 同步延迟**

**文件**：`trading_bot.py` 第 232-233 行

```python
# Place the order
# 等待 WebSocket 事件同步，避免上一订单状态未更新导致重复下单
await asyncio.sleep(0.2)

order_result = await self.exchange_client.place_open_order(
    self.config.contract_id,
    self.config.quantity,
    self.config.direction
)
```

## 效果

- ✅ 给 WebSocket 事件 200ms 同步时间
- ✅ 确保检查活动订单时能看到上一订单的真实状态
- ✅ 避免重复下单 → 避免 SELF_TRADE
- ⚠️ 交易频率轻微下降（<10%）

---

**修复日期**：2025-10-22
**状态**：已修复，待测试验证

---

# IOC 部分成交导致的仓位累积问题

## 问题描述

**现象**：IOC 订单部分成交后，兜底的 MARKET 订单失败，导致微小仓位无法平仓并累积

```
22:59:00.242 - [CLOSE_IOC] ⚠️ IOC partially filled: 0.00097/0.001, remaining: 0.00003
22:59:00.242 - [CLOSE_MARKET] Placing market order for remaining: 0.00003
22:59:00.604 - ❌ Market order failed: ORDER_SIZE_BELOW_MIN
```

**常见失败原因**：
- `ORDER_SIZE_BELOW_MIN`：剩余数量低于交易所最小订单量（0.0001 BTC）
- `EXCEEDS_MAX_SLIPPAGE`：市价单滑点超过交易所限制
- `SELF_TRADE`：撞到自己的限价订单

## 临时解决方案：TG 告警提示手动处理

**文件**：`trading_bot.py` 第 395-409 行

```python
# 兜底的 MARKET 订单失败，统一触发 TG 告警
ioc_filled = ioc_result.filled_size if (ioc_result and ioc_result.success) else 0
remaining = quantity - ioc_filled
alert_msg = (
    f"⚠️ [{self.config.exchange.upper()}_{self.config.contract}] "
    f"兜底平仓订单失败，请手动处理！\n\n"
    f"IOC 成交: {ioc_filled}/{quantity}\n"
    f"剩余数量: {remaining}\n"
    f"失败原因: {market_result.error_message}\n\n"
    f"当前可能有未平仓位，请检查并手动平仓！"
)
try:
    await self.send_notification(alert_msg)
except Exception as e:
    self.logger.log(f"Failed to send TG alert: {e}", "ERROR")
```

## 效果

- ✅ 统一捕获所有 MARKET 订单失败情况
- ✅ 提供详细的错误信息（IOC 成交量、剩余量、失败原因）
- ✅ 及时通知用户手动处理，避免仓位持续累积
- ⚠️ 需要手动介入处理残留仓位

## 未来改进方向

- 累积微小仓位，达到最小订单量后统一平仓
- 放宽 IOC 价格范围，减少部分成交概率
- 添加定期清理机制

