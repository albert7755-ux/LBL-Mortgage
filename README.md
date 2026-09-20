[README (1).md](https://github.com/user-attachments/files/32436768/README.1.md)
# Lombard 槓桿 vs 房貸覆蓋率試算器

以房貸借出本金 → 投入債券／基金組合 → 循環質押放大部位，
計算配息現金流能否覆蓋房貸本息，並評估 margin call 風險。

## 功能

| 區塊 | 內容 |
|---|---|
| ① 部位結構 | 借 1~4 次的總部位、總借款、槓桿倍數，可展開逐輪明細 |
| ② 現金流與覆蓋率 | 20／30 年期房貸覆蓋率、年結餘、打平所需最低配息率 |
| ③ 風險評估 | margin call 緩衝、淨值侵蝕存活年數、升息壓力測試 |
| ④ 配息率敏感度 | 折線圖，房貸年繳為水平線，曲線在線上即為可覆蓋 |
| ⑤ 輸出 PDF | 一鍵產生繁體中文 PDF 摘要報告 |

## 計算邏輯

**有效質借成數** = 加權 LTV × 錯幣折扣 × 額度動用比例

其中加權 LTV = 債券比重 × 債券 LTV + 基金比重 × 基金 LTV。

**維持成數（追繳線）** = 加權 LTV × 錯幣折扣（不含動用比例）

**循環質借**：每輪以上一輪買入金額 × 有效質借成數借出，再全數投入。

**房貸年繳**採本息均攤，利率固定。

## 安裝與執行

```bash
pip install -r requirements.txt
streamlit run lombard_mortgage_app.py
```

## 部署到 Streamlit Community Cloud

1. 將本 repo 推到 GitHub
2. 到 [share.streamlit.io](https://share.streamlit.io) 新增 app，主檔案選 `lombard_mortgage_app.py`
3. `requirements.txt` 與 `packages.txt` 會自動被讀取

> `packages.txt` 裡的 `fonts-noto-cjk` 是 **PDF 中文字型**用的，
> 沒有這一行雲端產出的 PDF 中文會變空白。

## PDF 中文字型

程式會依序尋找可用字型，找到任何一個即可：

| 環境 | 來源 |
|---|---|
| 專案自備 | `fonts/NotoSansTC-Regular.ttf` 或 `fonts/msjh.ttc` |
| Streamlit Cloud / Linux | `packages.txt` 安裝的 `fonts-noto-cjk` |
| Windows | 微軟正黑體 `msjh.ttc`、細明體 `mingliu.ttc` |
| macOS | PingFang、STHeiti、Arial Unicode |

都找不到時 app 會顯示提示，其餘功能不受影響。

## 模型假設與限制

- 本金來自房貸，投資部位自有資金為 0，實質風險由房屋淨值承擔
- 配息率視為穩定現金流，未計入債券價格波動、提前買回、違約風險
- 未計入匯率損益。錯幣折扣是銀行對匯率風險的定價，不等於投資人沒有風險
- 未計入海外所得最低稅負、信託／保管費、基金手續費
- 房貸為本息均攤、利率固定；Lombard 利率實務上為浮動
- 債券「持有到期還本」僅保原幣本金，不保台幣本金
- 長年期間債券需再投資數輪，存在再投資風險
- **基金質借成數實務上常低於債券**，請先與授信單位確認後再調整參數

## 免責聲明

本工具僅供內部試算與教育用途，不構成投資建議。
實際條件以各行授信規定與商品文件為準。
