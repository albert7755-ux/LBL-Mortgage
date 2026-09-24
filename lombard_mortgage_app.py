"""
Lombard 槓桿 vs 房貸覆蓋率試算器
=================================
兩種本金來源：
  A. 自有資金（無成本）——本金不用還、不付息，看的是自有資金報酬率
  B. 房貸借出（有成本）——本金需還本付息，看的是配息能否覆蓋房貸本息

執行方式：streamlit run lombard_mortgage_app.py
"""

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

def credit_line_rate(w_bond, ltv_bond, w_fund, ltv_fund, fx_discount):
    """
    可動用額度成數 = 加權LTV × 錯幣折扣
    這同時是「額度的分母基準」與「使用率 100%（追繳）」的臨界成數
    """
    weighted_ltv = w_bond * ltv_bond + w_fund * ltv_fund
    return weighted_ltv * fx_discount


def build_ladder(principal, rate, draw_ratio, last_draw_ratio, layers):
    """
    層次化質借階梯
      原始層  = 自有本金買入的債券（設質，尚未借款）
      第 N 層 = 用第 N-1 層的額度借出、買入並設質的債券
    最後一層可單獨設定動用比例；設 0% 即「只設質、不動用額度」
    """
    rows = [{"層次": "原始投入", "買入金額": principal, "質押借出": 0.0}]
    current = principal
    total_position = principal
    total_borrow = 0.0

    for i in range(1, layers + 1):
        dr = last_draw_ratio if i == layers else draw_ratio
        borrowed = current * rate * dr
        rows.append({"層次": f"第 {i} 層", "買入金額": borrowed, "質押借出": borrowed})
        total_borrow += borrowed
        total_position += borrowed
        current = borrowed

    rows.append({"層次": "合計", "買入金額": total_position, "質押借出": total_borrow})
    return pd.DataFrame(rows), total_position, total_borrow


LAYER_NAMES = {0: "原始層", 1: "第一層", 2: "第二層", 3: "第三層",
               4: "第四層", 5: "第五層", 6: "第六層"}


def utilization_ratio(total_borrow, collateral, rate, fx_shock=0.0):
    """
    使用率 = 借款 ÷ (擔保品市值 × 可動用額度成數)
    fx_shock：匯率不利變動幅度，直接折損擔保品在貸款幣別下的價值
    """
    limit = collateral * (1 - fx_shock) * rate
    return total_borrow / limit if limit > 0 else 0.0


def value_drop_to(total_borrow, collateral, rate, threshold, fx_shock=0.0):
    """
    在匯率已不利變動 fx_shock 的前提下，擔保品「價格」還能再跌多少才碰到某條線
    觸發條件：借款 = threshold × rate × 部位 × (1-價格跌幅) × (1-匯率跌幅)
    回傳可能為負值，代表已經觸發
    """
    if rate <= 0 or threshold <= 0 or collateral <= 0 or fx_shock >= 1:
        return 0.0
    base = total_borrow / (threshold * rate * collateral)
    return 1 - base / (1 - fx_shock)


def mortgage_annual_payment(principal, annual_rate, years):
    """房貸本息均攤——年繳金額（含本金攤還＋利息）"""
    if principal <= 0:
        return 0.0
    if annual_rate <= 0:
        return principal / years
    i = annual_rate / 12
    n = years * 12
    monthly = principal * i / (1 - (1 + i) ** (-n))
    return monthly * 12


def survival_years(bond_position, fund_position, total_borrow, rate, threshold,
                   fund_decline, fx_shock=0.0):
    """基金淨值逐年侵蝕下，幾年後碰到追繳線（債券假設持有到期不計價格變動）"""
    if fund_decline <= 0 or rate <= 0 or threshold <= 0 or fx_shock >= 1:
        return None
    trigger_value = total_borrow / (threshold * rate * (1 - fx_shock))
    if trigger_value <= bond_position:
        return None
    if fund_position <= 0:
        return None
    ratio = (trigger_value - bond_position) / fund_position
    if ratio >= 1:
        return 0.0
    return math.log(ratio) / math.log(1 - fund_decline)


# ============================================================
# 顯示輔助
# ============================================================

def fmt(x):
    """數字格式化為萬元（一位小數）"""
    return f"{x:,.1f}"


def cover_label(x):
    if x >= 1.3:
        return "充足"
    if x >= 1.0:
        return "勉強"
    return "不足"


def buffer_label(x):
    if x >= 0.25:
        return "充足"
    if x >= 0.18:
        return "偏薄"
    return "危險"


def is_never(x):
    """pandas 會把 None 轉成 NaN，需一併處理"""
    return x is None or (isinstance(x, float) and math.isnan(x))


def survival_label(x):
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

# 匯率雙因子矩陣要跑幾檔情境
FX_SCENARIO_COUNT = 5


def fx_shock_from_rate(rate_now, rate_base):
    """
    由匯率報價換算成擔保品價值折損率
    擔保品為外幣、貸款為台幣時，報價下跌（台幣升值）即為不利
    回傳負值代表匯率有利
    """
    if rate_base <= 0:
        return 0.0
    return 1 - rate_now / rate_base


def drop_cell(x, with_icon=True, borrow=None):
    """把「還能跌多少」格式化；負值代表已觸發，無借款則不適用"""
    if borrow is not None and borrow <= 0:
        return "♾️ 無借款" if with_icon else "無借款"
    if x <= 0:
        return "❌ 已觸發" if with_icon else "已觸發"
    return f"{ICON[buffer_label(x)]} -{x*100:.1f}%" if with_icon else f"-{x*100:.1f}%"


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
    ("fonts/NotoSansTC-Regular.ttf", "fonts/NotoSansTC-Bold.ttf"),
    ("fonts/msjh.ttc", "fonts/msjhbd.ttc"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", None),
    ("C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msjhbd.ttc"),
    ("C:/Windows/Fonts/mingliu.ttc", None),
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

def build_pdf(params, summary, terms, font_paths):
    """產生 PDF 摘要報告，回傳 bytes"""
    from fpdf import FPDF

    regular, bold = font_paths
    p = params
    has_mortgage = p["mortgage_amount"] > 0
    own_funded = not p["is_mortgage_funded"]

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.add_font("cjk", "", regular)
    pdf.add_font("cjk", "B", bold if bold else regular)
    pdf.add_page()

    PAGE_W = 180

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
        pdf.multi_cell(0, 5, text, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 20, 20)

    def table(headers, rows, weights, align=None):
        """weights 為相對寬度，自動縮放到 180mm"""
        total = sum(weights)
        widths = [w / total * PAGE_W for w in weights]
        align = align or ["C"] * len(headers)
        pdf.set_font("cjk", "B", 8.5)
        pdf.set_fill_color(235, 240, 248)
        for h, w in zip(headers, widths):
            pdf.cell(w, 7, h, border=1, align="C", fill=True)
        pdf.ln()
        pdf.set_font("cjk", "", 8.5)
        for idx, row in enumerate(rows):
            pdf.set_fill_color(250, 250, 250) if idx % 2 else pdf.set_fill_color(255, 255, 255)
            for cell, w, a in zip(row, widths, align):
                pdf.cell(w, 6.5, str(cell), border=1, align=a, fill=True)
            pdf.ln()

    # ---------- 標題 ----------
    title("Lombard 槓桿試算報告")
    pdf.set_font("cjk", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, f"本金性質：{p['capital_source']}　｜　產製日期：{date.today().isoformat()}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    pdf.ln(2)

    # ---------- 參數摘要 ----------
    section("一、參數設定")
    param_rows = [
        ["本金", f"{p['principal']:,} 萬", "本金性質", p["capital_source"]],
        ["債券比重", f"{p['w_bond']*100:.0f}%", "債券配息率", f"{p['yield_bond']*100:.2f}%"],
        ["基金比重", f"{p['w_fund']*100:.0f}%", "基金配息率", f"{p['yield_fund']*100:.2f}%"],
        ["債券 LTV", f"{p['ltv_bond']*100:.0f}%", "基金 LTV", f"{p['ltv_fund']*100:.0f}%"],
        ["錯幣折扣", f"{p['fx_discount']*100:.0f}%", "可動用額度成數", f"{p['credit_rate']*100:.2f}%"],
        ["Lombard 利率", f"{p['lombard_rate']*100:.2f}%", "混合配息率", f"{p['blended_yield']*100:.2f}%"],
        ["每層實際動用", f"{p['draw_ratio']*100:.0f}%",
         "最後一層動用", f"{p['last_draw_ratio']*100:.0f}%"],
        ["追加設質", f"{p['extra_pledge']:,} 萬",
         "", ""],
        ["通知線（使用率）", f"{p['notice_line']*100:.0f}%",
         "追繳線（使用率）", f"{p['call_line']*100:.0f}%"],
        [f"建倉匯率（{p['fx_pair']}）", f"{p['fx_base']:.2f}",
         "評估匯率", f"{p['fx_now']:.2f}"],
    ]
    if has_mortgage:
        param_rows.append(["房貸金額", f"{p['mortgage_amount']:,} 萬",
                           "房貸利率", f"{p['mortgage_rate']*100:.2f}%"])
    table(["項目", "數值", "項目", "數值"], param_rows, [45, 45, 45, 45],
          align=["L", "R", "L", "R"])

    # ---------- 部位結構 ----------
    section("二、部位結構")
    pos_rows = [
        [r["層次"], fmt(r["總部位"]), fmt(r["總借款"]),
         fmt(r["擔保品"]), f"{r['槓桿倍數']:.2f} 倍"]
        for _, r in summary.iterrows()
    ]
    table(["層次", "總部位（萬）", "總借款（萬）", "擔保品（萬）", "槓桿倍數"], pos_rows,
          [30, 40, 40, 40, 30], align=["C", "R", "R", "R", "C"])

    # ---------- 現金流 ----------
    section("三、現金流與房貸覆蓋率" if has_mortgage else "三、現金流與自有資金報酬率")

    if has_mortgage:
        note("　｜　".join(
            f"{t} 年期房貸年繳 {fmt(mortgage_annual_payment(p['mortgage_amount'], p['mortgage_rate'], t))} 萬"
            f"（含本金攤還＋利息）" if t == terms[0] else
            f"{t} 年期房貸年繳 {fmt(mortgage_annual_payment(p['mortgage_amount'], p['mortgage_rate'], t))} 萬"
            for t in terms
        ))
        pdf.ln(1)

    headers = ["層次", "年配息", "Lombard年息", "投資端年淨現金流"]
    weights = [22, 20, 24, 30]
    align = ["C", "R", "R", "R"]
    if own_funded:
        headers.append("自有資金年報酬率")
        weights.append(26)
        align.append("R")
    for t in terms if has_mortgage else []:
        headers += [f"{t}年覆蓋率", f"{t}年扣房貸後結餘"]
        weights += [24, 26]
        align += ["C", "R"]

    cf_rows = []
    for _, r in summary.iterrows():
        row = [r["層次"], fmt(r["年配息"]),
               fmt(0) if r["年利息"] <= 0 else "-" + fmt(r["年利息"]),
               fmt(r["年淨現金流"])]
        if own_funded:
            row.append(f"{r['自有資金報酬率']*100:.2f}%")
        for t in (terms if has_mortgage else []):
            cov = r[f"{t}年覆蓋率"]
            row += [f"{cov:.2f} 倍 {cover_label(cov)}", f"{r[f'{t}年結餘']:+.1f}"]
        cf_rows.append(row)
    table(headers, cf_rows, weights, align=align)
    note("「投資端年淨現金流」＝年配息 − Lombard 年息，尚未扣除房貸。"
         + ("　「扣房貸後結餘」才是全部扣完的數字。" if has_mortgage else ""))

    # 打平配息率
    if has_mortgage:
        pdf.ln(2)
        pdf.set_font("cjk", "B", 9)
        pdf.cell(0, 6, "打平所需的最低混合配息率", new_x="LMARGIN", new_y="NEXT")
        be_rows = []
        for _, r in summary.iterrows():
            row = [r["層次"]]
            for t in terms:
                pay = mortgage_annual_payment(p["mortgage_amount"], p["mortgage_rate"], t)
                need = (pay + r["總借款"] * p["lombard_rate"]) / r["總部位"] if r["總部位"] else 0
                row.append(f"{need*100:.2f}%")
            be_rows.append(row)
        table(["層次"] + [f"{t} 年期" for t in terms], be_rows,
              [60] + [60] * len(terms))

    # ---------- 風險 ----------
    if pdf.get_y() > 215:
        pdf.add_page()
    section("四、風險評估")
    risk_rows = []
    for _, r in summary.iterrows():
        surv = r["存活年數"]
        surv_text = "不會觸發" if is_never(surv) else f"約 {surv:.0f} 年（{survival_label(surv)}）"
        risk_rows.append([
            r["層次"],
            fmt(r["可動用額度"]),
            f"{r['目前使用率']*100:.1f}%",
            drop_cell(r["距通知線"], with_icon=False, borrow=r["總借款"]),
            drop_cell(r["距追繳線"], with_icon=False, borrow=r["總借款"]),
            surv_text,
        ])
    table(["層次", "可動用額度（萬）", "目前使用率",
           f"距通知線{p['notice_line']*100:.0f}%", f"距追繳線{p['call_line']*100:.0f}%",
           "淨值侵蝕存活年數"],
          risk_rows, [24, 34, 26, 28, 36, 38], align=["C", "R", "R", "R", "C", "C"])
    note("使用率 ＝ 借款 ÷（擔保品市值 × LTV × 錯幣折扣）。"
         + (f"　已套用 {p['fx_pair']} {p['fx_base']:.2f} → {p['fx_now']:.2f}。"
            if abs(p["fx_shock"]) > 1e-9 else "")
         + f"　淨值侵蝕假設：基金淨值年跌 {p['fund_decline']*100:.1f}%，債券持有到期不計價格變動。")

    # 匯率 × 債價 雙因子矩陣（整塊約 50mm，不夠就換頁）
    if pdf.get_y() > 220:
        pdf.add_page()
    pdf.ln(2)
    pdf.set_font("cjk", "B", 9)
    scen = p["fx_scenarios"]
    pdf.cell(0, 6,
             f"{p['fx_pair']} × 債券價格 雙因子："
             f"距追繳線（{p['call_line']*100:.0f}%）債券還能再跌多少",
             new_x="LMARGIN", new_y="NEXT")
    fx_headers = ["層次"] + [
        f"{q:.2f}" + ("（建倉）" if abs(q - p["fx_base"]) < 1e-9 else "") for q in scen
    ]
    fx_rows = [
        [r["層次"]] + [drop_cell(r["匯率情境"][q], with_icon=False, borrow=r["總借款"])
                      for q in scen]
        for _, r in summary.iterrows()
    ]
    table(fx_headers, fx_rows, [24] + [26] * len(scen),
          align=["C"] + ["R"] * len(scen))
    note(f"欄位為 {p['fx_pair']} 報價（建倉匯率 {p['fx_base']:.2f}）。"
         "錯幣結構下匯率與債券價格為相乘關係，"
         "數字為「匯率走到該價位後，債券價格還能再跌多少」才碰到追繳線。")

    # ---------- 假設 ----------
    if pdf.get_y() > 195:
        pdf.add_page()
    section("五、模型假設與限制")

    lines = []
    if p["is_mortgage_funded"]:
        lines.append("本金來自房貸，投資部位自有資金為 0，實質風險由房屋淨值承擔。")
    else:
        lines.append("本金為自有資金，無資金成本；margin call 的損失範圍限於投入的自有資金。")
    lines += [
        "配息率視為穩定現金流，未計入債券價格波動、提前買回、違約風險。",
        "匯率損益未計入配息現金流；margin call 部分已依「評估匯率」重估擔保品價值。",
        "未計入海外所得最低稅負、信託／保管費、基金手續費。",
        "房貸假設為本息均攤、利率固定；Lombard 利率實務上為浮動。",
        "債券「持有到期還本」僅保原幣本金，不保台幣本金。",
        "長年期間債券需再投資數輪，存在再投資風險。",
        "基金質借成數實務上常低於債券，請先與授信單位確認後再調整參數。",
    ]
    for line in lines:
        note("・" + line, size=9)

    pdf.ln(4)
    pdf.set_font("cjk", "", 8)
    pdf.set_text_color(140, 140, 140)
    pdf.multi_cell(0, 4.5, align="L", text=
                   "本報告由試算工具自動產生，僅供內部試算與教育用途，不構成投資建議。"
                   "實際條件以各行授信規定與商品文件為準。")

    out = pdf.output()
    return bytes(out)


# ============================================================
# 側邊欄：參數輸入
# ============================================================

st.sidebar.header("⚙️ 參數設定")

st.sidebar.subheader("1️⃣ 資金來源")

capital_source = st.sidebar.radio(
    "本金性質",
    ["自有資金（無成本）", "房貸借出（有成本）"],
    index=0,
    help="決定這筆本金本身要不要還本付息",
)
is_mortgage_funded = capital_source.startswith("房貸")

principal = st.sidebar.number_input(
    "本金（萬元）", min_value=100, max_value=100000, value=1000, step=100,
)

if is_mortgage_funded:
    mortgage_amount = principal
    st.sidebar.caption(f"房貸金額自動等於本金：**{principal:,} 萬**")
else:
    mortgage_amount = st.sidebar.number_input(
        "另有房貸要覆蓋（萬元）", min_value=0, max_value=100000, value=0, step=100,
        help="填 0 表示不比較房貸，只看自有資金報酬率",
    )

if mortgage_amount > 0:
    mortgage_rate = st.sidebar.slider("房貸利率 (%)", 0.5, 8.0, 2.65, 0.05) / 100
else:
    mortgage_rate = 0.0

has_mortgage = mortgage_amount > 0
own_funded = not is_mortgage_funded

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

st.sidebar.subheader("3️⃣ 銀行給的條件")
st.sidebar.caption("這一組決定**額度多大**，也決定**追繳線在哪**")

lombard_rate = st.sidebar.slider("Lombard 利率 (%)", 0.5, 10.0, 2.65, 0.05) / 100

col_l1, col_l2 = st.sidebar.columns(2)
with col_l1:
    ltv_bond = st.number_input("債券 LTV (%)", 0, 100, 75, 5) / 100
with col_l2:
    ltv_fund = st.number_input("基金 LTV (%)", 0, 100, 75, 5) / 100
st.sidebar.caption("⚠️ 基金的實際成數常低於債券，建議先與授信確認")

fx_discount = st.sidebar.slider(
    "錯幣折扣 (%)", 50, 100, 90, 5,
    help="錯幣時銀行對擔保品價值的折扣；本幣擔保請設 100%",
) / 100

st.sidebar.subheader("4️⃣ 你要借多少")
st.sidebar.caption("這一組只決定**實際借多少**，追繳線不動")

draw_ratio = st.sidebar.slider(
    "每層實際動用 (%)", 0, 100, 100, 5,
    help="最後一層以外，每層實際借出可動用額度的幾成",
) / 100

last_draw_ratio = st.sidebar.slider(
    "最後一層動用 (%)", 0, 100, 100, 5,
    help="只作用在每一列的最後一層。設 0% ＝ 只設質、不動用額度",
) / 100

st.sidebar.markdown("**要試算到第幾層**")
LAYER_DEFAULTS = {1: True, 2: True, 3: True, 4: False, 5: False, 6: False}
show_layer = {}
ly_cols = st.sidebar.columns(3)
for n in range(1, 7):
    with ly_cols[(n - 1) % 3]:
        show_layer[n] = st.checkbox(LAYER_NAMES[n], value=LAYER_DEFAULTS[n],
                                    key=f"layer_{n}")
st.sidebar.caption("原始層（不借款）一律顯示，作為對照基準")

extra_pledge = st.sidebar.number_input(
    "追加設質（萬元）", min_value=0, max_value=100000, value=0, step=100,
    help="客戶名下其他已持有、額外設質進擔保池的債券。只增加擔保品，不增加借款與配息",
)

st.sidebar.subheader("5️⃣ 追繳門檻")
notice_line = st.sidebar.slider("通知線：使用率 (%)", 70, 100, 95, 1) / 100
call_line = st.sidebar.slider("追繳線：使用率 (%)", 80, 120, 100, 1) / 100

st.sidebar.subheader("6️⃣ 風險假設")
fx_pair = st.sidebar.text_input("幣別對", value="USD/TWD")

col_fx1, col_fx2 = st.sidebar.columns(2)
with col_fx1:
    fx_base = st.number_input(
        "建倉匯率", min_value=0.01, max_value=1000.0, value=31.00, step=0.05,
        format="%.2f", help="建立部位當下的匯率，作為基準",
    )
with col_fx2:
    fx_now = st.number_input(
        "評估匯率", min_value=0.01, max_value=1000.0, value=31.00, step=0.05,
        format="%.2f", help="要評估的匯率。低於建倉匯率＝台幣升值＝擔保品縮水",
    )

fx_step = st.sidebar.number_input(
    "情境間距", min_value=0.05, max_value=10.0, value=0.50, step=0.05, format="%.2f",
    help="雙因子矩陣每一欄往下遞減的幅度",
)

fx_shock = fx_shock_from_rate(fx_now, fx_base)
if abs(fx_shock) > 1e-9:
    direction = "台幣升值，擔保品縮水" if fx_shock > 0 else "台幣貶值，擔保品增值"
    st.sidebar.caption(
        f"{fx_base:.2f} → {fx_now:.2f}　**{-fx_shock*100:+.2f}%**　（{direction}）"
    )
fund_decline = st.sidebar.slider(
    "基金淨值年侵蝕率 (%)", 0.0, 10.0, 3.0, 0.5,
    help="配息中屬於本金返還的部分，會造成淨值逐年下滑",
) / 100
stress_rate = st.sidebar.slider("壓力測試：Lombard 升至 (%)", 1.0, 12.0, 5.0, 0.25) / 100

# ============================================================
# 主畫面
# ============================================================

head_l, head_r = st.columns([5, 1])
with head_l:
    st.title("🏦 Lombard 槓桿試算")
with head_r:
    st.write("")
    if st.button("🔄 重新計算", help="強制重新整理所有表格與圖表"):
        st.rerun()

credit_rate = credit_line_rate(w_bond, ltv_bond, w_fund, ltv_fund, fx_discount)
blended_yield = w_bond * yield_bond + w_fund * yield_fund

c1, c2, c3, c4 = st.columns(4)
c1.metric("可動用額度成數", f"{credit_rate * 100:.2f}%",
          help=f"加權LTV {(w_bond*ltv_bond + w_fund*ltv_fund)*100:.1f}% × "
               f"錯幣折扣 {fx_discount*100:.0f}%")
c2.metric("混合配息率", f"{blended_yield * 100:.2f}%")
c3.metric("通知／追繳線",
          f"{notice_line*100:.0f}% / {call_line*100:.0f}%",
          help="使用率 = 借款 ÷（擔保品市值 × 可動用額度成數）")
c4.metric("利差", f"{(blended_yield - lombard_rate) * 100:.2f}%",
          help="混合配息率 − Lombard 利率")

weighted_ltv = w_bond * ltv_bond + w_fund * ltv_fund

with st.expander("🧮 公式拆解：算式怎麼跑的", expanded=False):
    st.markdown(f"""
**第一步｜銀行給你多少額度**（側邊欄 3️⃣）

```
可動用額度成數 = 加權LTV {weighted_ltv*100:.1f}% × 錯幣折扣 {fx_discount*100:.0f}% = {credit_rate*100:.2f}%
可動用額度     = 擔保品市值 × {credit_rate*100:.2f}%
```

這個成數同時就是**追繳線**——借款一旦等於可動用額度，使用率就是 100%。

**第二步｜你實際借多少**（側邊欄 4️⃣）

```
每層借款 = 該層可動用額度 × 每層實際動用 {draw_ratio*100:.0f}%
         （最後一層改用 {last_draw_ratio*100:.0f}%）
```

**第三步｜使用率**

```
使用率 = 借款金額 ÷ 可動用額度   → 達 {notice_line*100:.0f}% 通知，達 {call_line*100:.0f}% 補繳
```

---

**銀行條件 vs 你的決定**

- **3️⃣ 的參數（LTV、錯幣折扣）是銀行給的**，決定額度大小，追繳線也跟著跑
- **4️⃣ 的參數（動用比例）是你決定的**，只影響借多少，追繳線站在原地

所以想降低使用率，能動的只有 4️⃣ 那一組，以及「追加設質」。

---

**本金是自有資金還是房貸借來的，完全不影響這裡**

Lombard 額度只看「設質進去的擔保品」，不問這筆錢從哪來。
同樣的部位，額度、使用率、追繳線三者都一模一樣。

差別只在**被追繳時的後果**：自有資金的損失上限是投入的本金；
房貸借來的，斷頭後房貸債務還在，缺口要由房子的淨值吸收。
""")

st.caption(
    "**使用率 ＝ 借款金額 ÷（擔保品市值 × LTV × 錯幣折扣）**　"
    f"→ 達 {notice_line*100:.0f}% 啟動通知，達 {call_line*100:.0f}% 須補繳"
)

if is_mortgage_funded:
    st.warning(
        f"⚠️ **本金 {principal:,} 萬來自房貸**，投資部位的自有資金為 **0**。"
        f"真正在扛風險的是房子的淨值，下方覆蓋率已扣除房貸的**本金攤還＋利息**。"
    )
elif has_mortgage:
    st.info(
        f"💡 本金 {principal:,} 萬為**自有資金，無資金成本**；"
        f"另有 **{mortgage_amount:,} 萬**房貸需要覆蓋。"
    )
else:
    st.success(
        f"✅ 本金 {principal:,} 萬為**自有資金，無資金成本**。"
        f"無房貸比較，下方以**自有資金年報酬率**評估。"
    )

st.divider()

# ------------------------------------------------------------
# 共用計算
# ------------------------------------------------------------

layers_list = [0] + [n for n in range(1, 7) if show_layer[n]]
terms = [20, 30]

# 雙因子矩陣的情境匯率：自建倉匯率往下每檔遞減 fx_step
fx_scenarios = [round(fx_base - i * fx_step, 4) for i in range(FX_SCENARIO_COUNT)]

summary_rows = []
detail_store = {}

for r in layers_list:
    ladder_df, total_position, total_borrow = build_ladder(
        principal, credit_rate, draw_ratio, last_draw_ratio, r)
    detail_store[r] = ladder_df
    collateral = total_position + extra_pledge

    annual_income = total_position * blended_yield
    annual_interest = total_borrow * lombard_rate
    net_cf = annual_income - annual_interest
    net_cf_stress = annual_income - total_borrow * stress_rate

    util_now = utilization_ratio(total_borrow, collateral, credit_rate, fx_shock)
    drop_notice = value_drop_to(total_borrow, collateral, credit_rate,
                                notice_line, fx_shock)
    drop_call = value_drop_to(total_borrow, collateral, credit_rate,
                              call_line, fx_shock)

    surv = survival_years(
        collateral * w_bond, collateral * w_fund,
        total_borrow, credit_rate, call_line, fund_decline, fx_shock,
    )

    fx_grid = {
        rate_q: value_drop_to(total_borrow, collateral, credit_rate, call_line,
                              fx_shock_from_rate(rate_q, fx_base))
        for rate_q in fx_scenarios
    }

    row = {
        "層次": LAYER_NAMES[r],
        "總部位": total_position,
        "總借款": total_borrow,
        "擔保品": collateral,
        "可動用額度": collateral * (1 - fx_shock) * credit_rate,
        "槓桿倍數": total_position / principal if principal else 0,
        "年配息": annual_income,
        "年利息": annual_interest,
        "年淨現金流": net_cf,
        "自有資金報酬率": net_cf / principal if principal else 0,
        "目前使用率": util_now,
        "距通知線": drop_notice,
        "距追繳線": drop_call,
        "匯率情境": fx_grid,
        "存活年數": surv,
        "壓力淨現金流": net_cf_stress,
    }

    for t in terms:
        pay = mortgage_annual_payment(mortgage_amount, mortgage_rate, t)
        row[f"{t}年覆蓋率"] = net_cf / pay if pay else 0
        row[f"{t}年結餘"] = net_cf - pay
        row[f"{t}年壓力覆蓋率"] = net_cf_stress / pay if pay else 0

    summary_rows.append(row)

summary = pd.DataFrame(summary_rows)

# ------------------------------------------------------------
# 區塊一：部位結構
# ------------------------------------------------------------

st.subheader("① 部位結構")

pos_df = summary[["層次", "總部位", "總借款", "槓桿倍數"]].copy()
pos_df["總部位"] = pos_df["總部位"].map(fmt)
pos_df["總借款"] = pos_df["總借款"].map(fmt)
pos_df["槓桿倍數"] = pos_df["槓桿倍數"].map(lambda x: f"{x:.2f} 倍")
pos_df.columns = ["層次", "總部位（萬）", "總借款（萬）", "槓桿倍數"]
show_df(pos_df)

with st.expander("📋 查看逐輪明細"):
    pick = st.radio("選擇層次", layers_list, index=min(2, len(layers_list) - 1),
                    horizontal=True, format_func=lambda x: LAYER_NAMES[x],
                    key=f"detail_{'-'.join(map(str, layers_list))}")
    d = detail_store[pick].copy()
    d["買入金額"] = d["買入金額"].map(fmt)
    d["質押借出"] = d["質押借出"].map(fmt)
    d.columns = ["層次", "買入金額（萬）", "質押借出（萬）"]
    show_df(d)

st.divider()

# ------------------------------------------------------------
# 區塊二：現金流
# ------------------------------------------------------------

st.subheader("② 現金流與房貸覆蓋率" if has_mortgage else "② 現金流與自有資金報酬率")

if has_mortgage:
    pay_cols = st.columns(len(terms))
    for idx, t in enumerate(terms):
        pay = mortgage_annual_payment(mortgage_amount, mortgage_rate, t)
        total_paid = pay * t
        pay_cols[idx].metric(
            f"{t} 年期房貸年繳（本金＋利息）", f"{fmt(pay)} 萬",
            f"月繳 {pay / 12 * 10000:,.0f} 元　總繳 {total_paid:,.0f} 萬"
            f"（利息 {total_paid - mortgage_amount:,.0f} 萬）",
            delta_color="off",
        )

display = pd.DataFrame({
    "層次": summary["層次"],
    "年配息（萬）": summary["年配息"].map(fmt),
    "Lombard年息（萬）": summary["年利息"].map(lambda x: fmt(0) if x <= 0 else f"-{fmt(x)}"),
    "投資端年淨現金流（未扣房貸）": summary["年淨現金流"].map(fmt),
})
if own_funded:
    display["自有資金年報酬率"] = summary["自有資金報酬率"].map(lambda x: f"{x*100:.2f}%")
if has_mortgage:
    for t in terms:
        display[f"{t}年覆蓋率"] = summary[f"{t}年覆蓋率"].map(
            lambda x: f"{ICON[cover_label(x)]} {x:.2f} 倍"
        )
        display[f"{t}年扣房貸後結餘（萬）"] = summary[f"{t}年結餘"].map(lambda x: f"{x:+,.1f}")

show_df(display)

if has_mortgage:
    st.caption(
        "「投資端年淨現金流」＝年配息 − Lombard 年息，**尚未扣房貸**；"
        "「扣房貸後結餘」才是全部扣完的數字。　"
        "✅ 覆蓋率 ≥1.3 倍　⚠️ 1.0–1.3 倍　❌ <1.0 倍"
    )

    st.markdown("**打平所需的最低混合配息率**")
    be_rows = []
    for _, r in summary.iterrows():
        row = {"層次": r["層次"]}
        for t in terms:
            pay = mortgage_annual_payment(mortgage_amount, mortgage_rate, t)
            need = (pay + r["總借款"] * lombard_rate) / r["總部位"] if r["總部位"] else 0
            row[f"{t} 年期"] = f"{need * 100:.2f}%"
        be_rows.append(row)
    show_df(pd.DataFrame(be_rows))
else:
    st.caption(
        "「自有資金年報酬率」＝投資端年淨現金流 ÷ 本金。"
        "本金無成本，不需還本付息，這個數字就是這筆錢的年化現金收益率。"
    )

st.divider()

# ------------------------------------------------------------
# 區塊三：風險
# ------------------------------------------------------------

st.subheader("③ 風險評估")

st.markdown("**額度使用率與擔保品可跌幅度**")
mc = pd.DataFrame({
    "層次": summary["層次"],
    "借款（萬）": summary["總借款"].map(fmt),
    "擔保品（萬）": summary["擔保品"].map(fmt),
    "可動用額度（萬）": summary["可動用額度"].map(fmt),
    "目前使用率": summary["目前使用率"].map(
        lambda x: (
            f"❌ {x*100:.1f}%（已達追繳）" if x >= call_line
            else f"⚠️ {x*100:.1f}%（已達通知）" if x >= notice_line
            else f"🟡 {x*100:.1f}%" if x >= notice_line * 0.9
            else f"✅ {x*100:.1f}%"
        )
    ),
    f"距通知線（{notice_line*100:.0f}%）可跌": [
        drop_cell(x, borrow=b) for x, b in zip(summary["距通知線"], summary["總借款"])
    ],
    f"距追繳線（{call_line*100:.0f}%）可跌": [
        drop_cell(x, borrow=b) for x, b in zip(summary["距追繳線"], summary["總借款"])
    ],
})
show_df(mc)
st.caption(
    "使用率 ＝ 借款 ÷（擔保品市值 × LTV × 錯幣折扣）。"
    "　✅ 可跌 ≥25%　⚠️ 18–25%　❌ <18%　｜　參考：2022 年長天期投等債最大回檔逾 20%"
    + (f"　**已套用 {fx_pair} {fx_base:.2f} → {fx_now:.2f}**" if abs(fx_shock) > 1e-9 else "")
)

# ---- 匯率 × 債價 雙因子矩陣 ----
st.markdown(
    f"**{fx_pair} × 債券價格 雙因子：距追繳線（{call_line*100:.0f}%）債券還能再跌多少**"
)
fx_matrix = pd.DataFrame({
    "層次": summary["層次"],
    **{
        f"{rate_q:.2f}" + ("（建倉）" if abs(rate_q - fx_base) < 1e-9 else ""):
            [drop_cell(g[rate_q], borrow=b)
             for g, b in zip(summary["匯率情境"], summary["總借款"])]
        for rate_q in fx_scenarios
    },
})
show_df(fx_matrix)
st.caption(
    f"欄位為 {fx_pair} 報價（建倉匯率 {fx_base:.2f}）。"
    "錯幣結構下，匯率與債券價格是**相乘**關係，不是相加。"
    "數字代表「匯率走到該價位後，債券價格還能再跌多少」才碰到追繳線。"
)

st.markdown(f"**淨值侵蝕存活年數**（基金年跌 {fund_decline*100:.1f}%）")
sv = pd.DataFrame({
    "層次": summary["層次"],
    "幾年後碰到追繳線": summary["存活年數"].map(
        lambda x: "♾️ 不會觸發" if is_never(x)
        else f"{ICON[survival_label(x)]} 約 {x:.0f} 年"
    ),
})
show_df(sv)
st.caption("假設債券持有到期、僅基金淨值侵蝕")

st.markdown(f"**壓力測試：Lombard 利率升至 {stress_rate*100:.2f}%**")
stress_display = pd.DataFrame({
    "層次": summary["層次"],
    "壓力後淨現金流（萬）": summary["壓力淨現金流"].map(fmt),
})
if has_mortgage:
    for t in terms:
        stress_display[f"{t}年覆蓋率"] = summary[f"{t}年壓力覆蓋率"].map(
            lambda x: f"{ICON[cover_label(x)]} {x:.2f} 倍"
        )
else:
    stress_display["自有資金年報酬率"] = (
        summary["壓力淨現金流"] / principal
    ).map(lambda x: f"{x*100:.2f}%")
show_df(stress_display)

if is_mortgage_funded:
    st.caption("⚠️ 本金來自房貸：margin call 被斷頭時，房貸債務仍然存在，房子的淨值要承擔缺口。")
else:
    st.caption("本金為自有資金：margin call 的最大損失範圍限於投入的本金，不影響其他資產。")

st.divider()

# ------------------------------------------------------------
# 區塊四：配息率敏感度
# ------------------------------------------------------------

st.subheader("④ 配息率敏感度")

sens_rounds = st.multiselect(
    "選擇要比較的層次", layers_list,
    default=[x for x in (1, 2) if x in layers_list],
    format_func=lambda x: LAYER_NAMES[x],
    key=f"sens_{'-'.join(map(str, layers_list))}",
)

if sens_rounds:
    yields = [i / 100 for i in range(30, 101, 5)]
    chart_data = {}
    for r in sens_rounds:
        s = summary[summary["層次"] == LAYER_NAMES[r]].iloc[0]
        chart_data[LAYER_NAMES[r]] = [
            s["總部位"] * y - s["總借款"] * lombard_rate for y in yields
        ]

    chart_df = pd.DataFrame(chart_data, index=[f"{y*100:.0f}%" for y in yields])
    if has_mortgage:
        for t in terms:
            chart_df[f"{t}年房貸年繳"] = mortgage_annual_payment(mortgage_amount, mortgage_rate, t)

    st.line_chart(chart_df, height=360)
    st.caption(
        "橫軸：混合配息率　縱軸：年金額（萬元）。"
        + ("曲線高於水平線即表示 cover 得掉。" if has_mortgage else "")
    )

st.divider()

# ------------------------------------------------------------
# 區塊五：輸出 PDF
# ------------------------------------------------------------

st.subheader("⑤ 輸出 PDF 摘要")

# 參數指紋：任何一項變動，已產生的 PDF 就視為過期
param_sig = (
    capital_source, principal, mortgage_amount, mortgage_rate,
    w_bond, yield_bond, yield_fund, ltv_bond, ltv_fund,
    fx_discount, draw_ratio, last_draw_ratio,
    lombard_rate, notice_line, call_line, extra_pledge,
    fx_pair, fx_base, fx_now, fx_step, fund_decline, stress_rate,
    tuple(layers_list),
)

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
            capital_source=capital_source, is_mortgage_funded=is_mortgage_funded,
            principal=principal, mortgage_amount=mortgage_amount, mortgage_rate=mortgage_rate,
            w_bond=w_bond, w_fund=w_fund,
            yield_bond=yield_bond, yield_fund=yield_fund,
            ltv_bond=ltv_bond, ltv_fund=ltv_fund,
            fx_discount=fx_discount,
            lombard_rate=lombard_rate, blended_yield=blended_yield,
            credit_rate=credit_rate, draw_ratio=draw_ratio,
            last_draw_ratio=last_draw_ratio, extra_pledge=extra_pledge,
            notice_line=notice_line, call_line=call_line,
            fund_decline=fund_decline, stress_rate=stress_rate,
            fx_shock=fx_shock, fx_pair=fx_pair, fx_base=fx_base,
            fx_now=fx_now, fx_scenarios=fx_scenarios,
        )
        try:
            st.session_state["pdf_bytes"] = build_pdf(params, summary, terms, font_paths)
            st.session_state["pdf_sig"] = param_sig
            st.success("PDF 已產生，可按下方按鈕下載。")
        except Exception as e:  # noqa: BLE001
            st.error(f"PDF 產生失敗：{e}")

    if st.session_state.get("pdf_bytes") and st.session_state.get("pdf_sig") != param_sig:
        st.warning("⚠️ 參數已變更，下方 PDF 仍是舊版。請重新按「產生 PDF 摘要報告」。")

    if st.session_state.get("pdf_bytes"):
        st.download_button(
            "⬇️ 下載 PDF",
            data=st.session_state["pdf_bytes"],
            file_name=f"Lombard槓桿試算_{date.today().isoformat()}.pdf",
            mime="application/pdf",
        )

st.divider()

with st.expander("📌 模型假設與限制"):
    st.markdown(
        ("- **本金來自房貸**，投資部位自有資金為 0，實質風險由房屋淨值承擔\n"
         if is_mortgage_funded else
         "- **本金為自有資金**，無資金成本；margin call 的損失範圍限於投入的本金\n")
        + """- 配息率視為穩定現金流，未計入債券價格波動、提前買回、違約
- 匯率損益未計入配息現金流；margin call 部分已依「評估匯率」重估擔保品價值
- 未計入海外所得最低稅負、信託／保管費、基金手續費
- 房貸為本息均攤、利率固定；Lombard 利率實務上為浮動
- 債券「持有到期還本」僅保原幣本金，不保台幣本金
- 長年期間債券需再投資數輪，存在再投資風險
- **基金質借成數實務上常低於債券**，請先與授信確認後再調整參數
    """)

st.caption("此工具僅供內部試算與教育用途，不構成投資建議。")
