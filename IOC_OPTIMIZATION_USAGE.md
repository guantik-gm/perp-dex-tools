# IOC优化功能使用指南

## 功能说明

IOC (Immediate-Or-Cancel) 优化是一种智能平仓策略，通过先尝试IOC限价单，失败后降级为市价单的方式，减少平仓成本。

### 预期效果
- **Paradex**: 成本从 13.5 USDT → 9.5 USDT (节省30%)
- **GRVT**: 成本从 25.5 USDT → 15.5 USDT (节省39%)

基于刷 100,000 USDT 交易量的计算。

---

## 快速开始

### 1. Paradex IOC优化测试

```bash
# 小额测试（0.001 BTC）
python runbot.py --exchange paradex --ticker BTC \
    --quantity 0.001 --boost --use-ioc --wait-time 5

# 正式运行（0.01 BTC）
python runbot.py --exchange paradex --ticker BTC \
    --quantity 0.01 --boost --use-ioc --wait-time 2
```

### 2. GRVT IOC优化测试

```bash
# 小额测试（0.001 BTC）
python runbot.py --exchange grvt --ticker BTC \
    --quantity 0.001 --boost --use-ioc --wait-time 5

# 正式运行（0.01 BTC）
python runbot.py --exchange grvt --ticker BTC \
    --quantity 0.01 --boost --use-ioc --wait-time 2
```

---

## 参数说明

### 核心参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--boost` | 启用快速平仓模式（必需） | `--boost` |
| `--use-ioc` | 启用IOC优化（推荐） | `--use-ioc` |
| `--exchange` | 交易所选择 | `paradex` 或 `grvt` |
| `--ticker` | 交易币种 | `BTC`, `ETH`, `SOL` |
| `--quantity` | 每次交易数量 | `0.01` |
| `--wait-time` | 交易间隔（秒） | `2` |

### 工作原理

```
开仓（Post-Only限价单）
    ↓ 成交
平仓开始
    ↓
【IOC限价单】先尝试以更好价格成交
    ├─ 完全成交 ✅ → 节省成本！
    ├─ 部分成交 ⚠️ → 剩余用市价单
    └─ 未成交 ❌ → 降级为市价单
          ↓
【市价单】保证成交
```

---

## 日志示例

### 成功案例（IOC完全成交）

```
[CLOSE_IOC] Attempting IOC order: 0.01 @ 49999.50
[CLOSE_IOC] ✅ IOC fully filled: 0.01 @ 49999.50
```

**节省：** 相比市价单 @ 50003，节省了 3.5 USDT

### 部分成交案例

```
[CLOSE_IOC] Attempting IOC order: 0.01 @ 49999.50
[CLOSE_IOC] ⚠️ IOC partially filled: 0.005/0.01, remaining: 0.005
[CLOSE_MARKET] Placing market order for remaining: 0.005
[CLOSE_MARKET] ✅ Market order filled: 0.005 @ 50001.00
```

**节省：** IOC成交了50%，仍然节省了部分成本

### 未成交案例（降级）

```
[CLOSE_IOC] Attempting IOC order: 0.01 @ 49999.50
[CLOSE_IOC] IOC not filled, will use market order
[CLOSE_MARKET] Placing market order for remaining: 0.01
[CLOSE_MARKET] ✅ Market order filled: 0.01 @ 50002.00
```

**结果：** 与纯市价单相同，没有额外损失

---

## 成本对比

### Paradex示例（BTC @ 50,000 USDT）

#### 方案A：纯市价单（当前）

| 项目 | 计算 | 成本 |
|------|------|------|
| 开仓Maker | 500 × 0.003% | +1.5 USDT |
| 平仓Taker | 500 × 0.02% | +10.0 USDT |
| 滑点 | ~0.04% | +2.0 USDT |
| **总成本** | | **13.5 USDT** |

#### 方案B：IOC优化（推荐）

| 项目 | 计算 | 成本 |
|------|------|------|
| 开仓Maker | 500 × 0.003% | +1.5 USDT |
| 平仓（IOC混合） | ~0.014% | +7.0 USDT |
| 滑点优化 | ~0.02% | +1.0 USDT |
| **总成本** | | **9.5 USDT** ✅ |

**节省：4.0 USDT (30%)**

---

### GRVT示例（BTC @ 50,000 USDT）

#### 方案A：纯市价单（当前）

| 项目 | 计算 | 成本 |
|------|------|------|
| 开仓Maker | 500 × (-0.01%) | -0.05 USDT 返佣 |
| 平仓Taker | 500 × 0.055% | +27.5 USDT |
| 滑点 | ~0.06% | +3.0 USDT |
| **总成本** | | **25.5 USDT** |

#### 方案B：IOC优化（推荐）

| 项目 | 计算 | 成本 |
|------|------|------|
| 开仓Maker | 500 × (-0.01%) | -0.05 USDT 返佣 |
| 平仓（IOC混合） | ~0.038% | +19.0 USDT |
| 滑点优化 | ~0.03% | +1.5 USDT |
| **总成本** | | **15.5 USDT** ✅ |

**节省：10.0 USDT (39%)**

---

## 监控与验证

### 查看实时日志

```bash
# 查看最近的平仓操作
tail -f logs/paradex_BTC_trading_log.txt | grep CLOSE

# 统计IOC成功率
grep "IOC fully filled" logs/paradex_BTC_trading_log.txt | wc -l
```

### 计算实际节省

```bash
# 查看所有平仓记录
grep "CLOSE_" logs/paradex_BTC_trading_log.txt > close_analysis.txt

# 统计IOC vs Market比例
grep -c "IOC fully filled" close_analysis.txt
grep -c "CLOSE_MARKET" close_analysis.txt
```

---

## 常见问题

### Q1: IOC优化会影响成交速度吗？

**A:** 不会。IOC订单最多延迟0.5秒，如果不成交立即降级为市价单，总体成交时间仍在1-2秒内。

### Q2: 所有币种都适用吗？

**A:** 建议使用流动性好的主流币：
- ✅ **推荐**: BTC, ETH, SOL
- ⚠️ **慎用**: 小市值币种（流动性差，IOC成交率低）

### Q3: 实际节省效果如何验证？

**A:** 运行100轮对比测试：
```bash
# 不使用IOC（对照组）
python runbot.py --exchange paradex --ticker BTC \
    --quantity 0.001 --boost --wait-time 2 --max-orders 100

# 使用IOC（实验组）
python runbot.py --exchange paradex --ticker BTC \
    --quantity 0.001 --boost --use-ioc --wait-time 2 --max-orders 100

# 对比总交易成本
```

### Q4: 为什么必须配合--boost使用？

**A:** IOC优化只针对boost模式的市价平仓，普通模式使用限价单平仓，不需要IOC优化。

### Q5: 出现错误怎么办？

```bash
# 错误1：IOC order timeout
# 原因：网络延迟或交易所响应慢
# 解决：会自动降级为市价单，无需处理

# 错误2：IOC not filled
# 原因：价格波动大，IOC价格不合适
# 解决：会自动使用市价单，保证成交
```

---

## 环境要求

### Paradex环境变量

```bash
PARADEX_L1_ADDRESS=0x...
PARADEX_L2_PRIVATE_KEY=0x...
PARADEX_L2_ADDRESS=0x...
PARADEX_ENVIRONMENT=prod
```

### GRVT环境变量

```bash
GRVT_TRADING_ACCOUNT_ID=...
GRVT_PRIVATE_KEY=0x...
GRVT_API_KEY=...
GRVT_ENVIRONMENT=prod
```

---

## 技术细节

### IOC订单API

#### Paradex

```python
order = Order(
    market="BTC-USD-PERP",
    order_type=OrderType.Limit,
    order_side=OrderSide.Sell,
    size=0.01,
    limit_price=49999.50,
    instruction="IOC"  # 关键参数
)
```

#### GRVT

```python
order = rest_client.create_limit_order(
    symbol="BTC_USDT_Perp",
    side="sell",
    amount=0.01,
    price=49999.50,
    params={
        'time_in_force': 'IMMEDIATE_OR_CANCEL'  # 关键参数
    }
)
```

---

## 实战建议

### 1. 渐进式测试

```bash
# 第一步：极小量测试（10次）
python runbot.py --exchange paradex --ticker BTC \
    --quantity 0.001 --boost --use-ioc --wait-time 5 --max-orders 10

# 第二步：小量测试（100次）
python runbot.py --exchange paradex --ticker BTC \
    --quantity 0.001 --boost --use-ioc --wait-time 3 --max-orders 100

# 第三步：正式运行
python runbot.py --exchange paradex --ticker BTC \
    --quantity 0.01 --boost --use-ioc --wait-time 2
```

### 2. 成本监控

建议每刷10,000 USDT交易量后，检查实际成本：

```bash
# 计算总交易量
grep "FILLED" logs/paradex_BTC_trading_trades.csv | \
    awk -F',' '{sum+=$5*$4} END {print "Total Volume:", sum}'

# 估算实际成本（根据日志）
# IOC成交率 × IOC成本 + 市价成交率 × 市价成本
```

### 3. 参数调优

```bash
# 如果IOC成交率低于50%，可能需要：
# 1. 增加ioc_tolerance（默认0.01%）
# 2. 选择流动性更好的币种
# 3. 避开市场波动大的时段
```

---

## 相关文档

- [刷量策略详解](./docs/VOLUME_BOOSTING_STRATEGIES.md)
- [行业分析报告](./docs/VOLUME_FARMING_INDUSTRY_ANALYSIS.md)
- [交易所通讯架构](./docs/EXCHANGE_COMMUNICATION_ARCHITECTURE.md)

---

## 更新日志

### v1.0.0 (2025-01-XX)
- ✅ 实现Paradex IOC支持
- ✅ 实现GRVT IOC支持
- ✅ 智能降级机制
- ✅ 详细日志输出
- ✅ 成本监控

---

**需要帮助？** 查看日志文件或运行 `python runbot.py --help`
