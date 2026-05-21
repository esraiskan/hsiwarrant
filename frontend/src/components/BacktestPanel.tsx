import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  DatePicker,
  Divider,
  Empty,
  Form,
  InputNumber,
  Progress,
  Row,
  Segmented,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  BarChartOutlined,
  CalendarOutlined,
  DollarOutlined,
  FundOutlined,
  PlayCircleOutlined,
  RiseOutlined,
  SwapOutlined,
  TrophyOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { colors } from '../theme';
import * as api from '../api';
import type {
  BacktestBreakdownRow,
  BacktestExtremeBranch,
  BacktestRequest,
  BacktestResult,
  BacktestStrategy,
} from '../types';

type BacktestFormValues = {
  period_mode: 'months' | 'date_range';
  months: string[];
  date_range?: [DateLike, DateLike];
  strategies: BacktestStrategy[];
  extreme_branches: BacktestExtremeBranch[];
  rsi_length: number;
  rsi_oversold: number;
  rsi_overbought: number;
  take_profit_points: number;
  stop_loss_points: number;
  fixed_win_hkd: number;
  fixed_loss_hkd: number;
  cum_trend_mode: 'strict_breadth' | 'market_log_breadth' | 'kline_proxy';
};

type DateLike = {
  format: (format: string) => string;
  diff: (value: DateLike, unit: string) => number;
};

function formatMonth(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}

function monthAgo(index: number) {
  const date = new Date();
  date.setDate(1);
  date.setMonth(date.getMonth() - index);
  return formatMonth(date);
}

const monthOptions = Array.from({ length: 8 }).map((_, index) => {
  const value = monthAgo(index);
  return { label: value, value };
});

const strategyOptions = [
  { label: '普通超買/超賣', value: 'normal' },
  { label: '極度策略', value: 'extreme' },
  { label: '放量動能', value: 'momentum' },
  { label: '累積趨勢', value: 'cum_trend' },
  { label: 'RSI背離', value: 'rsi_divergence' },
];

const branchOptions = [
  { label: 'B1 極度RSI+放量', value: 'b1_volume_extreme' },
  { label: 'B2 非常極端回抽', value: 'b2_very_extreme_pullback' },
  { label: 'B3 完成K補單', value: 'b3_completed_k' },
  { label: 'B4 上下影反轉', value: 'b4_shadow_reversal' },
];

const warningText: Record<string, string> = {
  order_book_not_replayed: '歷史 order-book 未重播，入場成交以 HSI 1M close 估算。',
  market_log_missing: '找不到 market_log.csv，累積趨勢無法使用歷史 breadth。',
  market_log_no_range_data: 'market_log.csv 沒有覆蓋本次日期範圍。',
  market_log_read_failed: 'market_log.csv 讀取失敗。',
  market_log_invalid: 'market_log.csv 欄位格式不完整。',
  breadth_fallback_to_proxy: '部分累積趨勢信號缺 breadth，已 fallback 到 K 線代理。',
  breadth_missing_skipped: '嚴格 breadth 模式下，缺 breadth 的累積趨勢信號已跳過。',
  cum_trend_kline_proxy: '累積趨勢使用 K 線代理模式，未套用歷史 breadth。',
};

/* ─── Styles ─── */

const sectionTitle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
  marginBottom: 12,
};

function panelStyle(): React.CSSProperties {
  return {
    background: colors.bgCard,
    border: `1px solid ${colors.border}`,
    borderRadius: 12,
  };
}

function statCardStyle(accent?: string): React.CSSProperties {
  return {
    ...panelStyle(),
    borderTop: accent ? `3px solid ${accent}` : undefined,
    transition: 'border-color 0.2s',
  };
}

function chartCardStyle(): React.CSSProperties {
  return {
    ...panelStyle(),
    overflow: 'hidden',
  };
}

function fmtMoney(value: number) {
  return `${value >= 0 ? '+' : ''}${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function breakdownColumns() {
  return [
    { title: '項目', dataIndex: 'label', key: 'label' },
    { title: '筆數', dataIndex: 'trades', key: 'trades', width: 70, align: 'center' as const },
    { title: 'W/L', key: 'wl', width: 80, align: 'center' as const, render: (_: unknown, row: BacktestBreakdownRow) => `${row.wins}/${row.losses}` },
    { title: '勝率', dataIndex: 'win_rate', key: 'win_rate', width: 80, align: 'center' as const, render: (v: number) => `${v.toFixed(1)}%` },
    {
      title: 'PnL',
      dataIndex: 'pnl_hkd',
      key: 'pnl_hkd',
      width: 100,
      align: 'right' as const,
      render: (v: number) => <span style={{ color: v >= 0 ? colors.up : colors.down, fontWeight: 600 }}>{fmtMoney(v)}</span>,
    },
  ];
}

function ChartEmpty() {
  return (
    <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暫無交易數據" />
    </div>
  );
}

export default function BacktestPanel() {
  const [form] = Form.useForm<BacktestFormValues>();
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<BacktestResult | null>(null);

  const periodMode = Form.useWatch('period_mode', form) || 'months';
  const strategies = Form.useWatch('strategies', form) || [];
  const hasExtreme = strategies.includes('extreme');

  const maxDailyPnl = useMemo(() => {
    if (!result?.daily.length) return 0;
    const row = result.daily.reduce((best, item) => (
      Math.abs(item.pnl_hkd) > Math.abs(best.pnl_hkd) ? item : best
    ), result.daily[0]);
    return row.pnl_hkd;
  }, [result]);

  const runBacktest = async () => {
    try {
      const values = await form.validateFields();
      if (!values.strategies?.length) {
        message.error('請至少選擇一個策略');
        return;
      }
      if (values.strategies.includes('extreme') && !values.extreme_branches?.length) {
        message.error('選擇極度策略時，請至少勾選一個分支');
        return;
      }
      const payload: BacktestRequest = {
        period_mode: values.period_mode,
        months: values.period_mode === 'months' ? values.months || [] : [],
        date_start: values.period_mode === 'date_range' ? values.date_range?.[0].format('YYYY-MM-DD') : undefined,
        date_end: values.period_mode === 'date_range' ? values.date_range?.[1].format('YYYY-MM-DD') : undefined,
        rsi_length: values.rsi_length,
        rsi_oversold: values.rsi_oversold,
        rsi_overbought: values.rsi_overbought,
        take_profit_points: values.take_profit_points,
        stop_loss_points: values.stop_loss_points,
        fixed_win_hkd: values.fixed_win_hkd,
        fixed_loss_hkd: values.fixed_loss_hkd,
        selection: {
          strategies: values.strategies,
          extreme_branches: values.extreme_branches || [],
        },
        cum_trend_mode: values.cum_trend_mode,
      };

      setLoading(true);
      setProgress(8);
      setElapsed(0);
      const started = Date.now();
      const timer = window.setInterval(() => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        setElapsed(seconds);
        setProgress((prev) => Math.min(92, prev + (prev < 60 ? 8 : 3)));
      }, 1000);
      try {
        const data = await api.runBacktest(payload);
        setResult(data);
        setProgress(100);
        message.success('回測完成');
      } finally {
        window.clearInterval(timer);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '回測失敗');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: '4px 0' }}>
      <Row gutter={[20, 20]}>
        {/* ─── 左侧：回测条件面板 ─── */}
        <Col xs={24} xl={7}>
          <Card
            title={
              <Space>
                <CalendarOutlined style={{ color: colors.primary }} />
                <span style={{ fontWeight: 600 }}>回測條件</span>
              </Space>
            }
            style={panelStyle()}
            bodyStyle={{ padding: '16px 20px' }}
          >
            <Form
              form={form}
              layout="vertical"
              size="small"
              initialValues={{
                period_mode: 'months',
                months: [monthAgo(1), monthAgo(0)],
                strategies: ['normal', 'extreme', 'momentum', 'cum_trend'],
                extreme_branches: branchOptions.map((item) => item.value),
                rsi_length: 6,
                rsi_oversold: 20,
                rsi_overbought: 80,
                take_profit_points: 20,
                stop_loss_points: 20,
                fixed_win_hkd: 400,
                fixed_loss_hkd: -400,
                cum_trend_mode: 'market_log_breadth',
              }}
            >
              {/* 时间设置 */}
              <div style={sectionTitle}>時間設定</div>
              <Form.Item name="period_mode" style={{ marginBottom: 12 }}>
                <Segmented block options={[
                  { label: '月份', value: 'months' },
                  { label: '日期範圍', value: 'date_range' },
                ]} />
              </Form.Item>

              {periodMode === 'months' ? (
                <Form.Item
                  name="months"
                  rules={[
                    { required: true, message: '請選擇月份' },
                    {
                      validator: (_, value: string[]) => (!value || value.length <= 2
                        ? Promise.resolve()
                        : Promise.reject(new Error('最多選 2 個月份'))),
                    },
                  ]}
                  style={{ marginBottom: 16 }}
                >
                  <Select mode="multiple" maxCount={2} options={monthOptions} placeholder="最多選 2 個月份" />
                </Form.Item>
              ) : (
                <Form.Item
                  name="date_range"
                  rules={[
                    { required: true, message: '請選擇日期範圍' },
                    {
                      validator: (_, value?: [DateLike, DateLike]) => {
                        if (!value) return Promise.resolve();
                        return value[1].diff(value[0], 'day') <= 62
                          ? Promise.resolve()
                          : Promise.reject(new Error('最多支援 62 日'));
                      },
                    },
                  ]}
                  style={{ marginBottom: 16 }}
                >
                  <DatePicker.RangePicker style={{ width: '100%' }} />
                </Form.Item>
              )}

              <Divider style={{ margin: '8px 0 16px', borderColor: colors.border }} />

              {/* 策略选择 */}
              <div style={sectionTitle}>策略選擇</div>
              <Form.Item name="strategies" style={{ marginBottom: 12 }}>
                <Checkbox.Group options={strategyOptions} />
              </Form.Item>

              <Form.Item name="extreme_branches" label="極度分支" style={{ marginBottom: 16 }}>
                <Checkbox.Group disabled={!hasExtreme} options={branchOptions} />
              </Form.Item>

              <Form.Item name="cum_trend_mode" label="累積趨勢 breadth" style={{ marginBottom: 16 }}>
                <Select options={[
                  { value: 'market_log_breadth', label: 'market_log 優先' },
                  { value: 'strict_breadth', label: '嚴格 breadth' },
                  { value: 'kline_proxy', label: 'K 線代理' },
                ]} />
              </Form.Item>

              <Divider style={{ margin: '8px 0 16px', borderColor: colors.border }} />

              {/* 参数设置 */}
              <div style={sectionTitle}>參數設定</div>
              <Row gutter={8}>
                <Col span={8}>
                  <Form.Item name="rsi_length" label="RSI">
                    <Select options={[6, 8, 10, 12, 14].map((v) => ({ value: v, label: `${v}` }))} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="rsi_oversold" label="超賣">
                    <InputNumber min={5} max={40} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="rsi_overbought" label="超買">
                    <InputNumber min={60} max={95} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={8}>
                <Col span={12}>
                  <Form.Item name="take_profit_points" label="止盈點">
                    <InputNumber min={1} max={200} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="stop_loss_points" label="止損點">
                    <InputNumber min={1} max={200} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={8}>
                <Col span={12}>
                  <Form.Item name="fixed_win_hkd" label="贏 HKD">
                    <InputNumber min={1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="fixed_loss_hkd" label="輸 HKD">
                    <InputNumber max={-1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>

              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                onClick={runBacktest}
                loading={loading}
                block
                style={{
                  height: 44,
                  borderRadius: 10,
                  fontWeight: 700,
                  fontSize: 15,
                  marginTop: 8,
                  background: `linear-gradient(135deg, ${colors.primary}, ${colors.info})`,
                  border: 'none',
                  boxShadow: '0 4px 12px rgba(59,130,246,0.3)',
                }}
              >
                開始回測
              </Button>

              {loading && (
                <div style={{ marginTop: 16 }}>
                  <Progress
                    percent={progress}
                    status="active"
                    strokeColor={{ from: colors.primary, to: colors.info }}
                    trailColor={colors.bgInput}
                  />
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    正在拉取 Futu K 線並重播策略，已用 {elapsed}s
                  </Typography.Text>
                </div>
              )}
            </Form>
          </Card>
        </Col>

        {/* ─── 右侧：结果展示区 ─── */}
        <Col xs={24} xl={17}>
          {!result ? (
            <Card
              style={{ ...panelStyle(), minHeight: 520 }}
              bodyStyle={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                minHeight: 480,
                gap: 16,
              }}
            >
              <FundOutlined style={{ fontSize: 48, color: colors.textMuted }} />
              <Empty description="設定條件後開始回測" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </Card>
          ) : (
            <Space direction="vertical" size={20} style={{ width: '100%' }}>
              {/* 警告提示 */}
              {result.warnings.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  icon={<WarningOutlined />}
                  message="資料提示"
                  description={result.warnings.map((item) => warningText[item] || item).join('；')}
                  style={{ borderRadius: 10 }}
                />
              )}

              {/* 统计卡片 */}
              <Row gutter={[12, 12]}>
                <Col xs={12} md={6}>
                  <Card style={statCardStyle(result.summary.pnl_hkd >= 0 ? colors.up : colors.down)} bodyStyle={{ padding: '16px 20px' }}>
                    <Statistic
                      title={<span style={{ fontSize: 12 }}><DollarOutlined /> 總 PnL</span>}
                      value={fmtMoney(result.summary.pnl_hkd)}
                      valueStyle={{ color: result.summary.pnl_hkd >= 0 ? colors.up : colors.down, fontSize: 22, fontWeight: 700 }}
                      suffix="HKD"
                    />
                  </Card>
                </Col>
                <Col xs={12} md={6}>
                  <Card style={statCardStyle(colors.primary)} bodyStyle={{ padding: '16px 20px' }}>
                    <Statistic
                      title={<span style={{ fontSize: 12 }}><TrophyOutlined /> 勝率</span>}
                      value={result.summary.win_rate}
                      precision={1}
                      suffix="%"
                      valueStyle={{ fontSize: 22, fontWeight: 700, color: colors.primary }}
                    />
                  </Card>
                </Col>
                <Col xs={12} md={6}>
                  <Card style={statCardStyle(colors.info)} bodyStyle={{ padding: '16px 20px' }}>
                    <Statistic
                      title={<span style={{ fontSize: 12 }}><SwapOutlined /> 交易筆數</span>}
                      value={result.summary.trades}
                      suffix={<span style={{ fontSize: 13, color: colors.textSecondary }}>{result.summary.wins}W / {result.summary.losses}L</span>}
                      valueStyle={{ fontSize: 22, fontWeight: 700 }}
                    />
                  </Card>
                </Col>
                <Col xs={12} md={6}>
                  <Card style={statCardStyle(colors.warning)} bodyStyle={{ padding: '16px 20px' }}>
                    <Statistic
                      title={<span style={{ fontSize: 12 }}><RiseOutlined /> 最大單日盈虧</span>}
                      value={fmtMoney(maxDailyPnl)}
                      suffix="HKD"
                      valueStyle={{ fontSize: 22, fontWeight: 700, color: maxDailyPnl >= 0 ? colors.up : colors.down }}
                    />
                  </Card>
                </Col>
              </Row>

              {/* 日度 PnL 图表（全宽） */}
              <Card
                title={<Space><BarChartOutlined style={{ color: colors.primary }} />日度 PnL</Space>}
                style={chartCardStyle()}
                bodyStyle={{ padding: '12px 16px 16px' }}
              >
                {result.daily.length ? (
                  <div style={{ height: 220 }}>
                    <ResponsiveContainer>
                      <BarChart data={result.daily} margin={{ top: 8, right: 12, bottom: 0, left: -4 }}>
                        <CartesianGrid stroke={colors.border} strokeDasharray="3 3" vertical={false} />
                        <XAxis dataKey="label" tick={{ fill: colors.textSecondary, fontSize: 10 }} axisLine={{ stroke: colors.border }} />
                        <YAxis tick={{ fill: colors.textSecondary, fontSize: 10 }} axisLine={{ stroke: colors.border }} />
                        <Tooltip
                          contentStyle={{ background: colors.bgCard, border: `1px solid ${colors.border}`, borderRadius: 8 }}
                          labelStyle={{ color: colors.text }}
                        />
                        <Bar dataKey="pnl_hkd" radius={[3, 3, 0, 0]}>
                          {result.daily.map((row) => <Cell key={row.key} fill={row.pnl_hkd >= 0 ? colors.up : colors.down} />)}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                ) : <ChartEmpty />}
              </Card>

              {/* 策略拆分 & 极度分支 表格 */}
              <Row gutter={[16, 16]}>
                <Col xs={24} lg={12}>
                  <Card title="策略拆分" style={chartCardStyle()} bodyStyle={{ padding: '8px 16px 16px' }}>
                    <Table
                      size="small"
                      rowKey="key"
                      pagination={false}
                      columns={breakdownColumns()}
                      dataSource={result.strategy_breakdown}
                    />
                  </Card>
                </Col>
                <Col xs={24} lg={12}>
                  <Card title="極度分支" style={chartCardStyle()} bodyStyle={{ padding: '8px 16px 16px' }}>
                    <Table
                      size="small"
                      rowKey="key"
                      pagination={false}
                      columns={breakdownColumns()}
                      dataSource={result.extreme_branch_breakdown}
                    />
                  </Card>
                </Col>
              </Row>

              {/* 交易明细 */}
              <Card title="交易明細" style={chartCardStyle()} bodyStyle={{ padding: '8px 16px 16px' }}>
                <Table
                  size="small"
                  rowKey={(row) => `${row.trade_date}-${row.entry_time}-${row.mode}-${row.entry}`}
                  dataSource={result.trades}
                  scroll={{ x: 980 }}
                  pagination={{ pageSize: 12, showSizeChanger: false, size: 'small' }}
                  columns={[
                    { title: '日期', dataIndex: 'trade_date', width: 100 },
                    { title: '時間', key: 'time', width: 110, render: (_, row) => `${row.entry_time}–${row.exit_time}` },
                    {
                      title: '方向',
                      dataIndex: 'side',
                      width: 75,
                      align: 'center',
                      render: (v) => <Tag color={v === 'bull' ? 'green' : 'red'} style={{ margin: 0 }}>{v === 'bull' ? '牛' : '熊'}</Tag>,
                    },
                    { title: '策略', dataIndex: 'mode', width: 100 },
                    {
                      title: '結果',
                      dataIndex: 'result',
                      width: 60,
                      align: 'center',
                      render: (v) => <Tag color={v === 'W' ? 'green' : 'red'} style={{ margin: 0, fontWeight: 600 }}>{v}</Tag>,
                    },
                    {
                      title: '點數',
                      dataIndex: 'points',
                      width: 80,
                      align: 'right',
                      render: (v) => <span style={{ color: v >= 0 ? colors.up : colors.down, fontWeight: 500 }}>{v.toFixed(1)}</span>,
                    },
                    {
                      title: 'PnL',
                      dataIndex: 'pnl_hkd',
                      width: 90,
                      align: 'right',
                      render: (v) => <span style={{ color: v >= 0 ? colors.up : colors.down, fontWeight: 600 }}>{fmtMoney(v)}</span>,
                    },
                    { title: '買入原因', dataIndex: 'desc', ellipsis: true },
                  ]}
                />
              </Card>
            </Space>
          )}
        </Col>
      </Row>
    </div>
  );
}
