# 📈 FinNews AI — 台灣財經新聞智慧分析系統

## 新聞來源（全中文，全部 RSS）
| 來源 | 類別 | 特色 |
|------|------|------|
| 鉅亨網-台股 | 台股 | 台股主力，即時性強 |
| 鉅亨網-美股 | 美股 | 美股中文報導 |
| 鉅亨網-總覽 | 財經 | 綜合財經 |
| MoneyDJ-台股 | 台股 | 深度分析 |
| MoneyDJ-國際 | 國際 | 國際財經中文 |
| Yahoo奇摩股市 | 財經 | 綜合財經 |
| 經濟日報 | 財經 | 大報財經新聞 |
| 工商時報 | 產業 | 產業深度 |
| 科技新報 | 科技 | 科技產業 |
| 聯合報財經 | 財經 | 財經綜合 |

## 核心功能
- 📊 **總覽 Dashboard**：情緒圓餅圖、類股排行、即時篩選
- 🔥 **熱門股票**：依新聞次數排行，卡片式顯示，可點擊連結報價
- ⚑ **地緣政治警示**：自動偵測戰爭/衝突/制裁關鍵字
- 📋 **新聞列表**：多維度篩選、搜尋、CSV 匯出
- 🔍 **個股聚焦**：支援代碼或中文公司名稱搜尋（台積電/2330/NVDA/輝達）
- 🏭 **類股排行**：12個板塊情緒熱度圖，可點擊查看相關新聞
- ⚙️ **設定**：頻率調整、來源開關、自訂情緒詞典

## 強化代碼抽取
系統內建 **300+ 台股公司名稱↔代碼對照表**，中文新聞也能準確抽出代碼：
- 台積電 → 2330 ↗ Yahoo 台股報價
- 輝達 → NVDA ↗ Yahoo Finance
- 廣達 → 2382（AI/伺服器板塊）

---

## 🚀 部署到 Streamlit Cloud（免費）

### Step 1：上傳到 GitHub
```bash
# 建立新 repo 後
git init
git add .
git commit -m "init FinNews AI"
git push origin main
```

### Step 2：部署
1. 去 [share.streamlit.io](https://share.streamlit.io)
2. 用 GitHub 帳號登入 → New app
3. 選 Repository → Branch: `main` → Main file: `app.py`
4. Deploy！約 2 分鐘完成

---

## 本機執行
```bash
pip install -r requirements.txt
streamlit run app.py
```
