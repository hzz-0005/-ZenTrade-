import pandas as pd

from webapp.engine.context_builder import DailyContext
from webapp.engine.data_gateway import DataGateway, detect_market
from webapp.engine.market_profile import enrich_fundamentals, market_guidance
from webapp.engine.vendors.cn_source import _snapshot_from_indicator_frame


def test_cn_board_and_stable_profit_use_relative_pe_only():
    result = enrich_fundamentals(
        "600519.SS",
        "cn",
        {
            "摊薄每股收益(元)": 1.20,
            "上年同期摊薄每股收益(元)": 0.90,
            "扣除非经常性损益后的每股收益(元)": 1.10,
            "上年同期扣非每股收益(元)": 0.82,
            "每股经营性现金流(元)": 1.30,
            "市盈率PE(年化估算)": 45.0,
        },
    )

    assert result["A股板块"] == "沪深主板（常规10%涨跌幅）"
    assert result["盈利阶段"] == "稳定盈利"
    assert result["PE适用规则"] == "仅作行业/公司自身历史的相对比较；禁止套用海外固定倍数阈值"


def test_accounting_turnaround_with_negative_adjusted_eps_disables_pe():
    result = enrich_fundamentals(
        "300308.SZ",
        "cn",
        {
            "摊薄每股收益(元)": 0.05,
            "上年同期摊薄每股收益(元)": -0.10,
            "扣除非经常性损益后的每股收益(元)": -0.02,
            "上年同期扣非每股收益(元)": -0.12,
            "每股经营性现金流(元)": -0.03,
            "市盈率PE(年化估算)": 180.0,
        },
    )

    assert result["A股板块"] == "创业板（常规20%涨跌幅）"
    assert result["盈利阶段"] == "账面扭亏（扣非未转正，质量待验证）"
    assert result["PE适用规则"] == "禁用绝对PE：微利分母会放大倍数，改看扣非、现金流、营收与利润率"


def test_adjusted_turnaround_is_catalyst_not_stable_profit():
    result = enrich_fundamentals(
        "688001.SS",
        "cn",
        {
            "摊薄每股收益(元)": 0.10,
            "上年同期摊薄每股收益(元)": -0.08,
            "扣除非经常性损益后的每股收益(元)": 0.07,
            "上年同期扣非每股收益(元)": -0.06,
            "每股经营性现金流(元)": 0.12,
            "市盈率PE(年化估算)": 120.0,
        },
    )

    assert result["A股板块"] == "科创板（常规20%涨跌幅）"
    assert result["盈利阶段"] == "经营性扭亏（催化剂，仍需后续报告期确认）"
    assert result["PE适用规则"].startswith("禁用绝对PE")


def test_loss_marks_pe_not_applicable():
    result = enrich_fundamentals(
        "830001.BJ",
        "cn",
        {
            "摊薄每股收益(元)": -0.20,
            "上年同期摊薄每股收益(元)": -0.35,
        },
    )

    assert result["A股板块"] == "北交所（常规30%涨跌幅）"
    assert result["盈利阶段"] == "仍在亏损（PE不适用）"
    assert result["PE适用规则"].startswith("禁用PE")


def test_positive_eps_without_prior_or_adjusted_data_is_not_called_stable():
    result = enrich_fundamentals(
        "600001.SS",
        "cn",
        {"摊薄每股收益(元)": 0.08, "市盈率PE(年化估算)": 125.0},
    )

    assert result["盈利阶段"] == "账面盈利（缺少同比或扣非数据）"
    assert result["PE适用规则"].startswith("PE仅作弱参考")


def test_non_cn_fundamentals_are_unchanged():
    original = {"每股收益": 2.0, "市盈率PE(年化估算)": 25.0}

    assert enrich_fundamentals("AAPL", "us", original) == original
    assert market_guidance("AAPL", "us", original) == ""


def test_beijing_exchange_symbol_is_detected_as_cn():
    assert detect_market("830001.BJ") == "cn"


def test_snapshot_uses_latest_disclosed_period_and_same_period_last_year():
    frame = pd.DataFrame(
        [
            {
                "日期": "2024-03-31",
                "摊薄每股收益(元)": -0.10,
                "扣除非经常性损益后的每股收益(元)": -0.12,
                "每股经营性现金流(元)": -0.08,
                "销售毛利率(%)": 20.0,
                "销售净利率(%)": -3.0,
                "主营业务收入增长率(%)": 5.0,
                "净利润增长率(%)": -20.0,
                "净资产收益率(%)": -1.0,
                "资产负债率(%)": 40.0,
            },
            {
                "日期": "2024-06-30",
                "摊薄每股收益(元)": 9.99,
                "扣除非经常性损益后的每股收益(元)": 9.99,
            },
            {
                "日期": "2025-03-31",
                "摊薄每股收益(元)": 0.05,
                "扣除非经常性损益后的每股收益(元)": -0.02,
                "每股经营性现金流(元)": -0.03,
                "销售毛利率(%)": 24.0,
                "销售净利率(%)": 1.0,
                "主营业务收入增长率(%)": 18.0,
                "净利润增长率(%)": 105.0,
                "净资产收益率(%)": 0.5,
                "资产负债率(%)": 38.0,
            },
            {
                "日期": "2025-06-30",
                "摊薄每股收益(元)": 8.88,
                "扣除非经常性损益后的每股收益(元)": 8.88,
            },
        ]
    )

    result = _snapshot_from_indicator_frame(frame, "2025-04-30")

    assert result["报告期"] == "2025-03-31"
    assert result["摊薄每股收益(元)"] == 0.05
    assert result["上年同期摊薄每股收益(元)"] == -0.10
    assert result["扣除非经常性损益后的每股收益(元)"] == -0.02
    assert result["上年同期扣非每股收益(元)"] == -0.12
    assert result["每股经营性现金流(元)"] == -0.03
    assert result["销售毛利率(%)"] == 24.0
    assert result["销售净利率(%)"] == 1.0


def test_gateway_adds_adjusted_pe_and_market_profile(monkeypatch):
    gateway = DataGateway("300308.SZ", "cn")
    monkeypatch.setattr("webapp.engine.data_gateway.get_sim_date", lambda: "2025-03-31")
    gateway._price_frame = pd.DataFrame(
        [{"Date": pd.Timestamp("2025-03-31"), "Open": 9, "High": 11, "Low": 9,
          "Close": 10.0, "Volume": 1000}]
    )
    monkeypatch.setattr(
        "webapp.engine.data_gateway.cn_source.fetch_financial_snapshot",
        lambda *_: {
            "报告期": "2025-03-31",
            "摊薄每股收益(元)": 0.05,
            "上年同期摊薄每股收益(元)": -0.10,
            "扣除非经常性损益后的每股收益(元)": -0.02,
            "上年同期扣非每股收益(元)": -0.12,
            "每股经营性现金流(元)": -0.03,
        },
    )

    result, flags = gateway.fundamentals("2025-03-31")

    assert flags == []
    assert result["市盈率PE(年化估算)"] == 50.0
    assert "扣非市盈率PE(年化估算)" not in result
    assert result["盈利阶段"] == "账面扭亏（扣非未转正，质量待验证）"


def test_daily_context_renders_cn_guidance_without_claiming_fake_percentile():
    ctx = DailyContext(
        symbol="300308.SZ",
        market="cn",
        sim_date="2025-03-31",
        ohlcv_tail_csv=(
            "Date,Open,High,Low,Close,Volume\n"
            "2025-03-28,9,10,9,9.5,1000\n"
            "2025-03-31,9.5,10.5,9.4,10,1200"
        ),
        indicators={"rsi": 48},
        news=[],
        fundamentals={
            "摊薄每股收益(元)": 0.05,
            "上年同期摊薄每股收益(元)": -0.10,
            "扣除非经常性损益后的每股收益(元)": -0.02,
            "市盈率PE(年化估算)": 180.0,
        },
        portfolio={"cash": 100_000, "shares": 0, "equity": 100_000},
    )

    prompt = ctx.to_user_message()

    assert "## A股市场适配规则" in prompt
    assert "高PE只能降低置信度或目标仓位，不能单独否决买入" in prompt
    assert "一个可观察的企稳信号即可建立试探仓" in prompt
    assert "近五年偏低分位" not in prompt
