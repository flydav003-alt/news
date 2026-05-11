# 📈 FinNews AI — 財經新聞智慧分析系統

## 新聞來源（5個，全部 RSS）
| 來源 | 類別 | 語言 |
|------|------|------|
| Yahoo Finance | 美股主力 | 英文 |
| Reuters Business | 財經 | 英文 |
| Reuters World | 地緣政治 | 英文 |
| CNBC Top News | 科技財經 | 英文 |
| 鉅亨網台股 | 台股 | 中文 |
| 鉅亨網美股 | 美股中文 | 中文 |
| BBC World News | 戰爭/衝突 | 英文 |
| BBC Business | 財經 | 英文 |

## 功能
- 📊 總覽 Dashboard（情緒圓餅圖、類股排行）
- ⚑ 地緣政治/戰爭獨立警示頁面
- 📋 全部新聞列表（篩選、搜尋、匯出 CSV）
- 🔍 個股聚焦（輸入 2330 或 NVDA）
- 🏭 類股排行（含平均情緒熱度）
- ⚙️ 設定（頻率、來源開關、自訂情緒詞典）
- 三層去重（Hash + 標題相似度 + 時間窗口）

---

## 🚀 部署到 Streamlit Cloud（免費，10分鐘完成）

### 第一步：上傳到 GitHub
1. 去 [github.com](https://github.com) 建立新 Repository，名稱如 `finnews-ai`
2. 把這個資料夾所有檔案上傳（Add file → Upload files）

### 第二步：部署到 Streamlit Cloud
1. 去 [share.streamlit.io](https://share.streamlit.io)
2. 用 GitHub 帳號登入
3. 點「New app」
4. 選你的 Repository → Branch: `main` → Main file: `app.py`
5. 點「Deploy!」等約 2 分鐘

### 第三步：完成！
- 取得固定網址如 `https://yourname-finnews-ai.streamlit.app`
- 手機電腦都能開
- 點側邊欄「立即抓取新聞」開始使用

---

## 本機執行（測試用）
```bash
pip install -r requirements.txt
streamlit run app.py
```
