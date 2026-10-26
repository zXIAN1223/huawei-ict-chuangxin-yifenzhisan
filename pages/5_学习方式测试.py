import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import streamlit as st
from src import learning_test, user_model, storage

storage.init_db()
st.title("🧪 学习方式测试")
st.caption("通过VARK模型测试你的学习风格，AI据此定制最适合你的学习策略（元认知）")

current = learning_test.get_current_style()
if current:
    st.success(f"你已完成测试，主要学习风格：**{current['profile']['name']}**")
    with st.expander("查看上次结果"):
        st.markdown(current["profile"]["desc"])
        st.markdown("**推荐学习方式**")
        for tip in current["profile"]["tips"]:
            st.markdown(f"- {tip}")
    if st.button("重新测试"):
        st.session_state.clear()
        st.rerun()
    st.markdown("---")

questions = learning_test.get_test()
if "test_answers" not in st.session_state:
    st.session_state.test_answers = [None] * len(questions)

st.subheader("请根据真实偏好作答")
for i, q in enumerate(questions):
    st.markdown(f"**{i+1}. {q['q']}**")
    options = q["options"]
    keys = list(options.keys())
    labels = [f"{k}：{options[k]}" for k in keys]
    label_to_key = {f"{k}：{options[k]}": k for k in keys}
    selected_label = st.radio(f"第{i+1}题", labels, key=f"q_{i}",
                              horizontal=True, label_visibility="collapsed")
    st.session_state.test_answers[i] = label_to_key[selected_label]

st.markdown("---")
if st.button("提交测试", type="primary"):
    answers = st.session_state.test_answers
    result = learning_test.submit_test(answers)
    st.balloons()
    st.markdown(f"## 你的学习风格：{result['profile']['name']}")
    st.markdown(result["profile"]["desc"])
    st.markdown("**推荐学习方式**")
    for tip in result["profile"]["tips"]:
        st.markdown(f"- {tip}")
    st.markdown("**风格分布**")
    for code, score in result["ranking"]:
        name = learning_test.VARK_DESCRIPTIONS[code]["name"]
        bar = "█" * score
        st.markdown(f"- {name}：{bar} {score}题")
    st.markdown("---")
    st.subheader("🧭 为你定制的学习策略")
    for r in user_model.get_strategy_recommendations():
        st.markdown(f"- {r}")