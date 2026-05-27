# Requirements Document

## Introduction

本特性为恒指牛熊证交易系统（`backend/strategy.py::HSIStrategyEngine`）新增 "CBBC 街货磁吸信号" 模块，作为现有 `extreme`（极度反转）入场分支的方向加分 / 否决层（不新增独立入场分支）。

模块基于 HKEX 公开数据：
- 每日 T+1 收市后发布的 CBBC outstanding（CSV / 网页）；
- 盘中实时发布的 CBBC 新上市公告（含发行人、收回价 Call_Level、上市股数、相关资产等字段）。

通过简化代理（`distance_pts = |Call_Level - HSI_Spot|`，`weight = max(0, 1 - distance_pts / DECAY_POINTS)`），按 dollar-weighted 聚合每个方向的 magnet pull，得到 `magnet_bias ∈ [-1, 1]`，用于：
1. 当 `extreme` 分支反转方向直接撞向 ≤ 150 点的密集街货带时否决入场；
2. 当 `extreme` 分支反转方向背离磁吸（即与磁吸方向一致）时维持原有入场逻辑。

模块 MVP 必须包含 "日内新发上市检测"，并提供离线研究脚本（`cbbc_research.py`），统计 ≥ 60 个交易日的相关性，作为是否启用的上线依据。模块需可在运行时一键开关（沿用 `enabled_strategies` 风格），并兼容 `bt.py` 回测使用历史 outstanding 快照。

## Glossary

- **HKEX**: 香港交易所及结算所有限公司（公开数据源）。
- **CBBC**: Callable Bull/Bear Contract，港股牛熊证。
- **Outstanding**: 已发行未售回的 CBBC 街货数量（按 issuer × code 维度）。
- **Call_Level**: CBBC 的强制收回价（亦称 "收回价"）。
- **HSI_Spot**: 恒生指数（HK.800000）实时点位。
- **Distance_Pts**: `abs(Call_Level - HSI_Spot)`，单位为指数点。
- **DECAY_POINTS**: 磁吸权重衰减距离（默认 300 点；可调；有效范围为开区间 (0.0, 10000.0]）。
- **Weight**: `max(0, 1 - Distance_Pts / DECAY_POINTS)`，单只 CBBC 的磁吸权重。
- **Notional_HKD**: 单只 CBBC 的名义街货市值，定义为 `outstanding_shares × ER_RATIO_OF_CBBC × Weight`（dollar-weighted 聚合的乘积项）。
- **Magnet_Pull_Bull**: 所有牛证按 `Notional_HKD` 求和的牵引强度（向上磁吸）。
- **Magnet_Pull_Bear**: 所有熊证按 `Notional_HKD` 求和的牵引强度（向下磁吸）。
- **Magnet_Bias**: 归一化偏向，`(Magnet_Pull_Bear - Magnet_Pull_Bull) / max(Magnet_Pull_Bear + Magnet_Pull_Bull, ε)`，∈ [-1, 1]；正值 = 向下磁吸更强（杀牛风险），负值 = 向上磁吸更强（杀熊风险）。
- **Dense_Band_Threshold_Pts**: 密集街货带否决阈值（默认 150 点；可调；有效范围 [10.0, 1000.0]）。
- **CBBC_Data_Service**: CBBC 数据抓取与持久化模块（每日 outstanding + 盘中新发）。
- **Magnet_Calculator**: 磁吸量化计算模块。
- **Magnet_Signal_Adapter**: 磁吸信号与 `HSIStrategyEngine` 的集成适配层。
- **Research_Script**: 离线研究脚本（建议命名 `backend/cbbc_research.py`）。
- **Backtest_Adapter**: `backend/bt.py` 的 CBBC 历史快照兼容层。
- **Frontend_Magnet_Overlay**: `frontend/src/components/PriceChart.tsx` 上的磁吸位水平线展示层。
- **Runtime_Config**: 现有 `runtime_config_store.py` 的运行时可调配置。
- **CBBC_Snapshot**: 某一交易日 / 时刻的 CBBC outstanding 全集（包含 issuer、code、Call_Level、outstanding_shares、ER_ratio、direction、listing_date、maturity_date、underlying）。
- **Survivorship_Bias**: 仅用 "今日仍存活" 的 CBBC 反推历史，会导致已被收回 / 到期的 CBBC 缺失，从而高估磁吸强度。
- **HK_Trading_Day**: 港股交易日，定义为非周末且不在 HKEX 公布的港股公众假期清单内的日期；时区统一为 Asia/Hong_Kong。
- **HK_Public_Holiday**: HKEX 公布的港股公众假期清单中的日期。
- **Morning_Session**: 港股早盘交易时段，09:30:00 至 12:00:00（Asia/Hong_Kong）。
- **Afternoon_Session**: 港股午盘交易时段，13:00:00 至 16:00:00（Asia/Hong_Kong）。
- **HKEX_Whitelist**: CBBC_Data_Service 允许访问的 HKEX 公开数据端点白名单，域名集合为 `{www.hkex.com.hk, www1.hkexnews.hk}`，且白名单内端点必须同时满足免登录与免付费订阅。

## Requirements

### Requirement 1: 每日 CBBC Outstanding 抓取

**User Story:** 作为系统运营者，我希望系统每个港股交易日 T+1 自动从 HKEX 拉取 CBBC outstanding 全量快照，以便磁吸计算使用最新街货分布。

#### Acceptance Criteria

1. WHEN 当前日期为港股交易日（即非周末且不在 HKEX 公布的港股公众假期清单内）且当前香港时间进入 18:00:00 至 23:59:59 的窗口，THE CBBC_Data_Service SHALL 从 HKEX 公开端点发起一次 CBBC outstanding 全量数据拉取，单次请求超时 30 秒。
2. IF 当前日期为周末或港股公众假期，THEN THE CBBC_Data_Service SHALL 跳过当日抓取任务，并在结构化日志中写入一条 `level=INFO, source=cbbc_data, event=daily_fetch_skipped_non_trading_day` 的记录。
3. WHEN 当日抓取任务已成功完成（即已生成 snapshot_date 等于当日的快照文件），THE CBBC_Data_Service SHALL 在同一交易日内的后续触发中直接返回已存在的快照而不重复写入，确保再次运行的幂等性。
4. THE CBBC_Data_Service SHALL 仅保留 underlying 字段为恒生指数（HSI）的 CBBC 记录，其余 underlying 的记录在解析阶段被丢弃。
5. THE CBBC_Data_Service SHALL 将每条记录解析为包含 issuer、code、Call_Level、outstanding_shares、ER_ratio、direction（取值仅限 bull 或 bear）、listing_date、maturity_date、underlying、snapshot_date 共 10 个字段的 CBBC_Snapshot 条目，且每个字段必须非空。
6. IF 单条记录在解析时存在缺失字段、字段类型不符或 direction 不属于 {bull, bear}，THEN THE CBBC_Data_Service SHALL 丢弃该条记录并在结构化日志中写入一条 `level=WARN, source=cbbc_data, event=record_parse_failed` 的记录，包含被拒绝记录的 code 与失败原因。
7. THE CBBC_Data_Service SHALL 将每日 CBBC_Snapshot 持久化到本地存储（建议 `backend/data/cbbc/outstanding_YYYYMMDD.parquet` 或同等格式），文件名以 snapshot_date 作为唯一标识，且不得覆盖任何 snapshot_date 不同的历史快照。
8. IF HKEX 端点返回 HTTP 非 2xx 响应、连接超时或整体解析失败，THEN THE CBBC_Data_Service SHALL 按指数退避（首次失败后 60 秒，第二次 180 秒，第三次 600 秒）最多重试 3 次。
9. IF 重试 3 次后仍失败，THEN THE CBBC_Data_Service SHALL 在结构化日志中写入一条 `level=ERROR, source=cbbc_data, event=daily_fetch_failed` 的记录，并保留上一交易日（即 snapshot_date 严格小于当日且距今最近的已存在快照）的 CBBC_Snapshot 作为当前可用快照。
10. IF 抓取请求成功返回但解析后得到的 HSI CBBC 记录数为 0，或少于上一交易日快照记录数的 50%，THEN THE CBBC_Data_Service SHALL 视为不完整数据，不写入新快照，并在结构化日志中写入一条 `level=ERROR, source=cbbc_data, event=daily_fetch_incomplete` 的记录，同时保留上一交易日的 CBBC_Snapshot 作为当前可用快照。

### Requirement 2: 盘中新发 CBBC 上市检测

**User Story:** 作为策略，我希望盘中新发的近价 CBBC 一上市即被纳入磁吸图，以便磁吸偏向能反映当日真实街货分布。

#### Acceptance Criteria

1. WHILE 当前 Asia/Hong_Kong 时间处于 Morning_Session（09:30:00 至 12:00:00）或 Afternoon_Session（13:00:00 至 16:00:00），且 `cbbc_intraday_polling_suspended=False`，THE CBBC_Data_Service SHALL 每 `cbbc_intraday_poll_interval_seconds`（缺省 60 秒）轮询 HKEX 新上市 CBBC 公告端点一次，单次请求超时 10 秒。
2. WHEN CBBC_Data_Service 检测到一条记录满足 `code` 不在当前内存 CBBC_Snapshot 中且 `listing_date` 等于当日的港股交易日，THE CBBC_Data_Service SHALL 在 90 秒内将该新发记录合并到当前内存中的 CBBC_Snapshot 中，并以 `code` 作为去重主键。
3. IF 新上市记录的 underlying 字段不等于 HSI，THEN THE CBBC_Data_Service SHALL 丢弃该记录，并在结构化日志中写入一条 `level=DEBUG, source=cbbc_data, event=intraday_new_listing_dropped_non_hsi` 的记录，包含字段 `code`、`underlying`。
4. WHERE 已合并的新上市 CBBC 在合并时刻 `Distance_Pts <= cbbc_magnet_decay_points`（取整后限定在 [50, 1000] 区间，缺省 300），THE CBBC_Data_Service SHALL 在结构化日志中写入一条 `level=INFO, source=cbbc_data, event=intraday_new_listing_near_money` 的记录，记录中至少包含字段 `event`、`code`、`Call_Level`、`direction`、`Distance_Pts`、`fetch_attempt_count`。
5. IF HKEX 公告端点的盘中轮询连续失败超过 5 分钟（失败枚举包含 HTTP 非 2xx、网络异常、请求超时、解析失败、域名解析失败、TLS 握手失败），THEN THE CBBC_Data_Service SHALL 在结构化日志中写入一条 `level=WARN, source=cbbc_data, event=intraday_polling_degraded` 的记录（至少包含 `event`、`fetch_attempt_count`、最近一次失败原因），暂停自动轮询，并将 `cbbc_intraday_polling_suspended` 设置为 `True`。
6. WHILE `cbbc_intraday_polling_suspended=True`，THE CBBC_Data_Service SHALL 不再尝试盘中合并任何新发记录，且不得自动恢复轮询；恢复轮询必须由运维通过 Runtime_Config 显式将 `cbbc_intraday_polling_suspended` 重置为 `false` 之后方可继续。
7. WHEN 进入任一 Morning_Session 或 Afternoon_Session 时段且 `cbbc_intraday_polling_suspended=True`，THE CBBC_Data_Service SHALL 在该交易日内仅写入一条 `level=INFO, source=cbbc_data, event=cbbc_intraday_polling_suspended_notice` 的结构化日志，提示轮询当前处于暂停状态需人工恢复。

### Requirement 3: 历史快照持久化与生存偏差防护

**User Story:** 作为研究者与回测使用者，我希望访问任意历史交易日的 "当时点" 存活 CBBC，以避免生存偏差扭曲研究结论。

#### Acceptance Criteria

1. WHEN 交易日 D 的当日 CBBC outstanding 抓取成功完成，THE CBBC_Data_Service SHALL 在 D 的 23:59:59（Asia/Hong_Kong）之前生成且仅生成一份 `snapshot_date=D` 的 CBBC_Snapshot 文件，文件名包含交易日日期（YYYYMMDD）。
2. THE CBBC_Data_Service SHALL 在 CBBC_Snapshot 中保留所有满足 `listing_date <= D` 且 `maturity_date >= D` 且当日尚未被收回的 CBBC 记录，且禁止任何对已生成快照的字段进行追溯改写。
3. WHEN Research_Script 或 Backtest_Adapter 请求日期 D 的 CBBC_Snapshot，THE CBBC_Data_Service SHALL 仅返回 `listing_date <= D` 且 `maturity_date >= D` 且当日尚未被收回的 CBBC 记录。
4. IF 调用方仅以 "今日仍存活的 CBBC 集合" 作为输入请求历史日期 D 的快照，THEN THE CBBC_Data_Service SHALL 拒绝该请求并向调用方返回 `no_reverse_deduction_allowed` 错误。
5. IF 某历史日期 D 的 CBBC_Snapshot 文件缺失，THEN THE CBBC_Data_Service SHALL 在请求时返回明确的 `snapshot_missing` 错误，而不是返回近似快照或最新快照。
6. IF 调用方传入的日期 D 为非交易日（即周末或港股公众假期），THEN THE CBBC_Data_Service SHALL 拒绝该请求并向调用方返回 `non_trading_day` 错误。
7. IF 调用方尝试以已存在的 `snapshot_date` 同名覆盖已生成的 CBBC_Snapshot 文件，THEN THE CBBC_Data_Service SHALL 拒绝写入并向调用方返回 `snapshot_immutable` 错误。

### Requirement 4: 磁吸量化模型

**User Story:** 作为策略开发者，我希望使用一套确定的、可调参数的磁吸量化公式，把街货分布映射成单一方向偏向标量。

#### Acceptance Criteria

1. THE Magnet_Calculator SHALL 对每条 CBBC 记录计算 `Distance_Pts = abs(Call_Level - HSI_Spot)`。
2. THE Magnet_Calculator SHALL 对每条 CBBC 记录计算 `Weight = max(0, 1 - Distance_Pts / DECAY_POINTS)`。
3. THE Magnet_Calculator SHALL 对每条 CBBC 记录计算 `Notional_HKD = outstanding_shares × ER_ratio_of_cbbc × Weight`。
4. THE Magnet_Calculator SHALL 将 `Notional_HKD` 按 direction = bull 求和得到 Magnet_Pull_Bull，按 direction = bear 求和得到 Magnet_Pull_Bear。
5. THE Magnet_Calculator SHALL 计算 `Magnet_Bias = (Magnet_Pull_Bear - Magnet_Pull_Bull) / max(Magnet_Pull_Bear + Magnet_Pull_Bull, 1.0)`。
6. THE Magnet_Calculator SHALL 将 Magnet_Bias 截断到区间 [-1, 1]。
7. THE Magnet_Calculator SHALL 将 DECAY_POINTS 暴露为 Runtime_Config 中名为 `cbbc_magnet_decay_points` 的浮点参数，缺省值为 300.0，有效范围为开区间 (0.0, 10000.0]。
8. WHEN HSI_Spot 在最近 1 分钟 K 线收盘后相对上次用于 Magnet_Bias 计算的取值变化绝对值大于 5.0 点，THE Magnet_Calculator SHALL 在 3 秒内重新计算 Magnet_Bias 并发布最新结果对象。
9. THE Magnet_Calculator SHALL 输出包含以下字段的结果对象：Magnet_Bias、Magnet_Pull_Bull、Magnet_Pull_Bear、最近 200 点内每个 5 点桶的 Notional_HKD 直方图、参与计算的记录数、被跳过的记录数、HSI_Spot 是否陈旧的布尔标志 `hsi_spot_stale`、生成时间戳。
10. IF 对 `cbbc_magnet_decay_points` 的更新值不在 (0.0, 10000.0] 区间内或为非有限浮点（NaN / ±Inf），THEN THE Magnet_Calculator SHALL 拒绝该次更新、保留旧值，并向调用方返回 `cbbc_magnet_decay_points_invalid` 错误。
11. IF 单条 CBBC 记录的 Call_Level、outstanding_shares、ER_ratio_of_cbbc 之中任一字段为空、为非有限浮点或为负值，或 direction 不属于 {bull, bear}，THEN THE Magnet_Calculator SHALL 跳过该条记录、将被跳过记录计数累加 1，并不让该记录参与 Magnet_Pull_Bull / Magnet_Pull_Bear 求和。
12. IF HSI_Spot 在最近 5 秒内未刷新，THEN THE Magnet_Calculator SHALL 停止发布新的结果对象，将上一次结果对象的 `hsi_spot_stale` 标志置为 `True`，并向调用方返回 `hsi_spot_stale` 错误。

### Requirement 5: 与 Extreme 分支的方向加分或否决集成

**User Story:** 作为策略，我希望 `extreme` 分支在产生反转信号前先咨询磁吸层，以避开撞向密集街货的反转。

#### Acceptance Criteria

1. WHEN HSIStrategyEngine 在 extreme 分支生成 BULL 反转信号，THE Magnet_Signal_Adapter SHALL 计算最近的 bear-direction Call_Level 与 HSI_Spot 的距离 `nearest_bear_distance_pts`；IF 全部 bear-direction 记录的 Call_Level 不可用，THEN 将 `nearest_bear_distance_pts` 设为 `null`。
2. IF extreme 反转方向为 BULL 且 `nearest_bear_distance_pts > Dense_Band_Threshold_Pts`，THEN THE Magnet_Signal_Adapter SHALL 不否决该入场，并向引擎返回 `vetoed_by_cbbc_magnet=False` 与原因码 `cbbc_dense_band_clear`。
3. IF extreme 反转方向为 BULL 且 `nearest_bear_distance_pts <= Dense_Band_Threshold_Pts` 且对应密集带的 Magnet_Pull_Bear 占当前总 magnet pull 的比例不低于 `cbbc_dense_band_pull_share`（缺省 0.40，有效范围 [0.0, 1.0]；超出范围或非有限浮点时回退到默认值并写入一条 `level=WARN, source=cbbc_magnet, event=cbbc_dense_band_pull_share_invalid_fallback` 的日志），THEN THE Magnet_Signal_Adapter SHALL 否决该入场，并向引擎返回 `vetoed_by_cbbc_magnet=True` 与原因码 `cbbc_dense_band_above`。
4. WHEN HSIStrategyEngine 在 extreme 分支生成 BEAR 反转信号，THE Magnet_Signal_Adapter SHALL 计算最近的 bull-direction Call_Level 与 HSI_Spot 的距离 `nearest_bull_distance_pts`；IF 全部 bull-direction 记录的 Call_Level 不可用，THEN 将 `nearest_bull_distance_pts` 设为 `null`。
5. IF extreme 反转方向为 BEAR 且 `nearest_bull_distance_pts <= Dense_Band_Threshold_Pts` 且对应密集带的 Magnet_Pull_Bull 占当前总 magnet pull 的比例不低于 `cbbc_dense_band_pull_share`，THEN THE Magnet_Signal_Adapter SHALL 否决该入场，并向引擎返回 `vetoed_by_cbbc_magnet=True` 与原因码 `cbbc_dense_band_below`。
6. IF extreme 反转方向与 Magnet_Bias 方向背离（BULL 信号且 `Magnet_Bias > 0`；或 BEAR 信号且 `Magnet_Bias < 0`），THEN THE Magnet_Signal_Adapter SHALL 不否决该入场，并向引擎返回 `magnet_aligned_against_reversal=True`；当 `Magnet_Bias = 0` 时不视为背离。
7. THE Magnet_Signal_Adapter SHALL 将 Dense_Band_Threshold_Pts 暴露为 Runtime_Config 中名为 `cbbc_dense_band_threshold_pts` 的浮点参数，缺省值为 150.0，有效范围 [10.0, 1000.0]；超出范围或非有限浮点时回退到默认值并写入一条 `level=WARN, source=cbbc_magnet, event=cbbc_dense_band_threshold_pts_invalid_fallback` 的日志。
8. THE Magnet_Signal_Adapter SHALL 在每次咨询完成时恰好写入一条 `event=cbbc_magnet_consult` 的结构化日志，至少包含字段：`event`、信号方向、`nearest_bull_distance_pts`、`nearest_bear_distance_pts`、Magnet_Bias、`magnet_available`（磁吸可用性布尔标志）、`magnet_aligned_against_reversal`、是否否决与原因码。
9. THE Magnet_Signal_Adapter SHALL 不修改除 extreme 分支以外的任何入场分支（normal、momentum、cum_trend、rsi_divergence）的判定结果。
10. IF extreme 反转方向为 BEAR 且 `nearest_bull_distance_pts > Dense_Band_Threshold_Pts`，THEN THE Magnet_Signal_Adapter SHALL 不否决该入场，并向引擎返回 `vetoed_by_cbbc_magnet=False` 与原因码 `cbbc_dense_band_clear`。
11. IF HSI_Spot、Magnet_Bias 或 Call_Level 中的任一项不可用，或当前 `Magnet_Pull_Bull + Magnet_Pull_Bear = 0`，THEN THE Magnet_Signal_Adapter SHALL 不否决该入场，并写入一条 `event=cbbc_magnet_consult_unavailable` 的结构化日志（fail-safe）。
12. IF 距离已落在 Dense_Band_Threshold_Pts 以内但对应密集带的 pull share 严格小于 `cbbc_dense_band_pull_share`，THEN THE Magnet_Signal_Adapter SHALL 不否决该入场，并向引擎返回 `vetoed_by_cbbc_magnet=False` 与原因码 `cbbc_dense_band_pull_share_below`。

### Requirement 6: 离线研究脚本与上线门槛

**User Story:** 作为策略所有者，我希望在启用磁吸否决前先看到统计证据，以便决定是否值得上线。

#### Acceptance Criteria

1. THE Research_Script SHALL 接受 `--start-date`、`--end-date`、`--decay-points`、`--dense-band-threshold-pts` 命令行参数；其中 `--start-date` 与 `--end-date` 必须为 `YYYY-MM-DD` 格式，`--decay-points` 必须为 [1, 1000] 的整数，`--dense-band-threshold-pts` 必须为 [1, 1000] 的整数。
2. THE Research_Script SHALL 仅使用 Requirement 3 提供的历史 CBBC_Snapshot，不得使用今日存活集合反推。
3. THE Research_Script SHALL 对覆盖区间内的每个港股交易日，按 Asia/Hong_Kong 时间收盘窗口 15:30:00 至 16:00:00（含两端）计算当日平均 Magnet_Bias 与平均 `nearest_dense_band_distance_pts`；当日有效样本数严格小于 5 时，该日不计入相关性数据集。
4. THE Research_Script SHALL 对每个交易日识别 "近距离新发事件"，定义为当日盘中（Asia/Hong_Kong 09:30:00 至 16:00:00）存在新发 CBBC 且其在上市时刻 `Distance_Pts <= Dense_Band_Threshold_Pts`，并在每日输出中写入布尔字段 `is_intraday_new_listing_near_money_day`。
5. THE Research_Script SHALL 输出当日收盘前 Magnet_Bias 与次日 HSI 收盘方向（涨 / 跌）的相关性，包含 Pearson 与 Spearman 各一项（各保留 4 位小数），并附样本数 `N` 与 `p_value`。
6. THE Research_Script SHALL 输出 "近距离新发事件出现日" 与 "无该事件日" 在次日的最大顺势点数与最大逆势点数（相对当日 HSI 收盘价的最大有利 / 不利差，保留 2 位小数）的中位数与 75% 分位数，并附样本数 `N`。
7. IF 覆盖区间内有效港股交易日严格小于 60，THEN THE Research_Script SHALL 不输出最终结论，写入一条 `level=ERROR, source=cbbc_research, event=insufficient_trading_days` 的日志，并以非零退出码终止。
8. WHERE 用户希望复现结果，THE Research_Script SHALL 在结果文件头部记录使用的 DECAY_POINTS、Dense_Band_Threshold_Pts、`cbbc_dense_band_pull_share`、研究区间起止日期、CBBC_Snapshot 文件清单的 SHA-256 哈希值，以及脚本开始时间戳与结束时间戳（Asia/Hong_Kong 时间）。
9. THE Research_Script SHALL 将所有结果写入 CSV 与一份 markdown 摘要，路径分别为 `backend/research/cbbc_magnet_<timestamp>.csv` 和 `backend/research/cbbc_magnet_<timestamp>.md`，其中 `<timestamp>` 为 `YYYYMMDDHHMMSS` 格式（Asia/Hong_Kong 时间）；目录不存在时按 mode `0o755` 自动创建。
10. THE Research_Script SHALL 不调用任何用于切换 `cbbc_magnet_layer_enabled` 的接口；启用动作仅可通过 Requirement 9 描述的运行时开关由人工或其他系统显式触发。
11. IF 任一命令行参数不符合第 1 条所规定的格式或区间，THEN THE Research_Script SHALL 在 5 秒内以非零退出码终止，向 stderr 输出错误说明（包含字段名与拒绝原因），且不得写入任何 CSV 或 markdown 文件。

### Requirement 7: bt.py 回测兼容

**User Story:** 作为回测开发者，我希望在 `bt.py` 历史回放中也能复现磁吸否决逻辑，以验证策略效果。

#### Acceptance Criteria

1. WHEN 回放进入交易日 D 的任一 1 分钟 K 线之前，THE Backtest_Adapter SHALL 完成该 K 线所对应历史 CBBC_Snapshot 的加载，使其在该 K 线进入策略评估前已可在当日内存中读取。
2. WHILE 回放时间戳处于交易日 D 当日 09:30（含）至 18:00（不含）之间的 outstanding 发布时点之前，THE Backtest_Adapter SHALL 使用 D-1 交易日 18:00 已发布的 CBBC_Snapshot 作为该时段的基础快照。
3. WHERE 当日基础 CBBC_Snapshot 包含非空的 `intraday_new_listings` 字段，WHEN 回放时间戳达到或超过其中某条新发记录的 `listing_time`，THE Backtest_Adapter SHALL 将该条记录注入到当日内存中的 snapshot，且每条记录在同一交易日内仅注入一次。
4. THE Backtest_Adapter SHALL 通过直接调用与实盘相同代码路径的 Magnet_Calculator 与 Magnet_Signal_Adapter 完成磁吸否决判断，不得在回测代码中重新实现磁吸距离或密集带的计算公式。
5. IF 回放交易日 D 在 09:30-18:00 时段所需的 D-1 CBBC_Snapshot 文件在持久化存储中不存在或读取失败，THEN THE Backtest_Adapter SHALL 在该交易日跳过所有磁吸否决判断（视为模块停用），将 `cbbc_snapshot_missing_days` 计数加 1，并在该交易日的回测日志中以可观察方式标记 snapshot 缺失。
6. IF 回测中因 CBBC_Snapshot 已成功加载但磁吸模块运行时被关闭、参数无效或其他非快照缺失原因导致跳过磁吸否决，THEN THE Backtest_Adapter SHALL 不增加 `cbbc_snapshot_missing_days` 计数。
7. WHEN 回测运行结束，THE Backtest_Adapter SHALL 在结果摘要中输出以下四项非负整数计数：被磁吸否决的入场总次数、按密集带原因否决的入场次数、按距离原因否决的入场次数、以及在假设磁吸否决全程未启用条件下产生的对照入场总次数。

### Requirement 8: 前端磁吸位展示

**User Story:** 作为交易员，我希望在价格图上直接看到附近的磁吸位与密集带，以便目视确认策略判断。

#### Acceptance Criteria

1. WHEN Frontend_Magnet_Overlay 收到后端 WebSocket `magnet_overlay` 推送，THE Frontend_Magnet_Overlay SHALL 在 500 毫秒内于 `frontend/src/components/PriceChart.tsx` 的价格主图上重绘磁吸水平线，水平线位置为各 CBBC 的 Call_Level，线宽 1 像素。
2. THE Frontend_Magnet_Overlay SHALL 仅绘制 `Distance_Pts <= decay_points` 的 Call_Level，其中 `decay_points` 取自后端推送的 `decay_points` 字段；IF `decay_points` 字段缺失，THEN 不绘制任何磁吸水平线。
3. THE Frontend_Magnet_Overlay SHALL 使用与 direction 对应的色系：bull-direction Call_Level 使用 PriceChart 既有牛证下跌色（与现有图表常量一致），bear-direction Call_Level 使用 PriceChart 既有熊证下跌色（与现有图表常量一致），并在图例中同时展示 direction 与 Call_Level。
4. THE Frontend_Magnet_Overlay SHALL 将每 5 点价格桶（桶下沿为 5 的整数倍）内 Notional_HKD 占当前 `Magnet_Pull_Bull + Magnet_Pull_Bear` 比例不低于 0.15 的桶标记为 "密集带"，并对该桶范围同时执行：阴影不透明度 0.25、桶范围内水平线线宽提升至 2 像素。
5. WHEN HSIStrategyEngine 在 extreme 分支因磁吸否决了一次入场（`vetoed_by_cbbc_magnet=True`），THE Frontend_Magnet_Overlay SHALL 在被否决的 K 线时间位置上方显示一个否决标记（尺寸 ≥ 8 像素），并持续显示直到该 K 线滚出可见窗口。
6. WHERE 后端推送中 `cbbc_magnet_degraded=true` 或 `decay_points` 字段缺失，THE Frontend_Magnet_Overlay SHALL 隐藏所有磁吸水平线、密集带阴影与否决标记，并在 PriceChart 顶部标题栏右侧显示文字 "CBBC 磁吸数据不可用"。

### Requirement 9: 运行时开关与配置持久化

**User Story:** 作为运营者，我希望在运行时一键启用 / 关闭磁吸模块、调整阈值，并把改动持久化以跨重启生效。

#### Acceptance Criteria

1. THE Runtime_Config SHALL 在现有 `enabled_strategies` 之外新增名为 `cbbc_magnet_layer_enabled` 的布尔开关，缺省值为 `false`。
2. WHEN `cbbc_magnet_layer_enabled` 为 `false`，THE Magnet_Signal_Adapter SHALL 不向 HSIStrategyEngine 返回任何否决，并视为模块停用。
3. THE Runtime_Config SHALL 暴露以下浮点参数及缺省值与有效范围：`cbbc_magnet_decay_points` 缺省 300.0、有效范围 (0.0, 10000.0]；`cbbc_dense_band_threshold_pts` 缺省 150.0、有效范围 [10.0, 1000.0]；`cbbc_dense_band_pull_share` 缺省 0.40、有效范围 [0.0, 1.0]；`cbbc_intraday_poll_interval_seconds` 缺省 60.0、有效范围 [10.0, 600.0]。
4. THE Runtime_Config SHALL 暴露名为 `cbbc_intraday_polling_suspended` 的布尔字段，缺省值为 `false`，用于运维显式恢复 Requirement 2 中被暂停的盘中轮询。
5. WHEN 用户通过现有运行时配置接口修改上述任一参数，THE Runtime_Config SHALL 使用原子写入策略（先写入临时文件 `backend/runtime_config.json.tmp` 再原子重命名到 `backend/runtime_config.json`）在 1 秒内完成持久化，并通知 Magnet_Calculator、Magnet_Signal_Adapter、CBBC_Data_Service 应用新值。
6. IF 任一浮点参数被设置为非有限浮点（NaN / ±Inf）、超出第 3 条规定的有效范围或类型不符，THEN THE Runtime_Config SHALL 拒绝该次更新、保留旧值，并向调用方返回包含字段名与拒绝原因的错误说明。
7. WHEN HSIStrategyEngine 启动时读取持久化配置，THE HSIStrategyEngine SHALL 应用 `cbbc_magnet_layer_enabled`、`cbbc_intraday_polling_suspended` 与上述浮点参数到 Magnet_Calculator、Magnet_Signal_Adapter 与 CBBC_Data_Service。
8. IF 持久化写入过程中发生磁盘已满、权限拒绝或其他 IO 异常，THEN THE Runtime_Config SHALL 回滚到旧值，并向调用方返回包含失败原因的错误说明。
9. IF 启动时 `backend/runtime_config.json` 不存在、JSON 解析失败或字段类型不匹配，THEN THE Runtime_Config SHALL 回退到内置默认值、写入一条 `level=WARN, source=runtime_config, event=config_corrupt_fallback_defaults` 的结构化日志，并继续启动。

### Requirement 10: 数据失败降级

**User Story:** 作为运营者，我希望抓不到 CBBC 数据时主策略继续按原有 5 类入场分支正常运行，而不是因辅助模块失败而停止交易。

#### Acceptance Criteria

1. IF 当前 wall-clock 时间（Asia/Hong_Kong 时区）减去 CBBC_Snapshot 最近一次成功刷新时间戳所得差值严格大于 36 小时，THEN THE Magnet_Signal_Adapter SHALL 在下一次被 HSIStrategyEngine 调用时进入降级模式，并将 StrategyState 上的 `cbbc_magnet_degraded` 设置为 `True`。
2. WHILE Magnet_Signal_Adapter 处于降级模式（`cbbc_magnet_degraded=True`），THE Magnet_Signal_Adapter SHALL 对每次调用返回 magnet_bias=0 的中性结果、不向 HSIStrategyEngine 返回任何否决信号，并不阻塞 HSIStrategyEngine 对原有 5 类入场分支的评估流程。
3. WHEN Magnet_Signal_Adapter 从正常模式切换到降级模式的状态转换发生时，THE Magnet_Signal_Adapter SHALL 写入恰好一条 `level=WARN, source=cbbc_magnet, event=degraded_no_data` 的结构化日志，并在 StrategyState 上将 `cbbc_magnet_degraded` 暴露为 `True`。
4. WHEN CBBC_Snapshot 恢复刷新（最近一次 outstanding 或盘中合并刷新时间戳与当前 Asia/Hong_Kong wall-clock 时间之差小于或等于 36 小时），THE Magnet_Signal_Adapter SHALL 依次执行显式恢复初始化的子步骤：(a) 重新加载最新 CBBC_Snapshot；(b) 对当前 HSI_Spot 重算一次 Magnet_Bias；并且仅在 (a) 与 (b) 均未抛出异常且产生有效结果后，才执行 (c) 写入恰好一条 `level=INFO, source=cbbc_magnet, event=recovery_initialized` 的结构化日志、(d) 将 StrategyState 上的 `cbbc_magnet_degraded` 切换为 `False`。
5. IF 显式恢复初始化的子步骤 (a) 或 (b) 中任一项抛出异常或在 5 秒内未返回结果，THEN THE Magnet_Signal_Adapter SHALL 保持 `cbbc_magnet_degraded=True`、写入一条 `level=WARN, source=cbbc_magnet, event=recovery_failed` 的结构化日志，并在下一次 CBBC_Snapshot 刷新事件到达时再次尝试执行恢复初始化。
6. IF CBBC_Data_Service、Magnet_Calculator 或 Magnet_Signal_Adapter 中任一组件在被调用时抛出未捕获异常，THEN THE HSIStrategyEngine SHALL 捕获该异常、将 StrategyState 上的 `cbbc_magnet_degraded` 设置为 `True`、继续执行原有 5 类入场分支的评估，并维持主策略循环不中断。

### Requirement 11: 安全与合规

**User Story:** 作为合规负责人，我希望模块只使用 HKEX 公开数据，并且任何研究 / 日志 / 前端输出中都不暴露账户或下单敏感信息。

#### Acceptance Criteria

1. THE CBBC_Data_Service SHALL 仅访问预先配置的 HKEX 公开数据端点白名单（域名为 `www.hkex.com.hk` 与 `www1.hkexnews.hk`），且白名单内端点必须同时满足免登录与免付费订阅；任何不在白名单内或需鉴权 / 付费的端点一律视为禁止访问。
2. IF 抓取目标端点要求鉴权（包括 Cookie 会话、API Key、Basic Auth、OAuth Token、HTTP 401/403 响应）或付费订阅，THEN THE CBBC_Data_Service SHALL 在发起业务请求前阻断该次访问、不写入任何抓取结果到持久化存储，并在结构化日志中写入一条 `level=ERROR, source=cbbc_data, event=blocked_paywalled_or_authenticated_endpoint` 的记录，记录中仅包含端点 URL 与阻断原因，不得包含任何凭据。
3. IF 抓取目标 URL 的域名或路径不在第 1 条所述白名单内，THEN THE CBBC_Data_Service SHALL 阻断该次访问、放弃本次抓取结果，并在结构化日志中写入一条 `level=ERROR, source=cbbc_data, event=blocked_non_whitelisted_endpoint` 的记录，记录中包含被拒域名与路径但不得包含任何凭据。
4. THE CBBC_Data_Service SHALL 不在任何持久化文件、结构化日志、调试日志或前端推送中包含以下敏感字段中的任何一项：账户号、API Key、访问令牌（Access Token / Refresh Token / Session Cookie）、订单 ID、委托明细（订单价、订单量、买卖方向、订单状态）、成交价、成交量、持仓成本、未实现 / 已实现盈亏。
5. THE Research_Script SHALL 在生成的 CSV 与 markdown 中仅包含 CBBC 公开字段、HSI 公开行情字段、以及基于上述公开字段计算的聚合统计量；不得包含本系统任何账户号、订单 ID、订单价 / 量 / 方向 / 状态、成交价 / 量、持仓、持仓成本或盈亏字段。
6. THE Frontend_Magnet_Overlay SHALL 仅显示 Call_Level、direction、密集带与是否否决标记这四类字段；不得在磁吸图层显示任何账户号、订单 ID、订单价 / 量 / 方向 / 状态、成交价 / 量、持仓或盈亏数据。
7. THE CBBC_Data_Service SHALL 在任意 60 秒滚动窗口内，对同一端点（按完整 URL 计）的请求次数不超过 6 次。
8. IF 任意 60 秒滚动窗口内对同一端点的请求次数已达到 6 次的上限，THEN THE CBBC_Data_Service SHALL 延后该端点的下一次请求至当前滚动窗口内请求计数回落到 6 次以下，并在结构化日志中写入一条 `level=WARN, source=cbbc_data, event=rate_limit_deferred` 的记录，记录中包含端点 URL 与下次允许请求的相对延迟秒数。
