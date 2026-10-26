import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from src import storage, user_model, memory_engine, theory, knowledge_base
from src.learning_test import VARK_DESCRIPTIONS

st.set_page_config(page_title="AI辅助学习助手", page_icon="📚", layout="wide")
storage.init_db()

with st.sidebar:
    st.title("📚 AI辅助学习助手")
    st.caption("基于科学学习理论的一对一主动学习")
    st.markdown("---")
    st.markdown("**当前科目**")
    st.info(knowledge_base.get_subject())
    profile = storage.get_profile()
    style_name = VARK_DESCRIPTIONS[profile["learning_style"]]["name"] if profile and profile.get("learning_style") else "未测试"
    st.markdown(f"**学习风格**：{style_name}")
    st.markdown("---")
    st.markdown("**理论支撑**")
    for t in theory.LEARNING_THEORIES.values():
        st.markdown(f"• {t['name']}")

st.title("📊 学习仪表盘")
dashboard = user_model.get_user_dashboard()

col1, col2, col3, col4 = st.columns(4)
col1.metric("已学知识点", dashboard["total_learned"])
col2.metric("平均掌握度", f"{dashboard['avg_mastery']*100:.0f}%")
col3.metric("平均保持率", f"{dashboard['avg_retention']*100:.0f}%")
col4.metric("累计答题", dashboard["total_answers"])

st.markdown("---")
st.subheader("🎯 今日学习建议")
rec = memory_engine.get_study_recommendation()
st.info(rec["note"])
if rec["planned"]:
    for item in rec["planned"]:
        kp = item["kp"]
        st.markdown(f"- **{kp['title']}**（{knowledge_base.get_chapter_name(kp['chapter'])}）— 当前掌握度 {item['record']['mastery']*100:.0f}%")
else:
    st.write("暂无待复习内容。去「知识库与学习」页面添加新知识点吧！")

st.markdown("---")
st.subheader("🧭 策略优化建议")
for r in user_model.get_strategy_recommendations():
    st.markdown(f"• {r}")

st.markdown("---")
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("🔺 学习金字塔")
    df = pd.DataFrame(theory.LEARNING_PYRAMID)
    fig = go.Figure(go.Bar(
        x=df["retention"], y=df["level"], orientation="h",
        marker_color=["#ff6b6b" if t == "被动" else "#51cf66" for t in df["type"]],
        text=df["retention"], textposition="outside", texttemplate="%{text}%",
    ))
    fig.update_layout(height=360, margin=dict(l=10, r=40, t=10, b=10),
                      yaxis=dict(autorange="reversed"), showlegend=False)
    st.plotly_chart(fig, use_container_width=True)
    st.caption("绿色=主动学习，红色=被动学习。教授他人保留率高达90%！")

with col_right:
    st.subheader("📉 艾宾浩斯遗忘曲线")
    days = list(range(0, 31))
    fig2 = go.Figure()
    for info in theory.CONTENT_TYPES.values():
        rates = [theory.retention_rate(d, info["stability"]) * 100 for d in days]
        fig2.add_trace(go.Scatter(x=days, y=rates, name=info["name"], mode="lines"))
    fig2.add_trace(go.Scatter(
        x=theory.EBBINGHAUS_INTERVALS,
        y=[theory.retention_rate(d) * 100 for d in theory.EBBINGHAUS_INTERVALS],
        mode="markers", name="推荐复习点", marker=dict(size=11, symbol="star", color="gold"),
    ))
    fig2.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10),
                       xaxis_title="天数", yaxis_title="保持率%")
    st.plotly_chart(fig2, use_container_width=True)
    st.caption("不同类型知识遗忘速度不同，星标为推荐复习时间点。")