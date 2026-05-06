# 🤖 恒指牛熊证智能交易系统

基于 RSI + VWAP + 成交量 + 跨周期确认的恒指牛熊证量化交易策略，前后端分离架构。

## 系统架构

```
┌─────────────────────────────────────────────────┐
│  Frontend (React + TypeScript + Vite + Ant Design) │
│  - 实时价格/RSI/成交量图表 (Recharts)              │
│  - 策略状态面板                                    │
│  - 交易记录表格                                    │
│  - 参数配置控制面板                                │
│  Port: 5173                                        │
└──────────────────┬──────────────────────────────┘
                   │ REST API + WebSocket
┌──────────────────▼──────────────────────────────┐
│  Backend (Python + FastAPI)                        │
│  - 策略引擎 (RSI/VWAP/VOL/K线形态)                │
│  - 模拟行情数据生成器                              │
│  - WebSocket 实时推送                              │
│  - REST API (配置/控制/历史查询)                   │
│  Port: 8888                                        │
└─────────────────────────────────────────────────┘
```

## 策略逻辑

### 买入牛证条件 (做多)
- RSI < 25 (超卖)
- VWAP 斜率 > 0 (均价在抬高，多头趋势)
- 成交量 > 20周期均量 (放量确认)
- 15分钟 K 线收阳
- 30分钟 K 线收阳 (跨周期确认)

### 买入熊证条件 (做空)
- RSI > 75 (超买)
- VWAP 斜率 < 0 (均价在下降，空头趋势)
- 成交量 > 20周期均量 (放量确认)
- 15分钟 K 线收阴
- 30分钟 K 线收阴 (跨周期确认)

### 平仓条件
- 止盈：盈利达到 ±100 点 (约 1000 HKD)
- 止损：亏损达到 ±100 点 (约 1000 HKD)

## 快速启动

### 1. 安装后端依赖

```bash
cd backend
python -m venv venv
venv\Scripts\pip.exe install -r requirements.txt
```

### 2. 安装前端依赖

```bash
cd frontend
pnpm install
```

### 3. 启动

分别在两个终端中运行：

**后端** (端口 8888)：
```bash
start-backend.bat
```

**前端** (端口 5173)：
```bash
start-frontend.bat
```

然后打开浏览器访问 http://localhost:5173

## API 文档

启动后端后访问 http://localhost:8888/docs 查看 Swagger 文档。

### 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/config | 获取策略配置 |
| PUT | /api/config | 更新策略参数 |
| GET | /api/state | 获取策略状态 |
| POST | /api/start | 启动策略 |
| POST | /api/stop | 停止策略 |
| POST | /api/reset | 重置策略 |
| GET | /api/trades | 获取交易历史 |
| GET | /api/klines | 获取K线历史 |
| WS | /ws | WebSocket 实时推送 |

## 技术栈

- **后端**: Python 3.11 + FastAPI + Pandas + NumPy
- **前端**: React 18 + TypeScript + Vite + Ant Design + Recharts
- **通信**: REST API + WebSocket 实时推送
