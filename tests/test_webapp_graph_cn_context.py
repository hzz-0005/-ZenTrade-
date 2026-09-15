from webapp.engine.context_builder import DailyContext
from webapp.engine.graph_agent import _graph_context


def test_full_graph_context_includes_cn_market_policy_and_skills():
    ctx = DailyContext(
        symbol="300308.SZ",
        market="cn",
        sim_date="2025-03-31",
        ohlcv_tail_csv="Date,Open,High,Low,Close,Volume\n2025-03-31,9,10,9,10,1000",
        indicators={},
        news=[],
        fundamentals={
            "摊薄每股收益(元)": 0.05,
            "上年同期摊薄每股收益(元)": -0.10,
            "扣除非经常性损益后的每股收益(元)": -0.02,
        },
        skills=[{"category": "entry", "statement": "企稳后分批建仓"}],
    )

    result = _graph_context(ctx)

    assert "A股市场适配规则" in result
    assert "高PE只能降低置信度或目标仓位，不能单独否决买入" in result
    assert "[entry] 企稳后分批建仓" in result

