import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import streamlit as st
from src import knowledge_base, memory_engine, storage, theory

storage.init_db()
st.title("📖 知识库与学习")
st.caption("浏览高中生物知识点，学习后加入个性化复习计划（艾宾浩斯曲线）")

search = st.text_input("🔍 搜索知识点", "")
chapters = knowledge_base.get_chapters()
points = knowledge_base.search_points(search)

st.markdown(f"共 **{len(points)}** 个知识点")
st.markdown("---")

for kp in points:
    ctype = theory.CONTENT_TYPES.get(kp["content_type"], {})
    header = (f"**{kp['title']}**　"
              f"〔{knowledge_base.get_chapter_name(kp['chapter'])} · "
              f"难度{'★'*kp['difficulty']} · {ctype.get('name','')}〕")
    with st.expander(header):
        st.markdown(kp["details"])
        st.markdown("**核心要点**")
        for pt in kp["key_points"]:
            st.markdown(f"- {pt}")
        st.markdown(f"**内容类型**：{ctype.get('name','')} — {ctype.get('desc','')}")
        record = storage.get_review_record(kp["id"])
        if record:
            st.success(f"✅ 已加入复习计划 · 掌握度 {record['mastery']*100:.0f}% · 已复习 {record['review_count']} 次 · 第{record['stage']+1}轮")
        else:
            if st.button("开始学习并加入复习计划", key=f"learn_{kp['id']}"):
                memory_engine.add_knowledge_point(kp["id"])
                st.success(f"已将「{kp['title']}」加入复习计划，明天将提醒你第一次复习！")
                st.rerun()