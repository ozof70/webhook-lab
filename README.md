# Webhook Lab

把「Webhook 到底送了什麼？」變成可查詢、可重現的事件紀錄。

以 Python / FastAPI 製作的 Webhook 除錯後端，提供 Swagger 操作介面、SQLite 持久化與 Docker 啟動設定。

## 功能

- 建立多個接收端點，每個端點有獨立 token；資料庫只保存 token 雜湊。
- 接收 POST / PUT / PATCH，保留原始 payload bytes、方法、query 與標頭。
- 管理 API 金鑰驗證、事件分頁、端點篩選與 Base64 原文查詢。
- 指定已設定的目的地重送；連線失敗、429、5xx 可進行最多三次嘗試，採指數退避。
- 每次投遞保存狀態碼、錯誤類型、耗時與時間；重新啟動後紀錄仍保留。
- 預設不跟隨 redirect、不使用環境 proxy、不轉送來源驗證資訊。
- 1 MiB 串流 payload 上限、非 root Docker、GitHub Actions 自動測試。

## Docker 快速啟動

```sh
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
# 將輸出的隨機值填入 .env 的 WEBHOOK_API_KEY。
# 將 REPLAY_TARGETS 改為自己擁有的 HTTPS 測試服務；不需重送時填 {}。
docker compose up --build -d
```

開啟 <http://localhost:8000/docs> 操作 API。管理路由展開後在 `x-api-key` 欄位輸入金鑰。健康檢查：`GET /health`。

PowerShell 複製設定檔使用 `Copy-Item .env.example .env`。

## 本機開發

需要 Python 3.13。

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

設定環境變數後執行（本機模式不會自動讀取 `.env`）：

```powershell
$env:WEBHOOK_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:REPLAY_TARGETS = '{"staging":"https://your-service.example/webhook"}'
python -m uvicorn app.main:create_app --factory --reload
```

Linux/macOS 使用 `export WEBHOOK_API_KEY=...` 與 `export REPLAY_TARGETS='{}'`。

## 操作範例（PowerShell）

```powershell
$auth = @{ 'X-API-Key' = $env:WEBHOOK_API_KEY }
$endpoint = Invoke-RestMethod http://localhost:8000/api/endpoints -Method Post -Headers $auth -ContentType application/json -Body '{"name":"payment-test"}'
$event = Invoke-RestMethod ("http://localhost:8000" + $endpoint.capture_path) -Method Post -Headers @{ 'X-Webhook-Token' = $endpoint.capture_token } -ContentType application/json -Body '{"type":"payment.succeeded","amount":1200}'
Invoke-RestMethod http://localhost:8000/api/events -Headers $auth
Invoke-RestMethod ("http://localhost:8000/api/events/" + $event.id) -Headers $auth
Invoke-RestMethod ("http://localhost:8000/api/events/" + $event.id + "/replay") -Method Post -Headers $auth -ContentType application/json -Body '{"target":"staging","attempts":3}'
```

`capture_token` 只在建立時回傳，請妥善保存。此接收驗證需要發送方支援自訂 `X-Webhook-Token` 標頭。

## API

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| GET | `/health` | 資料庫連線檢查 |
| POST / GET | `/api/endpoints` | 建立 / 列出端點 |
| POST / PUT / PATCH | `/hooks/{id}` | 接收事件，使用端點 token |
| GET | `/api/events?endpoint_id=...&limit=50&offset=0` | 分頁查詢 |
| GET | `/api/events/{id}` | 內容與投遞紀錄 |
| GET | `/api/targets` | 可用目的地名稱 |
| POST | `/api/events/{id}/replay` | 重送事件 |

## 重送語意與部署界線

重送沿用原始方法、payload、Content-Type 與 Accept，加入 `X-Webhook-Lab-Event`；其餘標頭和原始 query 不轉送。目的地只由操作者在 `REPLAY_TARGETS` 設定固定 HTTPS URL，API 不接受任意 URL。不要設定不受信任的網域；正式部署應另以防火牆限制對內網及 metadata 服務的流量。

預設只嘗試一次。`attempts` 表示包含首次的總嘗試次數；重試可能造成重複副作用，測試接收服務應以事件 ID 去重。429 與 5xx 使用 0.5、1 秒退避，目前不解析 Retry-After。

這是單實例、內部除錯工具：重送在 HTTP 請求內執行，尚無耐久背景佇列、多租戶隔離、速率限制或自動清理。程序中斷可能留下未記錄的在途投遞；大量資料請自行安排備份與清理。原始 payload、query、自訂標頭可能包含敏感資料，常見驗證標頭遮蔽不等於完整去識別化。請先以測試資料使用；對外部署需 HTTPS、反向代理與流量限制。

## 測試

```sh
python -m pytest -q
```

測試使用暫存資料庫和 HTTPX MockTransport，不會投遞到外部服務；涵蓋驗證、二進位內容、標頭遮蔽、大小上限、分頁、重試、錯誤稽核與重啟保存。

參考：[FastAPI 官方文件](https://fastapi.tiangolo.com/tutorial/)、[HTTPX transports](https://www.python-httpx.org/advanced/transports/)。
