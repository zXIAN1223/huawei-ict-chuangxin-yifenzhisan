import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import streamlit as st
from src import chat_engine, knowledge_base, storage

storage.init_db()
st.title("💬 讨论与教授")
st.caption("学习金字塔：讨论(50%)、教授他人(90%)远比被动阅读(10%)高效")

mode_keys = list(chat_engine.MODES.keys())
mode_labels = [chat_engine.MODES[m]["name"] for m in mode_keys]
selected = st.radio("选择模式", mode_labels, horizontal=True)
mode = mode_keys[mode_labels.index(selected)]
st.caption(chat_engine.MODES[mode]["desc"])

kp_id = None
if mode == "teach":
    st.markdown("**选择要教授的知识点**")
    all_kps = knowledge_base.get_all_knowledge_points()
    options = {kp["id"]: f"{kp['title']}（{knowledge_base.get_chapter_name(kp['chapter'])}）" for kp in all_kps}
    kp_id = st.selectbox("知识点", list(options.keys()), format_func=lambda x: options[x])
    kp = knowledge_base.get_knowledge_point(kp_id)
    st.info(f"请用最通俗的语言把「{kp['title']}」讲给AI听，AI会扮演初学者向你提问（费曼学习法）。")

st.markdown("---")
history = chat_engine.get_history(mode, kp_id)
if not history:
    if mode == "teach":
        st.chat_message("assistant").markdown(f"我是生物初学者，想学「{knowledge_base.get_knowledge_point(kp_id)['title']}」。请你用最简单的话讲给我听吧！")
    elif mode == "discuss":
        st.chat_message("assistant").markdown("你好！我们可以深入讨论任何高中生物问题。我会追问引导你深度思考。请问你想聊哪个知识点？")
    else:
        st.chat_message("assistant").markdown("你好！问我任何高中生物知识点，我会为你详细讲解。")

for msg in history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input("输入消息..."):
    chat_engine.respond(prompt, mode, kp_id)
    st.rerun()