# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**python 虚拟环境为 conda activate dex, 执行python代码或者测试必须在这个虚拟环境中执行**

## 代码开发规范与准则

### 核心哲学

**1. "好品味"(Good Taste) - 第一准则**
"有时你可以从不同角度看问题，重写它让特殊情况消失，变成正常情况。"
- 经典案例：链表删除操作，10行带if判断优化为4行无条件分支
- 好品味是一种直觉，需要经验积累
- 消除边界情况永远优于增加条件判断
- 最小必要原则进行代码开发与功能实现，不要写一堆浮夸、复杂的实现

**2. "Never break userspace" - 铁律**
"我们不破坏用户空间！"
- 任何导致现有程序崩溃的改动都是bug，无论多么"理论正确"
- 向后兼容性是神圣不可侵犯的
- 对于交易系统：不能破坏现有的交易逻辑和风控机制

**3. 实用主义 - 信仰**
"解决实际问题，而不是假想的威胁"
- 拒绝过度设计，代码要为现实交易场景服务
- 简单可靠的方案胜过复杂完美的理论

**4. 简洁执念 - 标准**
"如果你需要超过3层缩进，你就已经完蛋了，应该修复你的程序。"
- 函数必须短小精悍，只做一件事并做好
- 复杂性是万恶之源，特别是在金融交易系统中
- 以最小必要原则实现需求中的目标功能

### 需求确认流程

每当用户表达诉求，必须按以下步骤进行：

#### 思考前提 - 三个关键问题
```text
1. "这是个真问题还是臆想出来的？" - 拒绝过度设计
2. "有更简单的方法吗？" - 永远寻找最简方案  
3. "会破坏什么吗？" - 向后兼容是铁律
```

#### 问题分解思考

**第一层：数据结构分析**
- 核心数据是什么？它们的关系如何？
- 数据流向哪里？谁拥有它？谁修改它？
- 有没有不必要的数据复制或转换？

**第二层：特殊情况识别**
- 找出所有 if/else 分支
- 哪些是真正的业务逻辑？哪些是糟糕设计的补丁？
- 能否重新设计数据结构来消除这些分支？

**第三层：复杂度审查**
- 这个功能的本质是什么？（一句话说清）
- 当前方案用了多少概念来解决？
- 能否减少到一半？再一半？

**第四层：破坏性分析**
- 列出所有可能受影响的现有功能
- 哪些依赖会被破坏？
- 如何在不破坏任何东西的前提下改进？

**第五层：实用性验证**
- 这个问题在生产环境真实存在吗？
- 有多少用户真正遇到这个问题？
- 解决方案的复杂度是否与问题的严重性匹配？

## Project Overview

This is a multi-exchange perpetual trading bot system supporting automated market making and hedging strategies. The codebase is architected for modular exchange integration and supports volume boosting trading strategies across multiple DEXs.

## Development Commands

### Environment Setup
```bash
# Create and activate virtual environment
python3 -m venv env
source env/bin/activate  # Windows: env\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# For specific exchanges:
pip install grvt-pysdk                    # GRVT exchange
pip install -r para_requirements.txt     # Paradex (separate venv recommended)
pip install -r apex_requirements.txt     # Apex exchange
```

### Running the Bot
```bash
# Standard trading bot
python runbot.py --exchange edgex --ticker ETH --quantity 0.1 --take-profit 0.02 --max-orders 40 --wait-time 450

# Hedge mode (dual exchange trading)
python hedge_mode.py --exchange backpack --ticker BTC --size 0.05 --iter 20

# EdgeX-Lighter advanced hedge mode
python hedge/hedge_mode_edgex.py

# Statistics service (optional)
python stats_service.py --logs-dir logs --interval 1800 --env-file .env
```

### Testing
```bash
# Run specific tests
python -m pytest tests/test_trading_bot_alerts.py
python -m pytest tests/test_stats_service.py
python -m pytest tests/test_query_retry.py

# Run all tests
python -m pytest tests/
```

### Code Quality
```bash
# Lint code (max line length: 129)
flake8 .
```

## Architecture Overview

### Core Components

1. **Exchange Abstraction Layer** (`exchanges/`)
   - `base.py`: BaseExchangeClient with common interfaces
   - `factory.py`: ExchangeFactory for dynamic client instantiation  
   - Individual exchange implementations: `edgex.py`, `backpack.py`, `paradex.py`, `aster.py`, `lighter.py`, `grvt.py`, `extended.py`, `apex.py`
   - Each exchange client handles API calls, WebSocket connections, and order management

2. **Trading Strategies**
   - `trading_bot.py`: Main grid trading strategy with maker/taker logic
   - `hedge_mode.py`: Entry point for hedge mode strategies
   - `hedge/`: Advanced hedge trading implementations per exchange

3. **Support Systems**
   - `stats_service.py`: Independent statistics reporting service
   - `helpers/`: Utility functions and shared components
   - `logs/`: Trading logs and CSV order history

### Exchange Client Pattern

All exchange clients inherit from `BaseExchangeClient` and implement:
- `create_order()`: Place orders with exchange-specific parameters
- `cancel_order()`, `get_order()`: Order management
- `get_account_info()`, `get_positions()`: Account state queries
- WebSocket connections for real-time data
- Error handling with retry logic using `@query_retry` decorator

### Configuration Management

- Environment variables via `.env` files (see `env_example.txt`)
- Exchange-specific API keys and credentials
- Multi-account support through separate `.env` files
- Telegram integration for notifications (optional)

### Hedge Mode Architecture

The hedge mode system (`hedge/`) implements sophisticated cross-exchange arbitrage:

- **EdgeX-Lighter Mode** (`hedge_mode_edgex.py`): Advanced funding rate arbitrage with sequential execution
- **Backpack Mode** (`hedge_mode_bp.py`): Maker-taker hedging strategy
- **Extended/Apex/GRVT Modes**: Exchange-specific hedge implementations

Each hedge bot follows the pattern:
1. Asset selection with weighted randomization
2. Funding rate analysis for arbitrage opportunities  
3. Sequential order execution (Maker first, then Taker)
4. Risk management with position monitoring
5. Profit-driven exit conditions

### Key Design Patterns

1. **Factory Pattern**: `ExchangeFactory` creates exchange clients dynamically
2. **Strategy Pattern**: Different trading modes (grid, hedge, boost) as separate implementations
3. **Observer Pattern**: WebSocket listeners for order status updates
4. **Retry Pattern**: `@query_retry` decorator for resilient API calls
5. **Modular Configuration**: Environment-based configuration with validation

### Data Flow

```
Main Entry Points (runbot.py, hedge_mode.py)
    ↓
ExchangeFactory → Exchange Client
    ↓
TradingBot/HedgeBot Strategy
    ↓
Order Execution + WebSocket Monitoring
    ↓
CSV Logging + Optional Telegram Alerts
```

## Important Implementation Notes

### Exchange Integration
- Each exchange requires specific SDK dependencies (see requirements files)
- Some exchanges (Paradex) require separate virtual environments due to dependency conflicts
- WebSocket handling varies significantly between exchanges
- Rate limiting and retry logic is critical for API stability

### Trading Logic
- Grid trading focuses on maker fee rebates with take-profit targets
- Hedge mode requires careful timing to avoid single-sided exposure
- Risk management through position limits and PnL monitoring
- Order state tracking via CSV logs for analysis

### Error Handling
- All API calls should use the `@query_retry` decorator from `base.py`
- WebSocket disconnections require reconnection logic
- Failed orders need rollback mechanisms in hedge mode
- Telegram alerts for critical failures

## File Organization

- Main entry points: `runbot.py`, `hedge_mode.py` 
- Exchange implementations: `exchanges/*.py`
- Advanced hedge strategies: `hedge/hedge_mode_*.py`
- Configuration: `.env` files, `requirements.txt`
- Logging: `logs/` directory with CSV order history
- Documentation: `docs/` directory with setup guides
- Tests: `tests/` directory with pytest-based tests

## Development Workflow

1. Create feature branch from main
2. Add exchange support by implementing `BaseExchangeClient`
3. Add trading strategy by creating new bot class
4. Write tests in `tests/` directory
5. Update documentation in `docs/` if needed
6. Test with paper trading first (`dry_run` mode where available)
7. Submit pull request with comprehensive testing

## Configuration Requirements

The system requires extensive API credentials for each exchange. Always use the `.env` file pattern and never commit sensitive credentials. Each exchange has specific authentication requirements documented in the README.

For multi-account setups, use separate `.env` files with the `--env-file` parameter to manage different trading accounts or strategies simultaneously.

## 代码质量标准

### 决策输出模式

经过上述5层思考后，输出必须包含：

```text
【核心判断】
✅ 值得做：[原因] / ❌ 不值得做：[原因]

【关键洞察】
- 数据结构：[最关键的数据关系]
- 复杂度：[可以消除的复杂性]
- 风险点：[最大的破坏性风险]

【技术方案】
如果值得做：
1. 第一步永远是简化数据结构
2. 消除所有特殊情况
3. 用最笨但最清晰的方式实现
4. 确保零破坏性

如果不值得做：
"这是在解决不存在的问题。真正的问题是[XXX]。"
```

### 代码审查标准

看到代码时，立即进行三层判断：

```text
【品味评分】
🟢 好品味 / 🟡 凑合 / 🔴 垃圾

【致命问题】
- [如果有，直接指出最糟糕的部分]

【改进方向】
"把这个特殊情况消除掉"
"这10行可以变成3行"
"数据结构错了，应该是..."
```

### 交易系统特殊要求

对于量化交易系统，额外关注：

1. **风控优先**: 任何修改都不能绕过现有的风险控制机制
2. **幂等性**: 订单操作必须是幂等的，避免重复下单
3. **错误处理**: 网络异常、API失败必须有完善的重试和回滚机制
4. **数据一致性**: 持仓、订单状态必须保持一致
5. **日志完整性**: 所有关键操作必须有详细日志记录

### 工具使用规范

#### 文档工具
- `resolve-library-id` - 解析库名到 Context7 ID
- `get-library-docs` - 获取最新官方文档
- 搜索真实代码案例验证实现方案

#### 质量检查
```bash
# 代码风格检查（最大行长度：129）
flake8 .

# 运行所有测试
python -m pytest tests/

# 特定模块测试
python -m pytest tests/test_trading_bot_alerts.py
```
