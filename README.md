# 睡不飽（shui-bu-bao）

LINE 機器人「睡不飽」，部署在 Cloady，Git 部署：推上 main 分支後，在 Cloady 手動重新部署才會上線（autoUpdate 關閉）。

## 功能

- 文字聊天（Groq 優先、Apinex 備援）
- 即時股價、天氣（Open-Meteo，任意地點）、新聞
- 圖片、影片、檔案摘要
- 生圖（Agnes 免費，/生圖 <描述>、生圖 <描述>、幫我畫 <描述>）
- 語音回覆（Edge TTS 台灣腔，預設雲哲男聲；Gemini TTS 備援）
- 發票對獎、附近美食、指令選單
- 長期記憶、記事情（SQLite）

## 部署

Cloady 上以 Git 來源部署此 repo，`autoUpdate` 關閉時需手動重新部署才會上線。

## 環境變數

- `GROQ_API_KEY`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `APINEX_API_KEY`
- `APINEX_MODEL`
- `AGNES_API_KEY`（生圖用，Agnes 免費模型）
- `GEMINI_API_KEY`（語音備援用，Gemini TTS）
- `MISTRAL_API_KEY`（第三備援）
- `AGENTSCOPE_API_KEY`（第四備援）
- `REQUESTY_API_KEY`（第五備援）
- `EDGE_TTS_VOICE`（選填，預設 `zh-TW-YunJheNeural` 雲哲男聲）
- `PUBLIC_URL`（生圖圖片網址，預設就是 Cloady 的網址，不用另外設）

金鑰一律放在部署平台的環境變數裡，絕對不要寫進程式或 commit 上來。
