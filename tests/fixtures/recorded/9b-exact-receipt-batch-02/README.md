# 9B 原文讀取候選：精確計量／收據第 02 批

固定5781799；原六例／來源／真值與8192／1024／180秒、12決策／20工具／2修正不變。9B JSON／關閉思考、精確vocab-only計量、direct發布／artifact_receipt。新初始決策實跑，未沿用舊批接受數。

| 案例 | 秒 | 模型／工具 | 人工判定 |
|---|---:|---:|---|
| analysis-standard | 30.70 | 6／7 | 接受240／15／6.25% |
| analysis-variant | 24.94 | 6／7 | 接受280／21／7.5% |
| analysis-missing | 24.23 | 5／7 | 接受150；異常總數／率不可算，無補零／虛構日期 |
| documents-standard | 22.37 | 5／6 | 內容事實接受；來源連結缺失，整例拒收 |
| documents-variant／missing | — | — | 未准入 |

第四例實際採用path-only source_read，表格有正確Release checklist／Nina／all smoke tests pass及Support handoff／Owen／Iris，也正確稱必要缺項為無。原先字面標籤造成的主題誤判未在此Run出現；這是本例觀察，不是穩定性或單因素因果宣告。

正文每列均無檔名或EID引用，只有伺服器canonical appendix列E1／E2及來源SHA／定位。模型宣告citations合法不代表正文有逐文件來源連結；依既有可追溯來源與正文引用合約拒收。不是僅由filenames_in_body substring失敗推論語意，也沒有新增主題字面欄名等門檻。詳見 documents-standard/manual-review.json 及原始事件／成果。

全部4 Run均實際發布並completed；人工3/4接受，首失敗停批、後兩例未准入。所有前置input count均與provider一致，各例含reserve最大3497／3488／3605／3082，未超8192；不據單次耗時比較效能。

下一步應用公開重播驗證正文與宣告證據的連結合約：識別目前body-only驗證與server appendix的界線，避免僅有全域附錄便被當成正文來源對應。不得替模型補寫引用、硬編文件答案或改判舊成果；任何新guard需先測完整原文／一般E文字／計算依賴／快照與既有修正限額，再由新固定批次證明品質。

本候選完整359項控制與mypy21檔及雙軸通過，不是完整六例驗收。當前受控故障品質、完整真GUI／實體下載與最終A1–A11審查仍未完成。
