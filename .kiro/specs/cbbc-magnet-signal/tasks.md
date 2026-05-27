# Implementation Plan: CBBC 街货磁吸信号

## Overview

按设计文档把模块切成 6 个新文件 + 既有文件的最小侵入扩展。实现顺序：先打地基（运行时配置、数据模型、目录与 dataclass），再做存储 → 计算器 → 抓取服务 → 信号适配器 → 引擎接入 → 回测 → 研究脚本 → 后端 API/WebSocket → 前端 overlay。每个新模块的纯逻辑先到位，再串入 `HSIStrategyEngine` 与 `bt.py`，最后才动前端。

实现语言：Python（沿用 `backend/` 现有栈），前端 TypeScript（沿用 `frontend/src/`）。所有测试子任务用 `pytest`（后端）与现有前端测试框架，标注为可选（`*`）；核心实现任务必做。

## Tasks

- [x] 1. 运行时配置与项目骨架
  - [x] 1.1 在 `runtime_config_store.py` 新增 6 个 CBBC 字段并准备目录占位
    - 在 `backend/runtime_config_store.py` 增加字段：`cbbc_magnet_layer_enabled` (bool, default `false`)、`cbbc_intraday_polling_suspended` (bool, default `false`)、`cbbc_magnet_decay_points` (float, 300.0, `(0.0, 10000.0]`)、`cbbc_dense_band_threshold_pts` (float, 150.0, `[10.0, 1000.0]`)、`cbbc_dense_band_pull_share` (float, 0.40, `[0.0, 1.0]`)、`cbbc_intraday_poll_interval_seconds` (float, 60.0, `[10.0, 600.0]`)
    - 沿用现有的 "临时文件 + 原子重命名" 写入路径；在加载时按区间校验，无效值回退默认值并写入 `level=WARN, source=runtime_config, event=config_corrupt_fallback_defaults`
    - 创建 `backend/data/cbbc/.gitkeep` 与 `backend/research/.gitkeep`
    - _Requirements: 9.1, 9.3, 9.4, 9.5, 9.6, 9.8, 9.9_

  - [x] 1.2 为 runtime_config 新增字段编写单元测试
    - 覆盖：默认值、合法值、超界值、NaN/±Inf、错误类型、回退日志、原子写入失败回滚
    - _Requirements: 9.3, 9.6, 9.8, 9.9_

- [x] 2. 实现 CBBC 存储层
  - [x] 2.1 创建 `cbbc_storage.py` 数据类与错误码
    - 在 `backend/cbbc_storage.py` 定义 `CbbcRecord`（10 个字段，frozen）、`CbbcSnapshot`（frozen，`records: tuple`）、`SnapshotError`（错误码：`non_trading_day`、`snapshot_missing`、`snapshot_immutable`、`no_reverse_deduction_allowed`）
    - 暴露 `is_hk_trading_day(date)` / `hk_public_holidays()` 工具函数（结合 HKEX 公布的港股公众假期清单与周末规则）
    - _Requirements: 1.5, 3.6, 3.7_

  - [x] 2.2 实现 `CbbcStorage` parquet 读写与生存偏差守卫
    - 路径 `backend/data/cbbc/outstanding_YYYYMMDD.parquet`；写入前若目标 `snapshot_date` 已存在则抛 `SnapshotError("snapshot_immutable")`
    - `read_snapshot(d)`：非交易日 → `non_trading_day`；文件缺失 → `snapshot_missing`；返回时只保留 `listing_date <= d` 且 `maturity_date >= d` 的记录
    - `latest_before(d)`：返回严格小于 d 且最近的存活快照（用于每日抓取失败时的回退）
    - `reject_reverse_deduction(today, requested)`：当调用方仅以"今日仍存活集合"反推历史时抛 `no_reverse_deduction_allowed`
    - 写入使用临时文件 + 原子重命名以避免半文件
    - _Requirements: 1.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 2.3 为 `cbbc_storage.py` 编写单元测试
    - 覆盖：成功写入、不可覆盖、缺失文件错误、非交易日错误、生存偏差守卫、`latest_before` 边界
    - _Requirements: 3.1, 3.4, 3.5, 3.6, 3.7_

- [x] 3. 实现磁吸量化计算
  - [x] 3.1 在 `cbbc_calculator.py` 实现 `compute_magnet` 纯函数与结果数据类
    - 在 `backend/cbbc_calculator.py` 定义 `HistBucket`、`MagnetResult`(含 `nearest_bull_distance_pts`、`nearest_bear_distance_pts`、`hsi_spot_stale`、5pt 桶直方图覆盖最近 200 点)
    - 计算公式严格按设计:`distance_pts = |Call_Level - HSI_Spot|`、`weight = max(0, 1 - distance_pts / decay_points)`、`notional = outstanding_shares * er_ratio * weight`、`magnet_bias = clamp((bear - bull)/max(bear+bull, 1.0), -1, 1)`
    - 跳过 `Call_Level / outstanding_shares / er_ratio` 任一为空 / 非有限 / 负值或 direction 非法的记录,累加 `skipped_count`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.9, 4.11_

  - [x] 3.2 实现 `MagnetEngine` 状态容器与发布逻辑
    - 在 `backend/cbbc_calculator.py` 增加 `MagnetEngine`:`update_hsi_spot`、`update_snapshot`、`update_decay_points`、`latest()`
    - HSI_Spot 最近 5 秒未刷新时把 `hsi_spot_stale=True` 并阻断新结果发布,向调用方返回 `hsi_spot_stale` 错误
    - `|new_hsi_spot - last_used_hsi_spot| > 5.0` 时在 3 秒内重算并发布最新结果对象
    - `update_decay_points` 校验 `(0.0, 10000.0]` 与有限性,非法值保留旧值并返回 `cbbc_magnet_decay_points_invalid`
    - 用 `asyncio.Lock` 串行化 publish;`latest()` 返回不可变 `MagnetResult`
    - _Requirements: 4.7, 4.8, 4.10, 4.12_

  - [x] 3.3 为 `cbbc_calculator.py` 编写单元测试
    - 覆盖:bias 截断、跳过非法记录、stale 标志、decay 越界拒绝、Δ>5 重算、零总 pull 边界
    - _Requirements: 4.5, 4.6, 4.8, 4.10, 4.11, 4.12_

- [x] 4. 实现 CBBC 数据服务
  - [x] 4.1 在 `cbbc_data.py` 创建 HKEX 白名单守卫与限频客户端
    - 在 `backend/cbbc_data.py` 实现 `RateLimitedClient`:60 秒滚动窗口对同一完整 URL 限 6 次,达上限后延后并写 `level=WARN, source=cbbc_data, event=rate_limit_deferred`
    - 域名白名单 `{www.hkex.com.hk, www1.hkexnews.hk}`;非白名单或路径不符 → `level=ERROR, source=cbbc_data, event=blocked_non_whitelisted_endpoint` 并阻断请求
    - 鉴权 / 付费检测:在请求前阻断包含 Cookie 会话头、Authorization、API Key 的请求;遇到响应 401/403 也判定为受限并写 `level=ERROR, source=cbbc_data, event=blocked_paywalled_or_authenticated_endpoint`(日志中只保留 URL 与原因,不写凭据)
    - _Requirements: 11.1, 11.2, 11.3, 11.7, 11.8_

  - [x] 4.2 实现 outstanding 解析器与 HSI 过滤
    - 解析 HKEX outstanding 表为 `CbbcRecord`,校验 10 字段非空、direction ∈ {bull, bear}
    - 仅保留 `underlying == "HSI"`;非 HSI 记录直接丢弃
    - 解析失败的单条记录跳过并写 `level=WARN, source=cbbc_data, event=record_parse_failed`(含 code 与原因)
    - _Requirements: 1.4, 1.5, 1.6_

  - [x] 4.3 实现每日 T+1 抓取任务
    - 仅在港股交易日的 18:00:00–23:59:59 (Asia/Hong_Kong) 触发;非交易日写 `level=INFO, source=cbbc_data, event=daily_fetch_skipped_non_trading_day` 并跳过
    - 同一交易日已生成快照时直接复用(幂等)
    - 单次请求超时 30 秒;退避 `[60s, 180s, 600s]`,最多 3 次重试
    - 所有重试用尽后写 `level=ERROR, source=cbbc_data, event=daily_fetch_failed`,并保留上一交易日快照
    - 解析后 HSI 记录数为 0 或 < 上一交易日 50% 时不写新快照,写 `level=ERROR, source=cbbc_data, event=daily_fetch_incomplete` 并保留上一交易日快照
    - 通过 `CbbcStorage.write_snapshot` 持久化,确保不可覆盖
    - _Requirements: 1.1, 1.2, 1.3, 1.7, 1.8, 1.9, 1.10_

  - [x] 4.4 实现盘中新发上市轮询
    - 仅在 Morning_Session(09:30–12:00)与 Afternoon_Session(13:00–16:00)轮询,间隔 `cbbc_intraday_poll_interval_seconds`(默认 60s),单次超时 10s
    - 检测到 `code` 不在内存快照且 `listing_date == today` 时,90 秒内合并到内存快照(按 `code` 去重)
    - 非 HSI 新发记录丢弃 + `level=DEBUG, source=cbbc_data, event=intraday_new_listing_dropped_non_hsi`
    - 失败枚举(HTTP 非 2xx / 网络异常 / 超时 / 解析失败 / DNS / TLS)连续 5 分钟则写 `level=WARN, source=cbbc_data, event=intraday_polling_degraded`,将 `cbbc_intraday_polling_suspended=true` 写入 Runtime_Config 并停止轮询;不得自动恢复
    - 进入交易时段且 `cbbc_intraday_polling_suspended=true` 时,每个交易日仅写一条 `level=INFO, source=cbbc_data, event=cbbc_intraday_polling_suspended_notice`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 4.5 串联 `CbbcDataService` 生命周期与只读视图
    - 在 `backend/cbbc_data.py` 实现 `start/stop`、`current_snapshot()`、`last_refresh_ts_hk()`、`is_intraday_polling_suspended()`、`trigger_daily_fetch(date)`、`trigger_intraday_poll_once()`
    - 把每日与盘中两个 asyncio 任务挂到事件循环,自检 `cbbc_intraday_polling_suspended` 标志
    - _Requirements: 1.1, 1.7, 2.1, 2.6, 10.4_

  - [x] 4.6 为 `cbbc_data.py` 编写单元测试(mock HTTP)
    - 覆盖:每日窗口外不触发、非交易日跳过、重试退避、incomplete 保留旧快照、idempotent;盘中合并去重、非 HSI 丢弃、近价事件日志、5 分钟降级 + suspended 持久化、suspended 时不再合并;白名单阻断、鉴权阻断、限频延后日志
    - _Requirements: 1.1-1.10, 2.1-2.7, 11.1-11.3, 11.7-11.8_

- [x] 5. Checkpoint - 抓取与计算层完成
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. 实现磁吸信号适配器
  - [x] 6.1 在 `cbbc_signal_adapter.py` 创建 `ConsultDecision` 与适配器骨架
    - 在 `backend/cbbc_signal_adapter.py` 定义 `ConsultDecision`（vetoed、reason_code 七项枚举、最近距离、bias、`magnet_aligned_against_reversal`、`magnet_available`）
    - 注入 `MagnetEngine`、Runtime_Config、`ClockProtocol`；构造 `MagnetSignalAdapter` 类
    - _Requirements: 5.1, 5.4, 5.6, 5.8, 5.11_

  - [x] 6.2 实现 `consult_for_extreme` 决策逻辑（含参数 fallback）
    - 仅作用于 extreme 分支：BULL 看最近 bear-direction Call_Level；BEAR 看最近 bull-direction Call_Level
    - 距离 > 阈值 → `cbbc_dense_band_clear`，不否决；距离 ≤ 阈值且对应方向 pull share ≥ `cbbc_dense_band_pull_share` → `cbbc_dense_band_above` / `cbbc_dense_band_below`，否决；距离 ≤ 阈值但 share < 阈值 → `cbbc_dense_band_pull_share_below`，不否决
    - HSI_Spot / Magnet_Bias / Call_Level 任一不可用，或 `pull_bull + pull_bear == 0` → 不否决，写 `event=cbbc_magnet_consult_unavailable`
    - 计算 `magnet_aligned_against_reversal`：BULL 信号且 `bias > 0` 或 BEAR 信号且 `bias < 0`；`bias = 0` 不视为背离
    - `cbbc_dense_band_threshold_pts`、`cbbc_dense_band_pull_share` 越界或非有限时回退默认并写 `level=WARN, source=cbbc_magnet, event=cbbc_dense_band_threshold_pts_invalid_fallback` / `cbbc_dense_band_pull_share_invalid_fallback`
    - 每次正常咨询恰好写一条 `event=cbbc_magnet_consult` 结构化日志（含设计指定字段）
    - 当 `cbbc_magnet_layer_enabled=false` 时直接返回 `vetoed=False, reason=cbbc_magnet_layer_disabled`，不写 consult 日志
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.10, 5.11, 5.12, 9.2_

  - [x] 6.3 实现降级生命周期与显式恢复初始化
    - 检测 `now_hk - last_refresh_ts_hk > 36h` 时进入降级:`cbbc_magnet_degraded=True`、bias=0 中性结果、不否决;状态转换写恰好一条 `level=WARN, source=cbbc_magnet, event=degraded_no_data`
    - 恢复路径:当快照刷新差 ≤ 36h 时依次执行 (a) 重载快照 (b) 重算 bias,两步均无异常且产出有效结果后才执行 (c) 写 `level=INFO, source=cbbc_magnet, event=recovery_initialized` (d) 把 `cbbc_magnet_degraded=False`
    - 任一步异常或 5 秒未返回 → 保持降级、写 `level=WARN, source=cbbc_magnet, event=recovery_failed`,下次刷新事件再试
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [x] 6.4 为 `cbbc_signal_adapter.py` 编写单元测试
    - 覆盖:七种 reason_code、layer disable 分支、不可用 fail-safe、share 不足分支、磁吸背离与对齐、阈值/份额非法回退、降级 / 恢复 / 恢复失败
    - _Requirements: 5.1-5.12, 9.2, 10.1-10.5_

- [x] 7. 接入 `HSIStrategyEngine`
  - [x] 7.1 扩展 `models.py` 的 `StrategyState`、配置载体与新模型
    - 在 `backend/models.py` 给 `StrategyState` 新增 `cbbc_magnet_layer_enabled`、`cbbc_magnet_degraded`、`cbbc_magnet_bias`、`cbbc_nearest_bull_distance_pts`、`cbbc_nearest_bear_distance_pts`、`last_magnet_consult` 字段
    - 新增 `MagnetConsultRecord`、`MagnetOverlayPayload`、`BacktestMagnetSummary` Pydantic 模型
    - 扩展 `ConfigResponse` / `ConfigUpdate` 以暴露 6 个 CBBC 配置字段
    - _Requirements: 5.8, 7.7, 8.1, 9.1, 9.3, 9.7, 10.3_

  - [x] 7.2 在 `HSIStrategyEngine.__init__` / `update_config` 中织入 CBBC 组件
    - 在 `backend/strategy.py` 构造 `CbbcStorage`、`MagnetEngine`、`MagnetSignalAdapter`、`CbbcDataService`，注入 Runtime_Config 与时钟
    - 启动时把 6 个 CBBC 字段同步到三组件；运行时配置变更时再次同步
    - 在合适的生命周期钩子调用 `CbbcDataService.start/stop`
    - _Requirements: 9.5, 9.7_

  - [x] 7.3 在 extreme 分支信号点接入磁吸咨询并捕获异常
    - 仅在 extreme 反转 `sig` 生成之后、`_submit_entry_order` 之前调用 `consult_for_extreme`，传入方向、HSI_Spot、HK 时间
    - 命中否决时调用 `_emit_strategy_disabled_skip(mode="extreme", reason=f"cbbc_magnet_veto:{reason_code}")` 并 return；不修改 normal / momentum / cum_trend / rsi_divergence 分支
    - 把 `ConsultDecision` 落到 `StrategyState.last_magnet_consult` 与对应汇总字段
    - 用 try/except 捕获 `CbbcDataService` / `MagnetEngine` / `MagnetSignalAdapter` 抛出的未捕获异常：写 `level=WARN, source=cbbc_magnet, event=...`，把 `cbbc_magnet_degraded=True`，继续主策略循环
    - _Requirements: 5.1-5.12, 5.9, 10.6_

  - [x] 7.4 为 strategy.py extreme 分支磁吸接入编写集成测试
    - 在 `backend/test_cbbc_strategy_integration.py` 覆盖：layer disabled 不影响入场、密集带否决路径、不可用 fail-safe、未捕获异常路径不阻塞主循环、其他 4 类入场分支结果不变
    - _Requirements: 5.9, 9.2, 10.6_

- [x] 8. Checkpoint - 实盘集成完成
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. 后端 API 与 WebSocket 推送
  - [x] 9.1 在 `main.py` 暴露 `/api/state` CBBC 字段并挂载 lifespan
    - 在 `backend/main.py` 把新增 `StrategyState` 字段输出到 `/api/state`
    - 通过 FastAPI lifespan 在应用启动 / 停止时调度 `CbbcDataService.start/stop`
    - _Requirements: 9.5, 9.7, 10.3_

  - [x] 9.2 推送 `type=magnet_overlay` WebSocket 消息
    - 在 `backend/main.py` 用 `MagnetEngine.latest()` + Runtime_Config 构造 `MagnetOverlayPayload`:含 `decay_points`、`dense_band_pull_share`、`cbbc_magnet_degraded`、`hsi_spot_stale`、`call_levels`、`histogram`、`recent_vetoes`
    - 在新结果发布或新增否决记录时推送
    - 降级 / `decay_points` 缺失时仍推送但保留状态字段,让前端据此隐藏 overlay
    - _Requirements: 8.1, 8.2, 8.6, 10.3_

  - [x] 9.3 为后端 API/WS 写测试
    - 在 `backend/test_main_magnet_overlay.py` 覆盖:`/api/state` 字段补全、`magnet_overlay` 推送在降级 / stale / 正常三态下的有效载荷
    - _Requirements: 8.1, 8.6, 10.3_

- [x] 10. 实现回测适配器
  - [x] 10.1 创建 `cbbc_backtest_adapter.py`
    - 在 `backend/cbbc_backtest_adapter.py` 实现 `CbbcBacktestAdapter`：`prepare_for_day(date)` 返回 `DayPreparation`（D-1 18:00 base snapshot + 有序 intraday_new_listings）；`at_replay_ts(ts, hsi_spot)` 推 `MagnetEngine`；`consult_extreme(side, hsi_spot, ts)` 复用 `MagnetSignalAdapter`；`summary()` 返回 `BacktestMagnetSummary`
    - 计数：`total_vetoed`、`vetoed_dense_band_above`、`vetoed_dense_band_below`、`control_total`（不论 layer 是否启用，每次 extreme 反转都 +1）、`cbbc_snapshot_missing_days`
    - 不在适配器中重新实现距离 / 密集带公式，直接复用 `MagnetEngine` 与 `MagnetSignalAdapter`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.7_

  - [x] 10.2 接入 `bt.py` 与 `backtest_service.run_backtest`
    - 在 `backend/bt.py` 与 `backend/backtest_service.py` 在每个交易日开始处调用 `prepare_for_day`，缺失 D-1 snapshot 时整日跳过 magnet 否决并 `cbbc_snapshot_missing_days += 1`、在该交易日日志中可观察标记 snapshot 缺失
    - 当 base snapshot 已加载但因 layer disabled / 参数无效 / 降级跳过 magnet 时，**不**增加 `cbbc_snapshot_missing_days`
    - 每条 1 分钟 K 线之前调用 `at_replay_ts`；进入 extreme 反转分支时调用 `consult_extreme`
    - 当 `ts >= listing_time` 时按序注入 `intraday_new_listings`，每条同日内仅注入一次
    - 在回测结束打印 / 返回 `summary()` 四项计数
    - _Requirements: 7.1, 7.2, 7.3, 7.5, 7.6, 7.7_

  - [x] 10.3 为回测适配器写单元测试
    - 在 `backend/test_cbbc_backtest_adapter.py` 覆盖：D-1 fallback、snapshot 缺失整日跳过、layer disabled 不计 missing、新发记录注入幂等、summary 计数正确、与实盘代码路径同源
    - _Requirements: 7.4, 7.5, 7.6, 7.7_

- [x] 11. 实现离线研究脚本
  - [x] 11.1 创建 `cbbc_research.py` CLI 与参数校验
    - 在 `backend/cbbc_research.py` 用 argparse 定义 `--start-date` / `--end-date`(YYYY-MM-DD)、`--decay-points`([1, 1000] int)、`--dense-band-threshold-pts`([1, 1000] int)
    - 任一参数不合法 → 5 秒内非零退出,stderr 输出字段名与拒绝原因,**不创建任何 CSV / md 文件**
    - _Requirements: 6.1, 6.11_

  - [x] 11.2 实现历史快照读取与每日聚合
    - 仅通过 `CbbcStorage.read_snapshot` 读取历史快照,禁止反推;调用方传入"今日存活集合"即触发 `no_reverse_deduction_allowed`
    - 枚举 `[start, end]` 港股交易日;非交易日由存储层报错跳过
    - 在 15:30:00–16:00:00 (HK) 按 1 分钟采样,计算当日均值 `Magnet_Bias` 与 `nearest_dense_band_distance_pts`;样本数 < 5 的当日丢弃
    - 在 09:30–16:00 内若有上市记录且其上市时刻 `Distance_Pts <= dense_band_threshold_pts`,写当日 `is_intraday_new_listing_near_money_day=True`
    - _Requirements: 6.2, 6.3, 6.4_

  - [x] 11.3 实现相关性、事件分组与上线门槛
    - 当日收盘前 `Magnet_Bias` vs 次日 HSI 收盘方向(涨/跌 → ±1)的 Pearson + Spearman(4 位小数)+ N + p_value
    - 按 `is_intraday_new_listing_near_money_day` 分两组,分别输出次日相对当日 HSI 收盘的最大顺势点数与最大逆势点数(保留 2 位)的中位数与 p75 + N
    - 有效交易日 < 60 → 不输出最终结论,写 `level=ERROR, source=cbbc_research, event=insufficient_trading_days` 并以非零码退出
    - _Requirements: 6.5, 6.6, 6.7_

  - [x] 11.4 输出 CSV / markdown 与可复现头
    - 写入 `backend/research/cbbc_magnet_<YYYYMMDDHHMMSS>.csv` 与 `.md`;目录不存在时按 `0o755` 创建
    - 文件头部记录:`decay_points`、`dense_band_threshold_pts`、`cbbc_dense_band_pull_share`、`start_date / end_date`、所用 `CbbcSnapshot` 文件清单的 SHA-256、脚本起止 HK 时间戳
    - 仅输出 CBBC + HSI 公开字段及聚合统计;**不**包含账户 / 订单 / 持仓 / 盈亏字段;**不**调用任何切换 `cbbc_magnet_layer_enabled` 的接口
    - _Requirements: 6.8, 6.9, 6.10, 11.5_

  - [x] 11.5 为研究脚本写单元测试
    - 在 `backend/test_cbbc_research.py` 覆盖:参数校验非零退出无副作用、生存偏差守卫触发、不足 60 个有效交易日的退出路径、相关性与分位数计算、可复现头哈希一致
    - _Requirements: 6.1, 6.2, 6.7, 6.8, 6.11_

- [x] 12. 实现前端磁吸 overlay
  - [x] 12.1 新增前端类型与 WebSocket 接收
    - 在 `frontend/src/types.ts` 增加 `MagnetOverlayPayload` 类型;`frontend/src/useWebSocket.ts` / `frontend/src/api.ts` 接收 `type=magnet_overlay` 消息
    - _Requirements: 8.1_

  - [x] 12.2 在 `PriceChart.tsx` 渲染 Call_Level 水平线与密集带阴影
    - 在 `frontend/src/components/PriceChart.tsx` 仅当后端推送中 `decay_points` 字段存在且 `cbbc_magnet_degraded=false` 时渲染
    - 仅画 `Distance_Pts <= decay_points` 的 Call_Level(线宽 1px);牛 / 熊证沿用 PriceChart 既有 `BULL_DOWN_COLOR` / `BEAR_DOWN_COLOR` 常量
    - 5pt 桶内 `pull_hkd / (pull_bull + pull_bear) >= 0.15` 时打 0.25 不透明度阴影,桶范围内水平线宽提升至 2px
    - 图例展示 direction + Call_Level;从收到推送到画面更新 ≤ 500ms
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

  - [x] 12.3 否决标记与降级文案
    - 收到 `recent_vetoes` 时在被否决 K 线时间位置上方画 ≥ 8px 标记,直至该 K 线滚出可见窗口
    - 当 `cbbc_magnet_degraded=true` 或 `decay_points` 字段缺失 → 隐藏所有水平线 / 阴影 / 否决标记,并在 PriceChart 顶部标题栏右侧显示文字 "CBBC 磁吸数据不可用"
    - 不显示账户 / 订单 / 持仓 / 盈亏字段
    - _Requirements: 8.5, 8.6, 11.6_

  - [x] 12.4 为前端 magnet overlay 编写组件测试
    - 覆盖:降级隐藏、密集带阈值切换、`decay_points` 过滤、否决标记生命周期
    - _Requirements: 8.4, 8.5, 8.6_

- [x] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标 `*` 的子任务为可选测试任务，可在 MVP 阶段跳过；非 `*` 子任务必做
- 设计文档无 "Correctness Properties" 章节，因此不引入属性测试，覆盖以单元 / 集成测试为主
- 全部任务严格围绕设计中定义的模块边界，禁止在回测 / 研究脚本里重写距离或密集带公式
- 默认 `cbbc_magnet_layer_enabled=false`，研究脚本不得切换；上线开关由 Requirement 9 的运行时配置接口人工触发
- 任一组件抛出未捕获异常都进入 fail-safe（不否决 + 主策略循环不中断），避免辅助模块拖垮主路径

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "4.1", "7.1", "12.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.1", "4.2", "6.1", "11.1"] },
    { "id": 2, "tasks": ["2.3", "3.2", "4.3", "6.2", "11.2"] },
    { "id": 3, "tasks": ["3.3", "4.4", "6.3", "11.3"] },
    { "id": 4, "tasks": ["4.5", "11.4", "12.2"] },
    { "id": 5, "tasks": ["4.6", "6.4", "7.2", "10.1", "11.5", "12.3"] },
    { "id": 6, "tasks": ["7.3", "10.2"] },
    { "id": 7, "tasks": ["7.4", "9.1", "10.3", "12.4"] },
    { "id": 8, "tasks": ["9.2"] },
    { "id": 9, "tasks": ["9.3"] }
  ]
}
```
