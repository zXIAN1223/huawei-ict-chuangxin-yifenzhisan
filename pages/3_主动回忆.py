import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import streamlit as st
from src import active_recall, memory_engine, knowledge_base, storage

storage.init_db()
st.title("❓ 主动回忆")
st.caption("AI主动提问，引导你从记忆中提取信息——检索练习比反复阅读高效得多")

profile = storage.get_profile()
style = profile.get("learning_style", "") if profile else ""

mode = st.radio("选择模式", ["单知识点回忆", "交错学习(混合多章节)"], horizontal=True)

if "ar_question" not in st.session_state:
    st.session_state.ar_question = None
if "ar_result" not in st.session_state:
    st.session_state.ar_result = None

if mode == "单知识点回忆":
    learned = storage.get_all_review_records()
    if learned:
        options = {r["kp_id"]: knowledge_base.get_knowledge_point(r["kp_id"])["title"] for r in learned}
        kp_id = st.selectbox("选择知识点", list(options.keys()), format_func=lambda x: options[x])
    else:
        st.info("还没有已学知识点，请先到「知识库与学习」页面学习。")
        kp_id = None
    if kp_id and st.button("出一道题", key="gen_single"):
        st.session_state.ar_question = active_recall.generate_question(kp_id, style)
        st.session_state.ar_result = None
        st.rerun()
else:
    st.markdown("交错学习会从**不同章节混合出题**，促进知识迁移与区分能力。")
    if st.button("生成交错题目集(5道)", key="gen_interleave"):
        qs = active_recall.get_interleaved_questions(5, style)
        st.session_state.ar_questions = qs
        st.session_state.ar_qindex = 0
        st.session_state.ar_question = qs[0] if qs else None
        st.session_state.ar_result = None
        st.rerun()
    if "ar_questions" in st.session_state and st.session_state.get("ar_questions"):
        idx = st.session_state.ar_qindex
        if idx < len(st.session_state.ar_questions):
            st.session_state.ar_question = st.session_state.ar_questions[idx]
            st.caption(f"第 {idx+1}/{len(st.session_state.ar_questions)} 题")

q = st.session_state.get("ar_question")
if q:
    st.markdown("---")
    st.markdown(f"**【{q['title']}】**　题型：{q['type']}")
    st.markdown(f"### {q['question']}")
    answer = st.text_area("你的回答（尽量凭回忆，不要翻书）", height=120, key="ar_answer")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("提交回答", type="primary"):
            result = active_recall.evaluate_answer(q, answer)
            active_recall.save_answer(q["kp_id"], q["question"], answer, result["score"], result["quality"])
            memory_engine.record_review(q["kp_id"], result["quality"])
            st.session_state.ar_result = result
            st.rerun()
    with col2:
        if st.button("查看提示"):
            st.info(q["hint"])

    r = st.session_state.get("ar_result")
    if r:
        if r["score"] >= 0.8:
            st.success(f"得分 {r['score']*100:.0f}%　{r['feedback']}")
        elif r["score"] >= 0.5:
            st.warning(f"得分 {r['score']*100:.0f}%　{r['feedback']}")
        else:
            st.error(f"得分 {r['score']*100:.0f}%　{r['feedback']}")
        if mode == "交错学习(混合多章节)" and "ar_qindex" in st.session_state:
            if st.button("下一题"):
                st.session_state.ar_qindex += 1
                st.session_state.ar_result = None
                nxt = st.session_state.ar_questions
                if st.session_state.ar_qindex < len(nxt):
                    st.session_state.ar_question = nxt[st.session_state.ar_qindex]
                else:
                    st.session_state.ar_question = None
                    st.balloons()
                st.rerun()