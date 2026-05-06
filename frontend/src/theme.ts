/** 深色交易面板主题色 */
export const colors = {
  // 背景层级
  bg: '#0b0e11',
  bgCard: '#141821',
  bgCardHover: '#1a1f2e',
  bgInput: '#1e2433',
  bgHeader: '#0d1117',

  // 边框
  border: '#1e2a3a',
  borderLight: '#2a3548',

  // 文字
  text: '#e6edf3',
  textSecondary: '#8b949e',
  textMuted: '#484f58',

  // 涨跌色 (港股惯例: 绿涨红跌)
  up: '#00d4aa',       // 涨 - 青绿
  upBg: 'rgba(0,212,170,0.1)',
  down: '#ff4d6a',     // 跌 - 玫红
  downBg: 'rgba(255,77,106,0.1)',

  // 功能色
  primary: '#3b82f6',
  primaryHover: '#60a5fa',
  warning: '#f59e0b',
  info: '#6366f1',

  // 牛熊
  bull: '#00d4aa',
  bear: '#ff4d6a',
} as const;
