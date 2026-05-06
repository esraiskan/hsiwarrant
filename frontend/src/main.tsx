import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';

const style = document.createElement('style');
style.textContent = `
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
    -webkit-font-smoothing: antialiased;
    background: #0b0e11;
    color: #e6edf3;
  }
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-thumb { background: #2a3548; border-radius: 3px; }
  ::-webkit-scrollbar-track { background: transparent; }

  /* Ant Design 深色覆盖 */
  .ant-card { background: #141821 !important; border-color: #1e2a3a !important; }
  .ant-card-head { border-color: #1e2a3a !important; color: #e6edf3 !important; }
  .ant-card-head-title { color: #e6edf3 !important; }
  .ant-table { background: transparent !important; }
  .ant-table-thead > tr > th { background: #1a1f2e !important; color: #8b949e !important; border-color: #1e2a3a !important; }
  .ant-table-tbody > tr > td { border-color: #1e2a3a !important; color: #e6edf3 !important; }
  .ant-table-tbody > tr:hover > td { background: #1a1f2e !important; }
  .ant-table-placeholder { background: transparent !important; }
  .ant-empty-description { color: #484f58 !important; }
  .ant-pagination .ant-pagination-item a { color: #8b949e !important; }
  .ant-pagination .ant-pagination-item-active { border-color: #3b82f6 !important; }
  .ant-pagination .ant-pagination-item-active a { color: #3b82f6 !important; }
  .ant-descriptions-item-label { color: #8b949e !important; background: #1a1f2e !important; }
  .ant-descriptions-item-content { color: #e6edf3 !important; background: #141821 !important; }
  .ant-descriptions-bordered .ant-descriptions-item-label,
  .ant-descriptions-bordered .ant-descriptions-item-content { border-color: #1e2a3a !important; }
  .ant-form-item-label > label { color: #8b949e !important; }
  .ant-input-number { background: #1e2433 !important; border-color: #2a3548 !important; color: #e6edf3 !important; }
  .ant-statistic-title { color: #8b949e !important; font-size: 12px !important; }
  .ant-statistic-content { color: #e6edf3 !important; }
  .ant-divider { border-color: #1e2a3a !important; }
  .ant-divider-inner-text { color: #8b949e !important; }
  .ant-spin-dot-item { background: #3b82f6 !important; }
  .ant-message-notice-content { background: #1a1f2e !important; color: #e6edf3 !important; border: 1px solid #2a3548 !important; }

  .trade-env-switch.ant-segmented {
    padding: 3px !important;
    border-radius: 8px !important;
    width: 174px;
    min-width: 174px;
    height: 34px;
  }
  .trade-env-switch .ant-segmented-group {
    gap: 2px;
    height: 26px;
    width: 100%;
  }
  .trade-env-switch .ant-segmented-item {
    flex: 1 1 0;
    min-width: 0;
    border-radius: 6px !important;
    color: #8b949e !important;
    transition: color 160ms ease, background 160ms ease !important;
  }
  .trade-env-switch .ant-segmented-item:hover {
    color: #e6edf3 !important;
    background: rgba(255,255,255,0.04) !important;
  }
  .trade-env-switch .ant-segmented-item-label {
    min-height: 26px !important;
    line-height: 26px !important;
    padding: 0 !important;
    font-size: 12px;
    font-weight: 700;
    text-align: center;
  }
  .trade-env-switch .ant-segmented-thumb,
  .trade-env-switch .ant-segmented-item-selected {
    border-radius: 6px !important;
    box-shadow: 0 8px 18px rgba(0,0,0,0.22), inset 0 1px 0 rgba(255,255,255,0.08) !important;
  }
  .trade-env-switch-simulate .ant-segmented-thumb,
  .trade-env-switch-simulate .ant-segmented-item-selected {
    background: linear-gradient(180deg, rgba(99,102,241,0.36), rgba(67,56,202,0.28)) !important;
    color: #c7d2fe !important;
  }
  .trade-env-switch-real .ant-segmented-thumb,
  .trade-env-switch-real .ant-segmented-item-selected {
    background: linear-gradient(180deg, rgba(255,77,106,0.34), rgba(185,28,28,0.26)) !important;
    color: #fecdd3 !important;
  }
  .trade-env-option {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    width: 100%;
  }
  .trade-env-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    display: inline-block;
  }
  .trade-env-dot-simulate {
    background: #818cf8;
    box-shadow: 0 0 8px rgba(129,140,248,0.55);
  }
  .trade-env-dot-real {
    background: #ff4d6a;
    box-shadow: 0 0 8px rgba(255,77,106,0.5);
  }
`;
document.head.appendChild(style);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
