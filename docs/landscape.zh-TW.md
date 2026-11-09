# 與主流 Agent 產品的比較

[English](landscape.md)

本文把這個 harness 和幾個公開說明操作流程的 agent 產品並排比較：OpenAI Codex、Claude Code（以及 Claude Agent SDK／Managed Agents）、opencode、DeepSeek Harness（DSH），以及具代理能力的瀏覽器。內容依 2026-11-08 讀到的官方文件與原始碼整理；這些產品變動很快，實際讀過的頁面列在[來源](#來源)。

目的不是追功能清單。那些產品是能執行 shell、改檔、上網的程式代理或瀏覽器；這個 harness 刻意收窄：**只從使用者選定的本機來源回答**，並發布**附引用的成果**。比較的重點是：它們的控制與安全機制有哪些適用這種形態、哪些這裡已經有、哪些值得補上。

## 操作流程

| 階段 | Codex | Claude Code | opencode | DSH | 代理瀏覽器 | 本專案 |
|---|---|---|---|---|---|---|
| 交付任務 | thread → turn；IDE、CLI、App、SDK 與 App Server 協定 | 互動對話、無頭 `-p` 搭配 stream-JSON、Agent SDK | TUI、`opencode run --format json`、共用的本機伺服器、ACP | Web、headless、SDK、ACP 等設定檔共用一棵外掛樹 | 側邊欄或任務分頁，常先「提出計畫」 | HTTP API、工作台或無頭 NDJSON 用戶端：任務＋選定來源；重送同一個 key 會拿回原本的 Run |
| 顯示計畫 | turn 串流中的計畫項目 | Plan 模式（核准前唯讀） | `plan` agent（除計畫檔外禁止編輯） | Plan 模式只是「軟性引導」；`exit_plan_mode` 顯示計畫 | 核准的計畫界定之後動作的範圍 | 必須先保存計畫，其他工具才會開放；計畫公開在事件串流中。可選擇開啟計畫核准（`require_plan_approval`）：使用者核准前不會執行任何工具，修改後的計畫需再次核准 |
| 核准高風險動作 | 型別化的核准請求：接受、本次對話都接受、拒絕、取消 | 權限模式；deny → ask → allow 規則；hooks | `ask` 可回覆一次／一律／拒絕 | 失敗即關閉的結果：allowed-once、rejected、cancelled、unavailable | 依網站／動作類別授權；敏感步驟由使用者接手 | 單次核准綁定參數摘要，用於發布（可選）與每次 `askTools` MCP 工具呼叫；未列出的工具一律拒絕 |
| 中途引導或中斷 | steer、排隊、中斷 | 中斷；排隊後續訊息 | Enter 引導、排隊、Esc 中斷；提示 ID 具冪等性 | inbox 的 `steer`／`followup`／`cancel(keepInbox)` | 暫停、接手、繼續 | Run 進行中可追加訊息（以訊息 ID 冪等受理，於下一次決策送達）；可取消 |
| 檢視與復原 | turn diff；thread 還原與檔案還原分開 | 檢查點與 rewind | 每步 Git 快照、分段 `/undo` | Review 檢視；文件未提到檔案復原 | 以草稿代替提交（例如寫好但不送出） | 不可變、有雜湊並可比較版本差異的成果；可選草稿 → 發布審閱模式；修訂是以基準成果開新的 Run |
| 完成 | turn completed 項目 | 結果訊息帶子類型（例如達到回合上限） | `run` 會等背景工作結束 | `turn/end` 帶中止原因 | 列出已執行的動作 | 有提供 `task_finish` 時，其摘要會和已發布成果比對；仍有未解決的工具錯誤或未發布草稿時不能完成；終止狀態不會被重新打開；顯示供應商回報的 token 用量，設定單價時另有費用估算 |
| 當機後接續 | 持久化的 thread | 接續對話 | SQLite 對話；事件匯流排只有即時、不重播 | 只追加的 JSONL；保留未結束的 turn，僅在擁有寫入權時修復 | 通常沒有 | 派送前先保存意圖；明確接續時重用已確認結果，絕不重跑效果未知的操作 |

## 機制

| 機制 | 產品做法 | 本專案 |
|---|---|---|
| 事件紀錄與串流 | Codex App Server 串流 thread/turn/item 事件；DSH 把即時片段和持久結果分開，重連時補齊缺口；opencode 的用戶端事件只有即時 | 事件先寫入 SQLite 再推送；WebSocket 依 cursor 接續並回 ACK |
| 隔離 | Codex 與 Claude Code 對 shell 與寫檔做沙箱；opencode 與 DSH 直接說明權限不等於隔離，建議用 VM 或容器 | 沒有 shell。來源唯讀；唯一會寫入工作區的是把上傳檔存進 `uploads/`（不覆寫），Run 的效果只有把成果發布到儲存區，或使用者核准過的 MCP 工具呼叫 |
| 不可信內容 | 瀏覽器把頁面內容視為受污染、用隔離的審查模型、不讓密鑰進入模型上下文 | 來源文字、記憶與摘錄都標成不可信資料；API 金鑰只從環境變數讀取，不會進入請求內容 |
| 上下文管理 | 接近上限時滾動摘要與壓縮（opencode、DSH、Claude Code） | 每個請求先量測；先做無損調整，再做標示過的有損省略，仍超出就停止——不會默默截斷或摘要 |
| 指令與記憶 | `AGENTS.md`／`CLAUDE.md` 鏈 | 可選用工作區的 `AGENTS.md`，Run 開始時連同 SHA-256 固定；使用者保存、具版本的記憶，以樂觀並行控制更新 |
| 擴充 | hooks、外掛、MCP、子代理 | 固定內建工具、使用者允許的本機或遠端 MCP 工具（唯讀或每次需核准）與 skills，都由每個 Run 選用；工具呼叫前（允許／拒絕，失敗即拒絕）與呼叫後的 HTTP webhook hooks；提供者都實作同一個 `DecisionModel` 協定。沒有外掛與子代理 |
| 驗證 | 多半交給使用者或自動審查模型 | 只看成果與證據、從不看任務文字的驗證器 |

## 刻意不同之處

- **以能力劃界，而不是事後授權。** 那些產品給通用代理廣泛的工具再加以管制；這裡工具集本身就是邊界：讀選定來源、對來源計算、發布附引用的成果。核准面因此小到每次核准都能綁定精確的參數摘要。
- **以驗證取代信任。** 每個計算值都必須引用產生它的證據，完成回覆也必須和已發布內容一致。比較的產品都沒有在文件中說明會依工具證據檢查輸出。
- **誠實地停下。** 超出預算時寧可停止，也不摘要使用者無法檢視的歷史。

## 本次找到的缺口

以這些產品的機制當檢查清單逐項對照程式碼。每項記錄問題與修正，每個修正都有回歸測試。

| # | 缺口 | 參考做法 | 修正 |
|---|---|---|---|
| 1 | 原生工具呼叫沒有強制「本步提供的工具清單」：例如只開放 `plan_update` 時，模型仍能讀取來源。（`json_tools` 協定在解碼時就已拒絕未知名稱。） | DSH 每個步驟只解析一次可用工具；Codex 與 Claude Code 拒絕權限集合以外的工具 | 原有檢查照舊先執行並保留專屬錯誤碼；通過後若呼叫不在清單內，就在派送前拒絕，回傳可修正的 `tool_not_offered` 並列出允許的工具；會消耗一次修正額度，但不要求模型之後一定重試該工具。模擬模型改為先保存計畫，和真實模型一樣。 |
| 2 | 發布後的收尾把對話換成「系統訊息＋任務＋已發布正文」，但記憶、摘錄與歷史的記錄仍指向被丟掉的訊息。發布期間若有人修改記憶，Run 會崩潰（`execution_error`）；超出預算時甚至可能刪到任務訊息。 | DSH 保留未結束的 turn，只在擁有寫入權時修復 | 收尾時清掉這些記錄，收尾上下文固定不變：記憶更新不套用到收尾。 |
| 3 | API 只要 `Host` 是 loopback 且沒有外來 `Origin` 就放行。同機的任何程序或其他作業系統使用者，或任何連到發布錯誤的容器埠的人，都能登錄資料夾並透過 Run 讀取檔案。 | opencode 在伺服器未設密碼時發出警告；筆記本伺服器採每次啟動的權杖 | 啟動的工作台需要每次啟動的存取權杖（自動產生，或使用 `AGENT_HARNESS_TOKEN`）。瀏覽器用 `/?token=…` 換成 HttpOnly、SameSite=Strict 的 cookie；腳本送 `Authorization: Bearer`。正式版不再接受僅供測試的 `testserver` 主機名稱。 |
| 4 | 頁面可以被嵌入框架，核准按鈕可能遭點擊劫持。 | 標準的點擊劫持防護 | 所有回應都帶 `X-Frame-Options: DENY`、`frame-ancestors 'none'`、`nosniff` 與 `Referrer-Policy: no-referrer`（第一次開啟的網址含權杖）。 |
| 5 | `model.requested` 只記錄上下文預算，沒有記錄實際送出的內容，事後無法把請求對回當時的工具清單。 | DSH：「模型看得到的都要記錄」 | 每個 `model.requested` 事件記錄提供的工具名稱，以及實際請求內容的 SHA-256。 |
| 6 | 引用預覽和發布用不同規則判定伺服器產生的證據附錄，模型自寫的標題可能讓一項檢查看不到正文、另一項卻看得到。 | — | 兩者改用同一個判定函式。 |
| 7 | 修正 #1 時發現：未開本機思考模式的原生模型（包含 Claude 與 OpenAI 相容轉接器）從不會被提供 `task_finish`，而是以純文字結束，因此遠端模型從不經過完成回覆檢查。 | Claude Code 與 Codex 以型別化的結果結束一回合；DSH 記錄回合結束的原因 | 遠端轉接器在發布後會被提供 `task_finish`；有提供 `task_finish` 時，發布後的純文字結束同樣經過 `completion_summary` 檢查，不符的回覆不放入下一次請求，改為只開放 `task_finish` 的可修正步驟。 |
| 8 | 執行中的 Run 只能取消，使用者無法在不重來的情況下補充或修正指示。 | Codex steer/queue、opencode `delivery: steer \| queue`、DSH inbox 的 `steer`／`followup` | `POST /api/runs/{id}/messages` 以 `message_id` 冪等受理訊息；Runtime 在下一個步驟邊界以使用者回合送達，記錄 `message.queued`／`message.delivered`；沒送到模型的訊息以 `message.undelivered` 回報。 |
| 9 | 腳本與 CI 只能自行撰寫 HTTP 呼叫來驅動 harness。 | Claude Code `-p --output-format stream-json`、`opencode run --format json` | `python -m agent_harness.headless` 提交任務，把每個已保存事件輸出為 NDJSON，最後輸出 `headless.result`；逐筆確認事件、從 cursor 重新連線，退出碼 0 完成／1 失敗／2 被拒／3 取消／4 中斷。 |
| 10 | 工具集固定，無法接上外部工具。 | Claude、Codex、opencode、DSH 的 MCP 伺服器 | 使用者設定的 stdio MCP 伺服器；只提供列在允許清單、且未宣告副作用的工具，Run 選用特定工具，結果成為被引用的證據，伺服器拿不到工作台的秘密。 |
| 11 | 只有發布能要求核准，任何有副作用的外部工具都只能拒絕。 | Claude Code 的 deny → ask → allow 規則、Codex 的型別化核准、DSH 失敗即關閉的結果 | `askTools` 讓有副作用的 MCP 工具只在單次核准後執行，核准綁定確切參數與範圍；否則派送一律拒絕，結果遺失時成為效果未知並阻擋自動續做。 |
| 12 | 無法把某類工作的做法包裝成可重複使用的指示。 | Claude 與 Codex 的 Agent Skills；opencode、DSH 的 AGENTS.md | `--skills-dir` 載入 `SKILL.md` 形式的 skills；Run 只看到選用 skill 的名稱與說明，需要時以 `skill_load` 載入指示（附雜湊），任務以 `/skill名稱` 開頭也能選用。 |
| 13 | 修改會保留每個版本，但看不出版本之間改了什麼。 | Codex 的 turn diff、Claude Code 的檢查點 | 差異 API 與預覽切換可比較同一對話中的任兩個版本（預設對照上一版），比較前先重新驗證兩者的雜湊。 |
| 14 | 模型與思考強度在工作台啟動時就固定。 | Claude、Codex、opencode 的模型與強度選單 | 遠端供應商可提供替代模型、Claude 可提供思考強度；每個 Run 自行選擇（工作台選單或 API 的 `model`／`effort`），選擇會被記錄並在續做時保留。本機模型維持固定，因為每個都需要各自的驗證。 |
| 15 | 計畫雖然公開，但使用者無法在工作開始前核准。 | Claude Code 的 Plan 模式、opencode 的 `plan` agent、先規劃的 AI 瀏覽器 | 可選的計畫核准：Run 停在計畫這一步；「要求修改」會連同使用者意見退回模型（不計入錯誤修正額度）；修改後的計畫要再次核准；續做時保留已核准的計畫。 |
| 16 | 工作區無法帶著固定指令，每個任務都得重述做法。 | Codex、opencode、DSH 的 `AGENTS.md`；Claude Code 的 `CLAUDE.md` | Run 可套用工作區的 `AGENTS.md`（8 KiB、UTF-8、一般檔案）。文字與 SHA-256 在受理時固定、續做時保留，且位於 harness 規則之下，不能增加來源、工具或批准。只讀根目錄一個檔案，不做目錄鏈。 |
| 17 | Run 只計算模型與工具呼叫次數，沒有 token 用量與費用。 | Claude Code 的 `/cost`；Codex 的 token 計數；opencode 的 session 費用 | 每個 Run 在保存每次回覆事件的同一交易中加總供應商回報的 tokens。`--price MODEL=INPUT,OUTPUT` 提供在受理時固定的費用估算；未回報用量的回覆會被計數，估算標示為不完整。沒有內建價格表。 |
| 18 | 工作台只有繁體中文介面。 | 參考產品多以英文為主要介面 | 新增英文／繁中切換（預設依瀏覽器語言，選擇存在本機）。所有介面字串都經過同一份以原中文為鍵的字典；新增字串若未包裝或缺英文，測試就會失敗。檔案內容與模型輸出不翻譯。 |
| 19 | 只有純文字檔能當來源。 | Claude 與 Codex 的檔案附件；AI 瀏覽器的文件閱讀 | 工作區中的 PDF 與 Word（`.docx`）檔可作為來源：模型讀取有上限的擷取文字，引用與變更偵測使用檔案本身的雜湊，無法讀取的文件會附上原因拒絕。工作台上傳的檔案經相同檢查後存進 `uploads/`，且不會覆寫。不做 OCR。 |
| 20 | MCP 伺服器只能以本機指令執行。 | Claude、Codex、opencode 的遠端 MCP 伺服器 | 透過 Streamable HTTP 連接遠端伺服器（HTTPS，或連到迴路位址的 HTTP），憑證從設定所指名的環境變數讀取，不寫在設定中也不顯示。不支援 OAuth。 |
| 21 | Harness 之外無法否決或觀察工具呼叫。 | Claude Code、Codex、opencode 的 hooks | 工具呼叫前（允許／拒絕；任何 deny 優先；連不上或回覆無效視為 deny）與呼叫後（只觀察）的 HTTP webhook，並記錄為 Run 事件。本機程式只能以參數清單執行（絕不經過 shell），需加上 `--allow-command-hooks`，且只給最小環境變數；hooks 不能修改參數，驗證器也不參考它們。 |
| 22 | Token 用量與費用只能看單一 Run。 | Claude Code 與 opencode 的 session 費用 | `GET /api/sessions/{id}/usage` 加總對話中的所有 Run；有 Run 沒有單價或未回報用量時，估算標示為不完整。 |
| 23 | 引用 CSV 樣本的值可能放在錯誤欄位。 | 參考產品多交由使用者自行檢查 | `table_columns`：值的標籤正好是所引用表格的欄名，而該值在樣本中只出現在其他欄時，拒絕發布並以可修正錯誤指出正確欄位。 |

## 已經具備

- **失敗即關閉的核准。** 核准只能使用一次並綁定參數摘要，結果只會是核准、拒絕、過期（逾時或重啟）或取消（Run 被取消），和 DSH 文件列的封閉集合相同，而且沒有「整個對話都允許」的授權，比較嚴格。
- **可持久重播的串流。** opencode 的用戶端事件只有即時；這裡用戶端可從任何 cursor 重新接上。
- **當機語意。** 派送前先保存意圖，被中斷的效果會標成不確定而不是自動重試，和 DSH 保留中斷 turn、不截斷的原則一致。

## 待辦（依對本專案的價值排序）

1. **真實的 Claude 批次。** 轉接器目前只有模擬傳輸測試；要錄一次真實執行需要 API 金鑰與花費同意。
2. **OAuth 連接器。** 遠端 MCP 伺服器的憑證來自環境變數；要透過 OAuth 登入，需要決定接哪些服務、權杖如何保存。
3. **掃描 PDF 的 OCR。** 沒有文字層的文件目前會被拒絕。

## 來源

見[英文版的來源清單](landscape.md#sources)。
