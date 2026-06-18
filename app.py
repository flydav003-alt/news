<style>
/* ---------- 全局變數 ---------- */
:root {
  --color-text-primary: #1A1A2E;
  --color-text-secondary: #64748B;
  --color-text-tertiary: #94A3B8;
  --color-background-body: #F8FAFC;
  --color-background-primary: #FFFFFF;
  --color-background-secondary: #F1F5F9;
  --color-border-light: #F1F5F9;
  --color-border-medium: #E2E8F0;
  --color-accent-blue: #2563EB;
  --color-bull: #DC2626;      /* 台股慣例：紅漲 */
  --color-bear: #059669;      /* 台股慣例：綠跌 */
  --color-bg-bull: #FEF2F2;
  --color-bg-bear: #ECFDF5;
  --font-size-xs: 11px;
  --font-size-sm: 13px;
  --font-size-md: 15px;
  --font-size-lg: 20px;
}

@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

html, body, [class*="css"], .stApp {
  font-family: 'Noto Sans TC', sans-serif !important;
  background-color: var(--color-background-body) !important;
  color: var(--color-text-primary) !important;
}

/* ── 完全移除 header & sidebar ── */
header[data-testid="stHeader"] { display: none !important; }
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] > div,
button[data-testid="collapsedControl"],
button[aria-label="Close sidebar"],
button[aria-label="Open sidebar"] { display: none !important; }

/* ── 消除上層 padding ── */
html, body { margin: 0 !important; padding: 0 !important; }
[data-testid="stAppViewContainer"] { padding-top: 0 !important; }
[data-testid="stAppViewContainer"] > section.main { padding-top: 0 !important; }
.main .block-container,
[data-testid="stMain"] .block-container {
  padding-top: 6px !important;
  padding-bottom: 20px !important;
  max-width: 1400px !important;
}

/* ══════════════════════════════════════
   ① Topbar：白底卡片、內嵌按鈕
══════════════════════════════════════ */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-background-primary);
  border: 1px solid var(--color-border-medium);
  border-radius: 10px;
  padding: 6px 14px;
  margin-bottom: 10px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.03);
  flex-wrap: wrap;
  gap: 6px;
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.topbar-logo {
  font-size: var(--font-size-sm);
  font-weight: 700;
  color: var(--color-text-primary);
  white-space: nowrap;
}
.topbar-status-ok {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-bear);
  background: var(--color-bg-bear);
  border: 1px solid #A7F0D0;
  border-radius: 20px;
  padding: 2px 10px;
}
.topbar-status-warn {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: #975A16;
  background: #FFFBEB;
  border: 1px solid #FDE68A;
  border-radius: 20px;
  padding: 2px 10px;
}
.topbar-time {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
  font-family: 'JetBrains Mono', monospace;
}
.topbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.topbar-right .stCheckbox { margin-bottom: 0 !important; }
.topbar-right .stCheckbox label { font-size: var(--font-size-xs) !important; }

/* ── 按鈕（配合 Topbar 高度） ── */
.stButton > button {
  border-radius: 7px;
  font-weight: 600;
  font-size: var(--font-size-xs);
  border: 1px solid var(--color-border-medium);
  background: var(--color-background-primary);
  color: var(--color-text-secondary);
  transition: all 0.15s;
  box-shadow: 0 1px 2px rgba(0,0,0,0.02);
  height: 32px !important;
  padding: 0 20px !important;
  white-space: nowrap !important;
}
.stButton > button:hover { background: var(--color-background-secondary); }
.stButton > button[kind="primary"] {
  background: linear-gradient(135deg, var(--color-bull) 0%, #B91C1C 100%);
  border-color: var(--color-bull);
  color: #FFFFFF;
  box-shadow: 0 2px 6px rgba(220,38,38,0.2);
}
.stButton > button[kind="primary"]:hover { opacity: 0.9; }

/* ══════════════════════════════════════
   ② Tabs：下底線風格（輕量）
══════════════════════════════════════ */
.stTabs [data-baseweb="tab-list"] {
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--color-border-medium);
  padding: 0;
  gap: 4px;
  margin-bottom: 12px;
}
.stTabs [data-baseweb="tab"] {
  font-size: var(--font-size-sm);
  font-weight: 500;
  padding: 8px 16px;
  border-radius: 0;
  color: var(--color-text-secondary);
  border-bottom: 2px solid transparent;
  background: transparent;
}
.stTabs [aria-selected="true"] {
  background: transparent !important;
  color: var(--color-text-primary) !important;
  border-bottom-color: var(--color-text-primary) !important;
  font-weight: 600;
}

/* ══════════════════════════════════════
   ③ Metric：輕量卡片
══════════════════════════════════════ */
[data-testid="metric-container"] {
  background: var(--color-background-primary);
  border: 1px solid var(--color-border-light);
  border-radius: 10px;
  padding: 12px 14px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.02);
}
[data-testid="stMetricLabel"] {
  color: var(--color-text-secondary) !important;
  font-size: var(--font-size-xs) !important;
  font-weight: 500 !important;
}
[data-testid="stMetricValue"] {
  color: var(--color-text-primary) !important;
  font-size: var(--font-size-lg) !important;
  font-weight: 600 !important;
}

/* ══════════════════════════════════════
   ④ 新聞卡片：保留區塊感，輕量化
══════════════════════════════════════ */
.nw {
  background: var(--color-background-primary);
  border: 1px solid var(--color-border-light);
  border-left: 4px solid var(--color-border-medium);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 6px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.02);
  transition: box-shadow 0.15s, transform 0.1s;
}
.nw:hover {
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
  transform: translateY(-1px);
}
.nw.bull  { border-left-color: var(--color-bull); }
.nw.bear  { border-left-color: var(--color-bear); }
.nw.geo   { border-left-color: #F59E0B; }

.nw-title {
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text-primary);
  line-height: 1.5;
  margin-bottom: 4px;
}
.nw-title a { color: var(--color-text-primary); text-decoration: none; }
.nw-title a:hover { color: var(--color-bull); }
.nw-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.nw-score-bull {
  font-size: var(--font-size-xs);
  font-weight: 700;
  color: var(--color-bull);
  background: var(--color-bg-bull);
  border-radius: 4px;
  padding: 1px 8px;
  font-family: 'JetBrains Mono', monospace;
}
.nw-score-bear {
  font-size: var(--font-size-xs);
  font-weight: 700;
  color: var(--color-bear);
  background: var(--color-bg-bear);
  border-radius: 4px;
  padding: 1px 8px;
  font-family: 'JetBrains Mono', monospace;
}
.nw-score-neu {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-text-tertiary);
  background: var(--color-background-secondary);
  border-radius: 4px;
  padding: 1px 8px;
  font-family: 'JetBrains Mono', monospace;
}
.nw-badge-ai {
  font-size: var(--font-size-xs);
  font-weight: 700;
  color: #975A16;
  background: #FFFBEB;
  border: 1px solid #FDE68A;
  border-radius: 4px;
  padding: 1px 6px;
}
.nw-badge-geo {
  font-size: var(--font-size-xs);
  font-weight: 700;
  color: #92400E;
  background: #FFFBEB;
  border: 1px solid #FDE68A;
  border-radius: 4px;
  padding: 1px 6px;
}
.nw-tick {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: #185FA5;
  background: #E6F1FB;
  border-radius: 4px;
  padding: 1px 6px;
  font-family: 'JetBrains Mono', monospace;
}
.nw-src { font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.nw-time { font-size: var(--font-size-xs); color: var(--color-text-tertiary); font-family: 'JetBrains Mono', monospace; }

/* ── 置頂高分新聞（強調卡片） ── */
.nw-pinned-bull {
  background: var(--color-bg-bull);
  border: 1px solid #FECACA;
  border-left: 5px solid var(--color-bull);
  border-radius: 10px;
  padding: 14px 18px;
  margin-bottom: 8px;
  box-shadow: 0 2px 8px rgba(220,38,38,0.06);
}
.nw-pinned-bear {
  background: var(--color-bg-bear);
  border: 1px solid #A7F0D0;
  border-left: 5px solid var(--color-bear);
  border-radius: 10px;
  padding: 14px 18px;
  margin-bottom: 8px;
  box-shadow: 0 2px 8px rgba(5,150,105,0.06);
}
.nw-pinned-label {
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  display: inline-block;
  padding: 2px 10px;
  border-radius: 4px;
  margin-bottom: 6px;
}
.nw-pinned-bull .nw-pinned-label { background: var(--color-bg-bull); color: var(--color-bull); }
.nw-pinned-bear .nw-pinned-label { background: var(--color-bg-bear); color: var(--color-bear); }
.nw-pinned-title {
  font-size: var(--font-size-md);
  font-weight: 700;
  line-height: 1.5;
  margin-bottom: 6px;
}
.nw-pinned-bull .nw-pinned-title a { color: #991B1B; text-decoration: none; }
.nw-pinned-bear .nw-pinned-title a { color: #065F46; text-decoration: none; }
.nw-pinned-score-bull { font-size: var(--font-size-sm); font-weight: 800; color: var(--color-bull); font-family: monospace; }
.nw-pinned-score-bear { font-size: var(--font-size-sm); font-weight: 800; color: var(--color-bear); font-family: monospace; }

/* ── AI 摘要展開 ── */
.nw-ai-box {
  margin-top: 8px;
  padding: 8px 12px;
  background: var(--color-background-secondary);
  border-radius: 6px;
  border-left: 3px solid var(--color-accent-blue);
  font-size: var(--font-size-xs);
  line-height: 1.7;
}
.nw-ai-toggle {
  font-size: var(--font-size-xs);
  color: #975A16;
  cursor: pointer;
  padding: 1px 8px;
  border: 1px solid #FDE68A;
  background: #FFFBEB;
  border-radius: 4px;
  font-weight: 600;
}
.nw-ai-box { display: none; }
.nw-ai-box.open { display: block; }

/* ── AI 總結卡（左側藍色 accent） ── */
.ai-card {
  background: var(--color-background-primary);
  border: 1px solid var(--color-border-light);
  border-left: 4px solid var(--color-accent-blue);
  border-radius: 0 10px 10px 0;
  padding: 16px 20px;
  margin-bottom: 6px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.02);
}
.ai-dir-bull { font-size: var(--font-size-md); font-weight: 700; color: var(--color-bull); }
.ai-dir-bear { font-size: var(--font-size-md); font-weight: 700; color: var(--color-bear); }
.ai-dir-neu  { font-size: var(--font-size-md); font-weight: 700; color: var(--color-text-secondary); }
.ai-tag-bull {
  background: var(--color-bg-bull);
  border: 1px solid #FECACA;
  border-radius: 5px;
  padding: 2px 12px;
  font-size: var(--font-size-xs);
  color: var(--color-bull);
  font-weight: 600;
}
.ai-tag-bear {
  background: var(--color-bg-bear);
  border: 1px solid #A7F0D0;
  border-radius: 5px;
  padding: 2px 12px;
  font-size: var(--font-size-xs);
  color: var(--color-bear);
  font-weight: 600;
}
.ai-tick-chip {
  background: var(--color-background-secondary);
  border: 1px solid var(--color-border-medium);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: var(--font-size-xs);
  font-family: 'JetBrains Mono', monospace;
  font-weight: 600;
}
.ai-body {
  font-size: var(--font-size-sm);
  line-height: 1.8;
  border-top: 1px solid var(--color-border-light);
  padding-top: 10px;
}

/* ── 其他元件（Geo、熱門股、空狀態） ── */
.geo-card {
  background: #FFFBEB;
  border: 1px solid #FDE68A;
  border-left: 4px solid #F59E0B;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 6px;
}
.tk-card {
  background: var(--color-background-primary);
  border: 1px solid var(--color-border-light);
  border-radius: 10px;
  padding: 12px;
  text-align: center;
  box-shadow: 0 1px 3px rgba(0,0,0,0.02);
  transition: box-shadow 0.15s;
}
.tk-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.06); }
.tk-code { font-size: var(--font-size-sm); font-weight: 700; font-family: monospace; }
.tk-bull { color: var(--color-bull); font-weight: 700; }
.tk-bear { color: var(--color-bear); font-weight: 700; }
.tk-neu  { color: var(--color-text-tertiary); font-weight: 600; }

.empty-box { text-align: center; padding: 40px 20px; color: var(--color-text-tertiary); }

/* ── Chip 篩選 ── */
.chip-bar { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.chip {
  font-size: var(--font-size-xs);
  font-weight: 600;
  padding: 4px 14px;
  border-radius: 20px;
  border: 1px solid var(--color-border-medium);
  background: var(--color-background-primary);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.chip.chip-bull.active { background: var(--color-bg-bull); border-color: #FECACA; color: var(--color-bull); }
.chip.chip-bear.active { background: var(--color-bg-bear); border-color: #A7F0D0; color: var(--color-bear); }
.chip.chip-all.active  { background: var(--color-text-primary); border-color: var(--color-text-primary); color: #FFFFFF; }

/* ── 日誌表格 ── */
.log-table { width: 100%; border-collapse: collapse; font-size: var(--font-size-xs); background: #FFF; border: 1px solid var(--color-border-light); border-radius: 8px; }
.log-table th { padding: 8px 12px; background: var(--color-background-secondary); border-bottom: 1px solid var(--color-border-medium); }
.log-table td { padding: 6px 12px; border-bottom: 1px solid var(--color-border-light); }
.log-ok   { background: var(--color-bg-bear); color: var(--color-bear); padding: 1px 8px; border-radius: 4px; font-weight: 700; }
.log-err  { background: var(--color-bg-bull); color: var(--color-bull); padding: 1px 8px; border-radius: 4px; font-weight: 700; }
</style>
