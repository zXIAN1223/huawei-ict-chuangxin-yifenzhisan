import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import streamlit as st
import pandas as pd
from src import memory_engine, knowledge_base, storage

storage.init_db()
st.title("🔔 复习提醒")
st.caption("基于艾宾浩斯遗忘曲线与SM-2算法，结合你的时间分配主动提醒复习")

profile = storage.get_profile()
st.subheader("⚙️ 复习设置")
col1, col2, col3 = st.columns([1, 1, 1])
with col1:
    limit = st.number_input("每日复习上限", 1, 20, profile["daily_review_limit"])
with col2:
    minutes = st.number_input("每日可用时间(分钟)", 5, 240, profile["daily_minutes"])
with col3:
    if st.button("保存设置"):
        storage.update_profile(daily_review_limit=int(limit), daily_minutes=int(minutes))
        st.success("已保存")
        st.rerun()

st.markdown("---")
st.subheader("📌 今日待复习")
recommendation = memory_engine.get_study_recommendation()
st.info(recommendation["note"])
today = recommendation["planned"]
if today:
    for item in today:
        kp = item["kp"]
        rec = item["record"]
        with st.expander(f"**{kp['title']}**　掌握度 {rec['mastery']*100:.0f}% · 第{rec['stage']+1}轮复习"):
            st.markdown(kp["summary"])
            st.markdown("**核心要点**")
            for pt in kp["key_points"]:
                st.markdown(f"- {pt}")
            q = st.slider("复习后自评掌握程度", 0, 5, 3, key=f"q_{kp['id']}",
                          help="0=完全不会, 3=基本掌握, 5=非常熟练")
            if st.button("完成复习", key=f"done_{kp['id']}"):
                result = memory_engine.record_review(kp["id"], q)
                st.success(f"已记录！下次复习：{result['next_interval']}天后 · 掌握度更新为 {result['mastery']*100:.0f}%")
                st.rerun()
else:
    st.info("今日没有待复习内容，继续保持！去「主动回忆」做几道题巩固一下吧。")

st.markdown("---")
st.subheader("📅 未来7天复习提醒")
reminders = memory_engine.get_upcoming_reminders(7)
if reminders:
    df = pd.DataFrame([{
        "知识点": r["title"],
        "章节": knowledge_base.get_chapter_name(r["chapter"]),
        "复习日期": r["date"].strftime("%m-%d %H:%M"),
        "距今(天)": r["days_away"],
        "轮次": f"第{r['stage']+1}轮",
    } for r in reminders])
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.write("未来7天暂无复习计划。多学几个知识点就会有提醒了！")