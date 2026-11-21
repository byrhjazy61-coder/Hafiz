import io
import os
import re
import json
import time
import uuid
import random
import datetime as dt

import requests
import streamlit as st
import pdfplumber

BANK_FILE = "bank.json"
TYPES = {
    "mcq": "ضع دائرة",
    "tf": "صح وخطأ",
    "count": "عدد",
    "fill": "املأ الفراغ",
    "explain": "علل",
}
LEVELS = ["سهل", "متوسط", "صعب"]
UNIT_PATTERN = re.compile(r"^\s*(الوحدة|الدرس|الفصل|المبحث)\s+\S+")


def today_str():
    return dt.date.today().isoformat()


def load_bank():
    if "bank" not in st.session_state:
        if os.path.exists(BANK_FILE):
            try:
                with open(BANK_FILE, "r", encoding="utf-8") as f:
                    st.session_state["bank"] = json.load(f)
            except Exception:
                st.session_state["bank"] = {}
        else:
            st.session_state["bank"] = {}
    return st.session_state["bank"]


def save_bank(bank):
    st.session_state["bank"] = bank
    try:
        with open(BANK_FILE, "w", encoding="utf-8") as f:
            json.dump(bank, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def ask_ai(prompt):
    key = st.session_state.get("api_key", "").strip()
    if not key:
        raise ValueError("أدخل مفتاح التشغيل أولًا")

    if key.startswith("sk-ant-"):
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"خطأ في الاتصال: {resp.text[:150]}")
        return resp.json()["content"][0]["text"]

    last_err = ""
    for model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        resp = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        if resp.status_code == 200:
            parts = resp.json()["candidates"][0]["content"].get("parts", [])
            return "\n".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        last_err = resp.text
        if resp.status_code in (400, 403):
            break
    raise RuntimeError(f"تأكد من صحة المفتاح أو انتظر دقيقة: {last_err[:150]}")


def extract_json(text):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("تعذر قراءة الأسئلة المولدة، حاول مرة أخرى")
    return json.loads(text[start:end + 1])


def generate_questions(unit, text, count, level, types):
    type_list = "\n".join(f'- type = "{k}" : {TYPES[k]}' for k in types)
    prompt = f"""أنت معلم خبير لطلاب المرحلة المتوسطة.
اعتمد على النص المدرسي التالي وأنشئ {count} سؤالًا باللغة العربية السليمة.
إذا كانت بعض الكلمات المستخرجة من ملف PDF معكوسة الحروف، رتبها بشكل صحيح قبل كتابة السؤال.
مستوى الصعوبة المطلوب: {level}
الأنواع المسموحة فقط:
{type_list}

قواعد كل نوع:
- mcq: ضع 4 خيارات في options، واجعل answer مطابقًا حرفيًا لأحد الخيارات.
- tf: عبارة للحكم عليها، وanswer هو "صح" أو "خطأ" فقط.
- count: سؤال يطلب تعداد عناصر أو نقاط، وanswer النقاط مفصولة بفاصلة.
- fill: جملة بها فراغ على شكل ....... ، وanswer الكلمة المناسبة للفراغ.
- explain: سؤال يبدأ بـ "علل"، وanswer التعليل المختصر.

أخرج JSON فقط بدون أي كلام إضافي بهذا الشكل:
[
  {{"type": "mcq", "question": "...", "options": ["...", "...", "...", "..."], "answer": "..."}},
  {{"type": "tf", "question": "...", "answer": "صح"}}
]

النص:
{text}
"""
    items = extract_json(ask_ai(prompt))
    questions = []
    for it in items:
        if not isinstance(it, dict) or it.get("type") not in types:
            continue
        qtype = it["type"]
        question = str(it.get("question", "")).strip()
        answer = str(it.get("answer", "")).strip()
        options = [str(o).strip() for o in it.get("options", [])] if qtype == "mcq" else []
        if not question or not answer:
            continue
        if qtype == "mcq" and (len(options) < 2 or answer not in options):
            continue
        if qtype == "tf":
            answer = "صح" if answer.startswith("ص") else "خطأ"
        questions.append({
            "id": uuid.uuid4().hex,
            "unit": unit,
            "type": qtype,
            "question": question,
            "options": options,
            "answer": answer,
            "interval": 1,
            "next_review": today_str(),
            "correct": 0,
            "wrong": 0,
        })
    return questions


def grade_open_answers(items):
    if not items:
        return {}
    prompt = f"""أنت معلم يصحح إجابات طلاب المرحلة المتوسطة.
قارن إجابة الطالب بالإجابة النموذجية وأعطِ درجة (1 = صحيحة، 0.5 = جزئية، 0 = خاطئة) مع تعليق مشجع قصير.
تقبل اختلاف الصياغة إذا كان المعنى صحيحًا.
أخرج JSON فقط:
[{{"id": "...", "score": 1, "feedback": "..."}}]

الإجابات:
{json.dumps(items, ensure_ascii=False)}
"""
    try:
        results = extract_json(ask_ai(prompt))
        return {str(r["id"]): (float(r["score"]), str(r.get("feedback", ""))) for r in results}
    except Exception:
        return {}


def split_into_sections(full_text):
    sections, title, lines = [], None, []
    for line in full_text.split("\n"):
        if UNIT_PATTERN.match(line) and len(line.strip()) < 65:
            if any(l.strip() for l in lines):
                sections.append((title or "مقدمة الكتاب", "\n".join(lines)))
            title, lines = line.strip(), []
        else:
            lines.append(line)
    if any(l.strip() for l in lines):
        sections.append((title or "محتوى الكتاب", "\n".join(lines)))

    if len(sections) <= 1:
        size = 3000
        sections = [
            (f"الجزء {i // size + 1}", full_text[i:i + size])
            for i in range(0, len(full_text), size)
        ]

    result = {}
    for t, body in sections:
        key, n = t, 2
        while key in result:
            key = f"{t} ({n})"
            n += 1
        result[key] = body
    return result


@st.cache_data(show_spinner="جارٍ قراءة ملف الكتاب...")
def load_sections(file_bytes):
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return split_into_sections(text) if text.strip() else {}


def update_srs(q, score):
    if score >= 0.6:
        q["correct"] += 1
        q["interval"] = max(1, q.get("interval", 1) * 2)
    else:
        q["wrong"] += 1
        q["interval"] = 1
    q["next_review"] = (dt.date.today() + dt.timedelta(days=q["interval"])).isoformat()


def build_session(units, types, level, count):
    bank = load_bank()
    today = today_str()

    due = [
        q for q in bank.values()
        if q["unit"] in units and q["type"] in types and q["next_review"] <= today
    ]
    random.shuffle(due)
    review = due[: count // 2]
    needed = count - len(review)

    new_questions = []
    sections = st.session_state.get("sections", {})
    gen_units = [u for u in units if u in sections]
    if needed > 0 and gen_units:
        per, extra = divmod(needed, len(gen_units))
        for i, u in enumerate(gen_units):
            n = per + (1 if i < extra else 0)
            if n == 0:
                continue
            with st.spinner(f"جارٍ إعداد أسئلة ({level}) من: {u}"):
                new_questions.extend(
                    generate_questions(u, sections[u][:5500], n, level, types)
                )

    for q in new_questions:
        bank[q["id"]] = q
    save_bank(bank)

    questions = review + new_questions
    random.shuffle(questions)
    return questions


def upload_page():
    st.subheader("📄 ارفع ملف المادة (PDF)")
    file = st.file_uploader("اختر ملف الكتاب أو الملخص من هاتفك", type=["pdf"])
    if file:
        sections = load_sections(file.getvalue())
        if not sections:
            st.error("هذا الملف عبارة عن صور ممسوحة لا تحتوي نصًا، جرب ملف PDF نصي.")
            return
        st.session_state["sections"] = sections
        all_names = list(sections.keys())
        st.success(f"تم استخراج {len(all_names)} وحدة/درس من الكتاب ✅")

        num = st.number_input(
            "كم وحدة أو درس تريد استخراجها للمراجعة؟",
            min_value=1,
            max_value=len(all_names),
            value=min(2, len(all_names)),
        )
        st.session_state["selected_units"] = all_names[:num]
        st.info("الآن اضغط على تبويب **🎯 الأسئلة** بالأعلى لبدء الاختبار!")

        with st.expander("معاينة الوحدات والدروس المكتشفة"):
            for i, name in enumerate(all_names, 1):
                mark = "✅" if i <= num else "⬜"
                st.write(f"{mark} {i}. {name}")


def start_page():
    bank = load_bank()
    sections = st.session_state.get("sections", {})
    all_units = list(dict.fromkeys(list(sections.keys()) + [q["unit"] for q in bank.values()]))
    if not all_units:
        st.warning("ارفع ملف PDF من تبويب **📄 الكتاب** أولًا.")
        return

    default_units = [u for u in st.session_state.get("selected_units", all_units[:1]) if u in all_units]
    units = st.multiselect("الوحدات أو الدروس المختارة:", all_units, default=default_units or all_units[:1])

    st.markdown("### ⚙️ إعدادات الأسئلة قبل البدء")
    level = st.radio("اختر مستوى الصعوبة:", LEVELS, index=1, horizontal=True)
    types = st.multiselect(
        "اختر أنواع الأسئلة:",
        list(TYPES.keys()),
        default=list(TYPES.keys()),
        format_func=lambda k: TYPES[k],
    )
    count = st.slider("عدد الأسئلة:", 3, 25, 6)
    mode = st.radio("نظام المراجعة:", ["تدريب مفتوح", "محاكاة اختبار بوقت"], horizontal=True)
    minutes = None
    if mode == "محاكاة اختبار بوقت":
        minutes = st.number_input("مدة الاختبار (بالدقائق):", 1, 90, 10)

    if st.button("🚀 ابدأ الأسئلة الآن", type="primary", use_container_width=True):
        if not st.session_state.get("api_key"):
            st.error("ضع مفتاح التشغيل المجاني في المربع أعلى الصفحة أولًا.")
        elif not units or not types:
            st.error("اختر وحدة واحدة ونوع سؤال واحد على الأقل.")
        else:
            for k in list(st.session_state.keys()):
                if k.startswith("ans_"):
                    del st.session_state[k]
            try:
                questions = build_session(units, types, level, count)
                if not questions:
                    st.error("لم تتولد أسئلة، حاول مرة أخرى.")
                else:
                    st.session_state["quiz"] = {
                        "questions": questions,
                        "start": time.time(),
                        "minutes": minutes,
                        "level": level,
                        "done": False,
                        "results": [],
                    }
                    st.rerun()
            except Exception as e:
                st.error(str(e))


def quiz_page(quiz):
    st.caption(f"مستوى الصعوبة: **{quiz['level']}** | عدد الأسئلة: **{len(quiz['questions'])}**")
    expired = False
    if quiz["minutes"]:
        remaining = quiz["minutes"] * 60 - (time.time() - quiz["start"])
        if remaining <= 0:
            expired = True
            st.warning("⏰ انتهى وقت الاختبار! تم التصحيح تلقائيًا.")
        else:
            m, s = divmod(int(remaining), 60)
            st.info(f"⏳ الوقت المتبقي: {m:02d}:{s:02d}")

    with st.form("quiz_form"):
        for i, q in enumerate(quiz["questions"]):
            st.markdown(f"**س{i + 1} ({TYPES[q['type']]}):** {q['question']}")
            if q["type"] == "mcq":
                st.radio("الإجابة", q["options"], index=None, key=f"ans_{i}", label_visibility="collapsed")
            elif q["type"] == "tf":
                st.radio("الإجابة", ["صح", "خطأ"], index=None, horizontal=True, key=f"ans_{i}", label_visibility="collapsed")
            else:
                st.text_area("اكتب إجابتك هنا...", key=f"ans_{i}", label_visibility="collapsed", height=80)
            st.divider()
        submitted = st.form_submit_button("✅ إنهاء وتصحيح الإجابات", type="primary", use_container_width=True)

    if submitted or expired:
        finish_quiz(quiz)
        st.rerun()


def finish_quiz(quiz):
    bank = load_bank()
    answers = [(st.session_state.get(f"ans_{i}") or "").strip() for i in range(len(quiz["questions"]))]
    scores, feedback, to_grade = {}, {}, []

    for i, q in enumerate(quiz["questions"]):
        ans = answers[i]
        if q["type"] in ("mcq", "tf"):
            scores[i] = 1.0 if ans == q["answer"] else 0.0
            feedback[i] = "إجابة صحيحة وممتازة ✅" if scores[i] else "إجابة خاطئة، راجع الإجابة النموذجية ❌"
        elif not ans:
            scores[i], feedback[i] = 0.0, "لم تكتب إجابة"
        else:
            to_grade.append({
                "id": str(i),
                "question": q["question"],
                "model_answer": q["answer"],
                "student_answer": ans,
            })

    if to_grade:
        with st.spinner("جارٍ التصحيح الذكي للإجابات المقالية..."):
            graded = grade_open_answers(to_grade)
        for item in to_grade:
            i = int(item["id"])
            scores[i], feedback[i] = graded.get(item["id"], (0.0, "راجع الإجابة النموذجية للمقارنة"))

    results = []
    for i, q in enumerate(quiz["questions"]):
        if q["id"] in bank:
            update_srs(bank[q["id"]], scores[i])
        results.append({"q": q, "student": answers[i], "score": scores[i], "feedback": feedback[i]})

    save_bank(bank)
    quiz["results"] = results
    quiz["done"] = True


def show_results(quiz):
    results = quiz["results"]
    total = sum(r["score"] for r in results)
    pct = (total / len(results) * 100) if results else 0
    st.balloons()
    st.success(f"🏆 نتيجتك النهائية: {total:.1f} من {len(results)} ({pct:.0f}%)")

    for i, r in enumerate(results, 1):
        q = r["q"]
        icon = "✅" if r["score"] >= 0.8 else ("🟡" if r["score"] > 0 else "❌")
        with st.expander(f"{icon} س{i}: {q['question']}"):
            st.write(f"**إجابتك:** {r['student'] or 'بدون إجابة'}")
            st.write(f"**الإجابة الصحيحة:** {q['answer']}")
            st.write(f"**التقييم:** {r['feedback']}")

    if st.button("🔄 اختبار جديد", type="primary", use_container_width=True):
        del st.session_state["quiz"]
        st.rerun()


def progress_page():
    bank = load_bank()
    if not bank:
        st.info("لم تجرِ أي اختبار بعد. ستظهر إحصائياتك وتقدمك هنا تلقائيًا!")
        return

    units = {}
    for q in bank.values():
        u = units.setdefault(q["unit"], {
            "الوحدة / الدرس": q["unit"],
            "الأسئلة": 0,
            "صحيحة": 0,
            "خاطئة": 0,
            "للمراجعة اليوم": 0,
        })
        u["الأسئلة"] += 1
        u["صحيحة"] += q["correct"]
        u["خاطئة"] += q["wrong"]
        if q["next_review"] <= today_str():
            u["للمراجعة اليوم"] += 1

    rows = []
    for u in units.values():
        att = u["صحيحة"] + u["خاطئة"]
        u["الإتقان"] = f"{u['صحيحة'] / att * 100:.0f}%" if att else "—"
        rows.append(u)
    st.table(rows)

    if st.button("🗑️ مسح سجل التقدم"):
        save_bank({})
        st.rerun()


def main():
    st.set_page_config(page_title="حافظ", page_icon="📚", layout="centered")
    st.markdown("<style>.stApp {direction: rtl;}</style>", unsafe_allow_html=True)
    st.title("📚 تطبيق حافظ")
    st.caption("مساعدك الذكي لمراجعة المواد الحفظية بالذكاء الاصطناعي")

    saved_key = st.session_state.get("api_key", "")
    with st.expander("🔑 إعداد مفتاح التشغيل المجاني", expanded=not bool(saved_key)):
        key_input = st.text_input(
            "الصق مفتاح Google AI المجاني هنا:",
            value=saved_key,
            type="password",
        )
        st.session_state["api_key"] = key_input.strip()
        if st.session_state["api_key"]:
            st.success("تم تفعيل المفتاح بنجاح ✅")

    tab1, tab2, tab3 = st.tabs(["📄 الكتاب", "🎯 الأسئلة", "📊 التقدم"])
    with tab1:
        upload_page()
    with tab2:
        quiz = st.session_state.get("quiz")
        if quiz and not quiz["done"]:
            quiz_page(quiz)
        elif quiz:
            show_results(quiz)
        else:
            start_page()
    with tab3:
        progress_page()


main()
