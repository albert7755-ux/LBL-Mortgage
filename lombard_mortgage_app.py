"""
Lombard 槓桿 vs 房貸覆蓋率試算器
=================================
情境：以房貸借出本金 → 投入債券/基金組合 → 循環質押放大部位
      計算配息現金流能否覆蓋房貸本息，並評估 margin call 風險

執行方式：streamlit run lombard_mortgage_app.py
"""

import io
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Lombard 槓桿房貸覆蓋率試算",
    page_icon="🏠",
    layout="wide",
)

# ============================================================
# 核心計算函式
# ============================================================

def blended_effective_ltv(w_bond, ltv_bond, w_fund, ltv_fund, fx_discount, utilization):
    """混合有效質借成數 = 加權LTV × 錯幣折扣 × 動用比例"""
    weighted_ltv = w_bond * ltv_bond + w_fund * ltv_fund
    return weighted_ltv * fx_discount * utilization


def blended_maintenance_ltv(w_bond, ltv_bond, w_fund, ltv_fund, fx_discount):
    """維持成數（不含動用比例）——這是 margin call 的觸發線"""
    weighted_ltv = w_bond * ltv_bond + w_fund * ltv_fund
    return weighted_ltv * fx_discount


def build_ladder(principal, eff_ltv, rounds):
    """
    循環質借階梯
    回傳: (明細DataFrame, 總部位, 總借款)
    """
    rows = []
    current = principal
    total_position = principal
    total_borrow = 0.0

    for i in range(1, rounds + 1):
        borrowed = current * eff_ltv
        rows.append({
            "輪次": f"第 {i} 輪",
            "買入金額": current,
            "質押借出": borrowed,
        })
        total_borrow += borrowed
        total_position += borrowed
        current = borrowed

    rows.append({"輪次": "最後投入", "買入金額": current, "質押借出": 0.0})
    rows.append({"輪次": "合計", "買入金額": total_position, "質押借出": total_borrow})

    return pd.DataFrame(rows), total_position, total_borrow


def mortgage_annual_payment(principal, annual_rate, years):
    """房貸本息均攤——年繳金額"""
    if annual_rate <= 0:
        return principal / years
    i = annual_rate / 12
    n = years * 12
    monthly = principal * i / (1 - (1 + i) ** (-n))
    return monthly * 12


def survival_years(bond_position, fund_position, total_borrow, maint_ltv, fund_decline):
    """
    基金淨值逐年侵蝕下，幾年後觸發 margin call
    （假設債券持有到期、價格不計入侵蝕）
    """
    if fund_decline <= 0:
        return None  # 不侵蝕 → 永不觸發
    trigger_asset = total_borrow / maint_ltv
    if trigger_asset <= bond_position:
        return None  # 光靠債券部位就守得住，基金歸零也沒事
    if fund_position <= 0:
        return None
    ratio = (trigger_asset - bond_position) / fund_position
    if ratio >= 1:
        return 0.0  # 一開始就已經在追繳線上
    return math.log(ratio) / math.log(1 - fund_decline)


# ============================================================
# 顯示輔助
# ============================================================

def fmt(x):
    """數字格式化為萬元（一位小數）"""
    return f"{x:,.1f}"


def cover_label(x):
    """覆蓋率評語（純文字，PDF 可用）"""
    if x >= 1.3:
        return "充足"
    if x >= 1.0:
        return "勉強"
    return "不足"


def buffer_label(x):
    """追繳緩衝評語"""
    if x >= 0.25:
        return "充足"
    if x >= 0.18:
        return "偏薄"
    return "危險"


def is_never(x):
    """判斷存活年數是否為「不會觸發」（pandas 會把 None 轉成 NaN，需一併處理）"""
    return x is None or (isinstance(x, float) and math.isnan(x))


def survival_label(x):
    """存活年數評語"""
    if is_never(x):
        return "不會觸發"
    if x >= 30:
        return "安全"
    if x >= 15:
        return "留意"
    return "危險"


ICON = {"充足": "✅", "勉強": "⚠️", "不足": "❌",
        "偏薄": "⚠️", "危險": "❌", "安全": "✅",
        "留意": "⚠️", "不會觸發": "♾️"}


def show_df(df):
    """顯示表格（相容新舊版 Streamlit 的寬度參數）"""
    try:
        st.dataframe(df, hide_index=True, width="stretch")
    except TypeError:
        st.dataframe(df, hide_index=True, use_container_width=True)


# ============================================================
# PDF 中文字型偵測
# ============================================================

FONT_CANDIDATES = [
    # 專案內自備字型（最優先，放一份在 repo 的 fonts/ 資料夾即可）
    ("fonts/NotoSansTC-Regular.ttf", "fonts/NotoSansTC-Bold.ttf"),
    ("fonts/msjh.ttc", "fonts/msjhbd.ttc"),
    # Linux / Streamlit Cloud（需 packages.txt 安裝 fonts-noto-cjk）
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", None),
    # Windows
    ("C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msjhbd.ttc"),
    ("C:/Windows/Fonts/mingliu.ttc", None),
    # macOS
    ("/System/Library/Fonts/PingFang.ttc", None),
    ("/System/Library/Fonts/STHeiti Medium.ttc", None),
    ("/Library/Fonts/Arial Unicode.ttf", None),
]


@st.cache_data(show_spinner=False)
def find_cjk_font():
    """找一個可用的中文字型，回傳 (regular路徑, bold路徑或None)。找不到回傳 None。"""
    for regular, bold in FONT_CANDIDATES:
        if os.path.exists(regular):
            return regular, (bold if bold and os.path.exists(bold) else None)
    return None


# ============================================================
# PDF 產出
# ============================================================

def build_pdf(params, summary, terms, rounds_list, font_paths):
    """產生 PDF 摘要報告，回傳 bytes"""
    from fpdf import FPDF

    regular, bold = font_paths

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(15, 15, 15)

    pdf.add_font("cjk", "", regular)
    pdf.add_font("cjk", "B", bold if bold else regular)
    pdf.add_page()

    W = 180  # 可用寬度

    def title(text, size=16):
        pdf.set_font("cjk", "B", size)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 9, text, new_x="LMARGIN", new_y="NEXT")

    def section(text):
        pdf.ln(3)
        pdf.set_font("cjk", "B", 12)
        pdf.set_text_color(30, 60, 120)
        pdf.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 20, 20)

    def note(text, size=9):
        pdf.set_font("cjk", "", size)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 20, 20)

    def table(headers, rows, widths, align=None):
        align = align or ["C"] * len(headers)
        pdf.set_font("cjk", "B", 9)
        pdf.set_fill_color(235, 240, 248)
        for h, w in zip(headers, widths):
            pdf.cell(w, 7, h, border=1, align="C", fill=True)
        pdf.ln()
        pdf.set_font("cjk", "", 9)
        for idx, row in enumerate(rows):
            pdf.set_fill_color(250, 250, 250) if idx % 2 else pdf.set_fill_color(255, 255, 255)
            for cell, w, a in zip(row, widths, align):
                pdf.cell(w, 6.5, str(cell), border=1, align=a, fill=True)
            pdf.ln()

    # ---------- 標題 ----------
    title("Lombard 槓桿 vs 房貸覆蓋率試算報告")
    pdf.set_font("cjk", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, f"產製日期：{date.today().isoformat()}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    pdf.ln(2)

    # ---------- 參數摘要 ----------
    section("一、參數設定")
    p = params
    param_rows = [
        ["本金（來自房貸）", f"{p['principal']:,} 萬", "房貸利率", f"{p['mortgage_rate']*100:.2f}%"],
        ["債券比重", f"{p['w_bond']*100:.0f}%", "債券配息率", f"{p['yield_bond']*100:.2f}%"],
        ["基金比重", f"{p['w_fund']*100:.0f}%", "基金配息率", f"{p['yield_fund']*100:.2f}%"],
        ["債券 LTV", f"{p['ltv_bond']*100:.0f}%", "基金 LTV", f"{p['ltv_fund']*100:.0f}%"],
        ["錯幣折扣", f"{p['fx_discount']*100:.0f}%", "額度動用比例", f"{p['utilization']*100:.0f}%"],
        ["Lombard 利率", f"{p['lombard_rate']*100:.2f}%", "混合配息率", f"{p['blended_yield']*100:.2f}%"],
        ["有效質借成數", f"{p['eff_ltv']*100:.2f}%", "維持成數（追繳線）", f"{p['maint_ltv']*100:.2f}%"],
    ]
    table(["項目", "數值", "項目", "數值"], param_rows, [45, 45, 45, 45],
          align=["L", "R", "L", "R"])

    # ---------- 部位結構 ----------
    section("二、部位結構")
    pos_rows = [
        [r["借款次數"], fmt(r["總部位"]), fmt(r["總借款"]), f"{r['槓桿倍數']:.2f} 倍"]
        for _, r in summary.iterrows()
    ]
    table(["借款次數", "總部位（萬）", "總借款（萬）", "槓桿倍數"], pos_rows,
          [36, 48, 48, 48], align=["C", "R", "R", "C"])

    # ---------- 現金流與覆蓋率 ----------
    section("三、現金流與房貸覆蓋率")
    pay_text = "　｜　".join(
        f"{t} 年期房貸年繳 {fmt(mortgage_annual_payment(p['principal'], p['mortgage_rate'], t))} 萬"
        for t in terms
    )
    note(pay_text, size=9)
    pdf.ln(1)

    headers = ["借款次數", "年配息", "年利息", "年淨現金流"]
    widths = [24, 22, 22, 24]  # 合計 92mm
    for t in terms:
        headers += [f"{t}年覆蓋率", f"{t}年結餘"]
        widths += [26, 18]  # 每組 44mm，兩組共 88mm → 總計 180mm
    cf_rows = []
    for _, r in summary.iterrows():
        row = [r["借款次數"], fmt(r["年配息"]), "-" + fmt(r["年利息"]), fmt(r["年淨現金流"])]
        for t in terms:
            cov = r[f"{t}年覆蓋率"]
            row += [f"{cov:.2f} 倍 {cover_label(cov)}", f"{r[f'{t}年結餘']:+.1f}"]
        cf_rows.append(row)
    table(headers, cf_rows, widths, align=["C", "R", "R", "R"] + ["C", "R"] * len(terms))

    # 打平配息率
    pdf.ln(2)
    pdf.set_font("cjk", "B", 9)
    pdf.cell(0, 6, "打平所需的最低混合配息率", new_x="LMARGIN", new_y="NEXT")
    be_rows = []
    for _, r in summary.iterrows():
        row = [r["借款次數"]]
        for t in terms:
            pay = mortgage_annual_payment(p["principal"], p["mortgage_rate"], t)
            need = (pay + r["總借款"] * p["lombard_rate"]) / r["總部位"] if r["總部位"] else 0
            row.append(f"{need*100:.2f}%")
        be_rows.append(row)
    table(["借款次數"] + [f"{t} 年期" for t in terms], be_rows,
          [60] + [60] * len(terms))

    # ---------- 風險評估 ----------
    if pdf.get_y() > 215:
        pdf.add_page()
    section("四、風險評估")
    risk_rows = []
    for _, r in summary.iterrows():
        surv = r["存活年數"]
        surv_text = "不會觸發" if is_never(surv) else f"約 {surv:.0f} 年（{survival_label(surv)}）"
        risk_rows.append([
            r["借款次數"],
            f"{r['總借款']/r['總部位']*100:.1f}%",
            f"-{r['追繳緩衝']*100:.1f}%（{buffer_label(r['追繳緩衝'])}）",
            surv_text,
            fmt(r["壓力淨現金流"]),
        ])
    table(["借款次數", "借款/資產", "跌多少觸發追繳", "淨值侵蝕存活年數", "壓力後淨現金流"],
          risk_rows, [30, 28, 42, 42, 38], align=["C", "R", "C", "C", "R"])
    note(f"淨值侵蝕假設：基金淨值年跌 {p['fund_decline']*100:.1f}%，債券持有到期不計價格變動。"
         f"　壓力情境：Lombard 利率升至 {p['stress_rate']*100:.2f}%。")

    # ---------- 假設與限制 ----------
    if pdf.get_y() > 200:
        pdf.add_page()
    section("五、模型假設與限制")
    for line in [
        "本金來自房貸，投資部位自有資金為 0，實質風險由房屋淨值承擔。",
        "配息率視為穩定現金流，未計入債券價格波動、提前買回、違約風險。",
        "未計入匯率損益。錯幣折扣是銀行對匯率風險的定價，不等於投資人沒有風險。",
        "未計入海外所得最低稅負、信託／保管費、基金手續費。",
        "房貸假設為本息均攤、利率固定；Lombard 利率實務上為浮動。",
        "債券「持有到期還本」僅保原幣本金，不保台幣本金。",
        "長年期間債券需再投資數輪，存在再投資風險。",
        "基金質借成數實務上常低於債券，請先與授信單位確認後再調整參數。",
    ]:
        note("・" + line, size=9)

    pdf.ln(4)
    pdf.set_font("cjk", "", 8)
    pdf.set_text_color(140, 140, 140)
    pdf.multi_cell(0, 4.5,
                   "本報告由試算工具自動產生，僅供內部試算與教育用途，不構成投資建議。"
                   "實際條件以各行授信規定與商品文件為準。")

    out = pdf.output()
    return bytes(out) if not isinstance(out, (bytes, bytearray)) else bytes(out)


# ============================================================
# 側邊欄：參數輸入
# ============================================================

st.sidebar.header("⚙️ 參數設定")

st.sidebar.subheader("1️⃣ 資金來源")
principal = st.sidebar.number_input(
    "本金（萬元）", min_value=100, max_value=100000, value=1000, step=100,
    help="這筆錢從房貸借出",
)
mortgage_rate = st.sidebar.slider("房貸利率 (%)", 0.5, 8.0, 2.65, 0.05) / 100

st.sidebar.subheader("2️⃣ 資產配置")
w_bond_pct = st.sidebar.slider("債券比重 (%)", 0, 100, 70, 5)
w_bond = w_bond_pct / 100
w_fund = 1 - w_bond
st.sidebar.caption(f"債券 {w_bond_pct}% ／ 基金 {100 - w_bond_pct}%")

col_y1, col_y2 = st.sidebar.columns(2)
with col_y1:
    yield_bond = st.number_input("債券配息 (%)", 0.0, 20.0, 5.5, 0.1) / 100
with col_y2:
    yield_fund = st.number_input("基金配息 (%)", 0.0, 20.0, 8.0, 0.1) / 100

st.sidebar.subheader("3️⃣ 質借條件")
lombard_rate = st.sidebar.slider("Lombard 利率 (%)", 0.5, 10.0, 2.65, 0.05) / 100

col_l1, col_l2 = st.sidebar.columns(2)
with col_l1:
    ltv_bond = st.number_input("債券 LTV (%)", 0, 100, 75, 5) / 100
with col_l2:
    ltv_fund = st.number_input("基金 LTV (%)", 0, 100, 75, 5) / 100
st.sidebar.caption("⚠️ 基金的實際成數常低於債券，建議先與授信確認")

fx_discount = st.sidebar.slider("錯幣折扣 (%)", 50, 100, 90, 5) / 100
utilization = st.sidebar.slider("額度動用比例 (%)", 50, 100, 90, 5) / 100

st.sidebar.subheader("4️⃣ 風險假設")
fund_decline = st.sidebar.slider(
    "基金淨值年侵蝕率 (%)", 0.0, 10.0, 3.0, 0.5,
    help="配息中屬於本金返還的部分，會造成淨值逐年下滑",
) / 100

stress_rate = st.sidebar.slider("壓力測試：Lombard 升至 (%)", 1.0, 12.0, 5.0, 0.25) / 100

# ============================================================
# 主畫面
# ============================================================

st.title("🏠 Lombard 槓桿 vs 房貸覆蓋率試算")

eff_ltv = blended_effective_ltv(w_bond, ltv_bond, w_fund, ltv_fund, fx_discount, utilization)
maint_ltv = blended_maintenance_ltv(w_bond, ltv_bond, w_fund, ltv_fund, fx_discount)
blended_yield = w_bond * yield_bond + w_fund * yield_fund

c1, c2, c3, c4 = st.columns(4)
c1.metric("有效質借成數", f"{eff_ltv * 100:.2f}%",
          help=f"加權LTV {(w_bond*ltv_bond + w_fund*ltv_fund)*100:.1f}% × "
               f"錯幣 {fx_discount*100:.0f}% × 動用 {utilization*100:.0f}%")
c2.metric("混合配息率", f"{blended_yield * 100:.2f}%")
c3.metric("維持成數（追繳線）", f"{maint_ltv * 100:.2f}%")
c4.metric("利差", f"{(blended_yield - lombard_rate) * 100:.2f}%",
          help="混合配息率 − Lombard 利率")

if principal > 0 and eff_ltv > 0:
    st.info(
        f"💡 這筆 **{principal:,} 萬** 本金來自房貸，"
        f"因此投資部位的自有資金為 **0**——真正在扛風險的是房子的淨值。"
    )

st.divider()

# ------------------------------------------------------------
# 借款次數比較（共用計算）
# ------------------------------------------------------------

rounds_list = [1, 2, 3, 4]
terms = [20, 30]

summary_rows = []
detail_store = {}

for r in rounds_list:
    ladder_df, total_position, total_borrow = build_ladder(principal, eff_ltv, r)
    detail_store[r] = ladder_df

    annual_income = total_position * blended_yield
    annual_interest = total_borrow * lombard_rate
    net_cf = annual_income - annual_interest
    net_cf_stress = annual_income - total_borrow * stress_rate

    trigger_asset = total_borrow / maint_ltv if maint_ltv > 0 else 0
    buffer = 1 - trigger_asset / total_position if total_position > 0 else 0

    surv = survival_years(
        total_position * w_bond, total_position * w_fund,
        total_borrow, maint_ltv, fund_decline,
    )

    row = {
        "借款次數": f"借 {r} 次",
        "總部位": total_position,
        "總借款": total_borrow,
        "槓桿倍數": total_position / principal if principal else 0,
        "年配息": annual_income,
        "年利息": annual_interest,
        "年淨現金流": net_cf,
        "追繳緩衝": buffer,
        "存活年數": surv,
        "壓力淨現金流": net_cf_stress,
    }

    for t in terms:
        pay = mortgage_annual_payment(principal, mortgage_rate, t)
        row[f"{t}年覆蓋率"] = net_cf / pay if pay else 0
        row[f"{t}年結餘"] = net_cf - pay
        row[f"{t}年壓力覆蓋率"] = net_cf_stress / pay if pay else 0

    summary_rows.append(row)

summary = pd.DataFrame(summary_rows)

# ------------------------------------------------------------
# 區塊一：部位結構
# ------------------------------------------------------------

st.subheader("① 部位結構")

pos_df = summary[["借款次數", "總部位", "總借款", "槓桿倍數"]].copy()
pos_df["總部位"] = pos_df["總部位"].map(fmt)
pos_df["總借款"] = pos_df["總借款"].map(fmt)
pos_df["槓桿倍數"] = pos_df["槓桿倍數"].map(lambda x: f"{x:.2f} 倍")
pos_df.columns = ["借款次數", "總部位（萬）", "總借款（萬）", "槓桿倍數"]
show_df(pos_df)

with st.expander("📋 查看逐輪明細"):
    pick = st.radio("選擇借款次數", rounds_list, index=1, horizontal=True,
                    format_func=lambda x: f"借 {x} 次")
    d = detail_store[pick].copy()
    d["買入金額"] = d["買入金額"].map(fmt)
    d["質押借出"] = d["質押借出"].map(fmt)
    d.columns = ["輪次", "買入金額（萬）", "質押借出（萬）"]
    show_df(d)

st.divider()

# ------------------------------------------------------------
# 區塊二：現金流與覆蓋率
# ------------------------------------------------------------

st.subheader("② 現金流與房貸覆蓋率")

pay_cols = st.columns(len(terms))
for idx, t in enumerate(terms):
    pay = mortgage_annual_payment(principal, mortgage_rate, t)
    pay_cols[idx].metric(
        f"{t} 年期房貸年繳", f"{fmt(pay)} 萬",
        f"月繳 {pay / 12 * 10000:,.0f} 元", delta_color="off",
    )

display = pd.DataFrame({
    "借款次數": summary["借款次數"],
    "年配息（萬）": summary["年配息"].map(fmt),
    "年利息（萬）": summary["年利息"].map(lambda x: f"-{fmt(x)}"),
    "年淨現金流（萬）": summary["年淨現金流"].map(fmt),
})
for t in terms:
    display[f"{t}年覆蓋率"] = summary[f"{t}年覆蓋率"].map(
        lambda x: f"{ICON[cover_label(x)]} {x:.2f} 倍"
    )
    display[f"{t}年結餘（萬）"] = summary[f"{t}年結餘"].map(lambda x: f"{x:+,.1f}")

show_df(display)
st.caption("✅ 覆蓋率 ≥1.3 倍（有緩衝）　⚠️ 1.0–1.3 倍（勉強）　❌ <1.0 倍（不足）")

st.markdown("**打平所需的最低混合配息率**")
be_rows = []
for _, r in summary.iterrows():
    row = {"借款次數": r["借款次數"]}
    for t in terms:
        pay = mortgage_annual_payment(principal, mortgage_rate, t)
        need = (pay + r["總借款"] * lombard_rate) / r["總部位"] if r["總部位"] else 0
        row[f"{t} 年期"] = f"{need * 100:.2f}%"
    be_rows.append(row)
show_df(pd.DataFrame(be_rows))

st.divider()

# ------------------------------------------------------------
# 區塊三：風險
# ------------------------------------------------------------

st.subheader("③ 風險評估")

r1, r2 = st.columns(2)

with r1:
    st.markdown("**Margin call 緩衝（淨值可跌幅度）**")
    mc = pd.DataFrame({
        "借款次數": summary["借款次數"],
        "借款/資產": (summary["總借款"] / summary["總部位"]).map(lambda x: f"{x*100:.1f}%"),
        "跌多少觸發": summary["追繳緩衝"].map(
            lambda x: f"{ICON[buffer_label(x)]} -{x*100:.1f}%"
        ),
    })
    show_df(mc)
    st.caption("參考：2022 年長天期投等債最大回檔逾 20%")

with r2:
    st.markdown(f"**淨值侵蝕存活年數**（基金年跌 {fund_decline*100:.1f}%）")
    sv = pd.DataFrame({
        "借款次數": summary["借款次數"],
        "幾年後追繳": summary["存活年數"].map(
            lambda x: "♾️ 不會觸發" if is_never(x)
            else f"{ICON[survival_label(x)]} 約 {x:.0f} 年"
        ),
    })
    show_df(sv)
    st.caption("假設債券持有到期、僅基金淨值侵蝕")

st.markdown(f"**壓力測試：Lombard 利率升至 {stress_rate*100:.2f}%**")
stress_display = pd.DataFrame({
    "借款次數": summary["借款次數"],
    "壓力後淨現金流（萬）": summary["壓力淨現金流"].map(fmt),
})
for t in terms:
    stress_display[f"{t}年覆蓋率"] = summary[f"{t}年壓力覆蓋率"].map(
        lambda x: f"{ICON[cover_label(x)]} {x:.2f} 倍"
    )
show_df(stress_display)

st.divider()

# ------------------------------------------------------------
# 區塊四：配息率敏感度
# ------------------------------------------------------------

st.subheader("④ 配息率敏感度")

sens_rounds = st.multiselect(
    "選擇要比較的借款次數", rounds_list, default=[2, 3],
    format_func=lambda x: f"借 {x} 次",
)

if sens_rounds:
    yields = [i / 100 for i in range(30, 101, 5)]
    chart_data = {}
    for r in sens_rounds:
        s = summary[summary["借款次數"] == f"借 {r} 次"].iloc[0]
        chart_data[f"借 {r} 次"] = [
            s["總部位"] * y - s["總借款"] * lombard_rate for y in yields
        ]

    chart_df = pd.DataFrame(chart_data, index=[f"{y*100:.0f}%" for y in yields])
    for t in terms:
        chart_df[f"{t}年房貸年繳"] = mortgage_annual_payment(principal, mortgage_rate, t)

    st.line_chart(chart_df, height=360)
    st.caption("橫軸：混合配息率　縱軸：年金額（萬元）。曲線高於水平線即表示 cover 得掉。")

st.divider()

# ------------------------------------------------------------
# 區塊五：輸出 PDF
# ------------------------------------------------------------

st.subheader("⑤ 輸出 PDF 摘要")

font_paths = find_cjk_font()

if font_paths is None:
    st.warning(
        "找不到可用的中文字型，PDF 無法產生。解法：\n\n"
        "- **Streamlit Cloud**：在 repo 根目錄新增 `packages.txt`，內容寫 `fonts-noto-cjk`\n"
        "- **本機 Windows／macOS**：通常自帶字型，若仍失敗請把字型檔放到專案的 `fonts/` 資料夾\n"
        "- **自備字型**：下載 Noto Sans TC，放成 `fonts/NotoSansTC-Regular.ttf`"
    )
else:
    st.caption(f"使用字型：`{font_paths[0]}`")
    if st.button("📄 產生 PDF 摘要報告", type="primary"):
        params = dict(
            principal=principal, mortgage_rate=mortgage_rate,
            w_bond=w_bond, w_fund=w_fund,
            yield_bond=yield_bond, yield_fund=yield_fund,
            ltv_bond=ltv_bond, ltv_fund=ltv_fund,
            fx_discount=fx_discount, utilization=utilization,
            lombard_rate=lombard_rate, blended_yield=blended_yield,
            eff_ltv=eff_ltv, maint_ltv=maint_ltv,
            fund_decline=fund_decline, stress_rate=stress_rate,
        )
        try:
            pdf_bytes = build_pdf(params, summary, terms, rounds_list, font_paths)
            st.session_state["pdf_bytes"] = pdf_bytes
            st.success("PDF 已產生，可按下方按鈕下載。")
        except Exception as e:  # noqa: BLE001
            st.error(f"PDF 產生失敗：{e}")

    if st.session_state.get("pdf_bytes"):
        st.download_button(
            "⬇️ 下載 PDF",
            data=st.session_state["pdf_bytes"],
            file_name=f"Lombard槓桿房貸試算_{date.today().isoformat()}.pdf",
            mime="application/pdf",
        )

st.divider()

with st.expander("📌 模型假設與限制"):
    st.markdown("""
- **本金來自房貸**，投資部位自有資金為 0，實質風險由房屋淨值承擔
- 配息率視為穩定現金流，未計入債券價格波動、提前買回、違約
- 未計入匯率損益。錯幣折扣是銀行對匯率風險的定價，不等於你沒有風險
- 未計入海外所得最低稅負、信託／保管費、基金手續費
- 房貸為本息均攤、利率固定；Lombard 利率實務上為浮動
- 債券「持有到期還本」僅保原幣本金，不保台幣本金
- 30 年期間債券需再投資數輪，存在再投資風險
- **基金質借成數實務上常低於債券**，請先與授信確認後再調整參數
    """)

st.caption("此工具僅供內部試算與教育用途，不構成投資建議。")
