import html
import io
import json
import os
import random
import re
import time
import uuid
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pdfplumber
import requests
import streamlit as st

APP_NAME = "حِلْ"
BANK_FILE = "bank.json"
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
MAX_CHARS_PER_UNIT = 7000

TYPES = {
    "mcq": "ضع دائرة",
    "tf": "صح وخطأ",
    "count": "عدد",
    "fill": "املأ الفراغ",
    "explain": "علل",
}
TYPE_ICONS = {"mcq": "🔘", "tf": "⚖️", "count": "🔢", "fill": "✏️", "explain": "💡"}
TYPE_COLORS = {"mcq": "#6366f1", "tf": "#0ea5e9", "count": "#10b981", "fill": "#f59e0b", "explain": "#ec4899"}
LEVELS = ["سهل", "متوسط", "صعب"]
LEVEL_DESC = {
    "سهل": "أسئلة مباشرة عن المعلومات الأساسية.",
    "متوسط": "أسئلة تتطلب فهم العلاقات بين الأفكار.",
    "صعب": "أسئلة تحليلية تربط بين عدة معلومات.",
}
UNIT_PATTERN = re.compile(r"^\s*(الوحدة|الدرس|الفصل|المبحث)\s+\S+")


def esc(s):
    return html.escape(str(s))


def today_str():
    return dt.date.today().isoformat()


# ================== التخزين ==================
def load_bank():
    if "bank" not in st.session_state:
        data = {}
        if os.path.exists(BANK_FILE):
            try:
                with open(BANK_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        st.session_state["bank"] = data
    return st.session_state["bank"]


def save_bank(bank):
    st.session_state["bank"] = bank
    try:
        with open(BANK_FILE, "w", encoding="utf-8") as f:
            json.dump(bank, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ================== الذكاء الاصطناعي ==================
def ask_gemini(prompt, key, json_mode=True):
    if not key:
        raise RuntimeError("أدخل مفتاح التشغيل أولًا من القسم العلوي.")
    last_err = "لا يوجد رد"
    for model in GEMINI_MODELS:
        gen = {"temperature": 0.6, "maxOutputTokens": 8192}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        if model.startswith("gemini-2.5"):
            gen["thinkingConfig"] = {"thinkingBudget": 0}  # أسرع بكثير
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            resp = requests.post(url, params={"key": key}, json=body, timeout=90)
        except requests.RequestException as e:
            last_err = str(e)
            continue
        if resp.status_code == 200:
            try:
                parts = resp.json()["candidates"][0]["content"]["parts"]
            except (KeyError, IndexError, TypeError):
                last_err = "رد فارغ من النموذج"
                continue
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if text.strip():
                return text
            last_err = "رد فارغ"
            continue
        last_err = f"{resp.status_code}: {resp.text[:200]}"
        if resp.status_code in (401, 403) or "API key" in resp.text:
            break
    raise RuntimeError(f"تعذر الاتصال بالذكاء الاصطناعي. {last_err}")


def parse_json(text):
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (cleaned.find("["), cleaned.find("{")) if i != -1]
    ends = [i for i in (cleaned.rfind("]"), cleaned.rfind("}")) if i != -1]
    if not starts or not ends:
        raise ValueError("رد الذكاء الاصطناعي ليس بصيغة صالحة")
    return json.loads(cleaned[min(starts): max(ends) + 1])


def as_text(v):
    if isinstance(v, list):
        return "، ".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


def build_prompt(text, level, plan):
    counts = "\n".join(f'- {n} سؤال من نوع "{t}" ({TYPES[t]})' for t, n in plan.items() if n)
    return f"""أنت معلم خبير في إعداد أسئلة المراجعة لطلاب المرحلة المتوسطة.
اعتمد فقط على النص المدرسي التالي.
مستوى الصعوبة: {level} ({LEVEL_DESC[level]})

أنشئ بالضبط عدد الأسئلة التالي:
{counts}

قواعد كل نوع:
- mcq: أربعة خيارات في "options"، و"correct_index" رقم الخيار الصحيح من 0 إلى 3.
- tf: عبارة تُحكم عليها بصح أو خطأ، و"answer" هو "صح" أو "خطأ".
- count: سؤال يطلب ذكر عدد من العناصر أو الأنواع أو الخطوات، و"answer" هو العناصر مفصولة بفاصلة.
- fill: جملة فيها فراغ واحد على الأقل بالشكل ........ ، و"answer" هو الكلمة أو العبارة المناسبة.
- explain: سؤال يبدأ بكلمة "علل"، و"answer" هو التعليل في جملة أو جملتين.

تعليمات عامة:
- اكتب بالعربية الفصحى الواضحة، ولا تكرر السؤال نفسه.
- إذا كانت بعض الكلمات معكوسة بسبب استخراج PDF فأصلحها في صياغة السؤال.
- تجاهل الصور والجداول التي لا يظهر نصها.

أخرج JSON فقط، وهو مصفوفة بهذا الشكل:
[
 {{"type": "mcq", "question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0}},
 {{"type": "tf", "question": "...", "answer": "صح"}},
 {{"type": "fill", "question": "...", "answer": "..."}}
]

النص:
{text}
"""


def normalize_item(it, unit, level, allowed):
    if not isinstance(it, dict):
        return None
    qtype = str(it.get("type", "")).strip()
    question = as_text(it.get("question", ""))
    if qtype not in allowed or not question:
        return None
    options, answer = [], ""
    if qtype == "mcq":
        options = [as_text(o) for o in (it.get("options") or []) if as_text(o)]
        try:
            idx = int(it.get("correct_index"))
        except (TypeError, ValueError):
            return None
        if not (2 <= len(options) <= 6 and 0 <= idx < len(options)):
            return None
        answer = options[idx]
        random.shuffle(options)
    elif qtype == "tf":
        raw = as_text(it.get("answer", "")).lower()
        if raw in ("صح", "صحيح", "صواب", "true", "نعم"):
            answer = "صح"
        elif raw in ("خطأ", "خاطئ", "خطا", "false", "لا"):
            answer = "خطأ"
        else:
            return None
    else:
        answer = as_text(it.get("answer", ""))
        if not answer:
            return None
    return {
        "id": uuid.uuid4().hex,
        "unit": unit,
        "level": level,
        "type": qtype,
        "question": question,
        "options": options,
        "answer": answer,
        "interval": 1,
        "next_review": today_str(),
        "correct": 0,
        "wrong": 0,
    }


def generate_for_unit(key, unit, text, level, plan):
    """تعمل في خيط مستقل، لذلك لا تستخدم أي أمر من Streamlit هنا."""
    if len(text) > MAX_CHARS_PER_UNIT:
        start = random.randint(0, len(text) - MAX_CHARS_PER_UNIT)
        text = text[start:start + MAX_CHARS_PER_UNIT]
    allowed = [t for t, n in plan.items() if n]
    prompt = build_prompt(text, level, plan)
    last_err = "لم يُرجع النموذج أسئلة صالحة"
    for _ in range(2):
        try:
            data = parse_json(ask_gemini(prompt, key))
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            items = [normalize_item(it, unit, level, allowed) for it in data]
            items = [x for x in items if x]
            if items:
                return items
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"«{unit}»: {last_err}")


def grade_open_answers(items, key):
    prompt = f"""أنت معلم يصحح إجابات طلاب المرحلة المتوسطة.
قارن كل إجابة طالب بالإجابة النموذجية، وأعطِ درجة من 0 إلى 1:
1 = صحيحة، 0.5 = صحيحة جزئيًا، 0 = خاطئة.
تقبل اختلاف الصياغة إذا كان المعنى صحيحًا، واكتب تعليقًا قصيرًا ومشجعًا بالعربية.
أخرج JSON فقط بهذا الشكل:
[{{"id": "0", "score": 1, "feedback": "..."}}]

الإجابات:
{json.dumps(items, ensure_ascii=False)}
"""
    try:
        data = parse_json(ask_gemini(prompt, key))
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        out = {}
        for r in data:
            score = max(0.0, min(1.0, float(r.get("score", 0))))
            out[str(r["id"])] = (score, str(r.get("feedback", "")).strip())
        return out
    except Exception:
        return {}


def plan_types(n, types):
    order = list(types)
    random.shuffle(order)
    plan = {t: 0 for t in types}
    for i in range(n):
        plan[order[i % len(order)]] += 1
    return plan


# ================== قراءة الكتاب ==================
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
        size = 6000
        sections = [(f"الجزء {i // size + 1}", full_text[i:i + size])
                    for i in range(0, len(full_text), size)]

    result = {}
    for t, body in sections:
        key, n = t, 2
        while key in result:
            key = f"{t} ({n})"
            n += 1
        result[key] = body
    return result


@st.cache_data(show_spinner=False)
def load_sections(file_bytes):
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return split_into_sections(text) if text.strip() else {}


# ================== التكرار المتباعد ==================
def update_srs(q, score):
    if score >= 0.6:
        q["correct"] += 1
        q["interval"] = max(1, q.get("interval", 1) * 2)
    else:
        q["wrong"] += 1
        q["interval"] = 1
    q["next_review"] = (dt.date.today() + dt.timedelta(days=q["interval"])).isoformat()


# ================== بناء الجلسة ==================
def build_session(units, types, level, count):
    bank = load_bank()
    sections = st.session_state.get("sections", {})
    today = today_str()

    pool = [q for q in bank.values()
            if q["unit"] in units and q["type"] in types and q.get("level") == level]
    due = [q for q in pool if q["next_review"] <= today]
    random.shuffle(due)
    chosen = due[: count // 2]
    needed = count - len(chosen)

    gen_units = [u for u in units if u in sections]
    if needed > 0 and gen_units:
        per, extra = divmod(needed, len(gen_units))
        jobs = [(u, per + (1 if i < extra else 0)) for i, u in enumerate(gen_units)]
        jobs = [(u, n) for u, n in jobs if n > 0]
        key = (st.session_state.get("api_key") or "").strip()
        new, errors = [], []
        with st.spinner("✨ جارٍ إعداد أسئلتك..."):
            with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as ex:
                futures = [
                    ex.submit(generate_for_unit, key, u, sections[u], level, plan_types(n, types))
                    for u, n in jobs
                ]
                for f in futures:
                    try:
                        new.extend(f.result())
                    except Exception as e:
                        errors.append(str(e))
        if not new and errors:
            raise RuntimeError(" | ".join(errors))
        for q in new:
            bank[q["id"]] = q
        chosen.extend(new)
        save_bank(bank)

    if len(chosen) < count:
        taken = {q["id"] for q in chosen}
        extra_pool = [q for q in pool if q["id"] not in taken]
        random.shuffle(extra_pool)
        chosen.extend(extra_pool[: count - len(chosen)])

    random.shuffle(chosen)
    return chosen[:count]


# ================== تصميم ==================
def inject_css():
    st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap');
.stApp, .stApp p, .stApp label, .stApp li, .stApp textarea, .stApp input, .stApp button, .stApp h1, .stApp h2, .stApp h3, .stApp h4 {
  font-family: 'Cairo', sans-serif;
}
.stApp { direction: rtl; background: #f6f7fc; }
.block-container { max-width: 780px; padding-top: 1.5rem; padding-bottom: 3rem; }
#MainMenu, footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }

.hero { background: linear-gradient(135deg,#4f46e5 0%,#7c3aed 55%,#db2777 100%); color:#fff;
  border-radius:26px; padding:26px 20px; text-align:center; box-shadow:0 14px 32px rgba(79,70,229,.30); margin-bottom:18px; }
.logo { font-size:2.8rem; font-weight:800; letter-spacing:2px; line-height:1.2; }
.hero-title { font-size:1.15rem; font-weight:700; margin-top:4px; }
.hero-sub { font-size:.92rem; opacity:.9; margin-top:4px; }

.steps { display:flex; gap:8px; margin:0 0 18px; }
.step { flex:1; text-align:center; padding:10px 6px; border-radius:16px; background:#e9ecfb; color:#6b7280; font-weight:700; font-size:.88rem; }
.step.active { background:linear-gradient(90deg,#6366f1,#8b5cf6); color:#fff; box-shadow:0 6px 16px rgba(99,102,241,.35); }
.step.done { background:#dcfce7; color:#166534; }

div[data-testid="stVerticalBlockBorderWrapper"] { background:#fff; border-radius:20px !important; box-shadow:0 6px 20px rgba(30,27,75,.06); }
.card { background:#fff; border:1px solid #e7e9f6; border-radius:20px; padding:18px 20px; box-shadow:0 6px 20px rgba(30,27,75,.06); margin-bottom:14px; }
.muted { color:#6b7280; font-size:.92rem; }
.badge { display:inline-block; color:#fff; padding:4px 12px; border-radius:999px; font-size:.82rem; font-weight:700; }
.qnum { color:#6b7280; font-weight:700; margin-right:6px; }
.qtext { font-size:1.1rem; font-weight:700; color:#1e1b4b; line-height:2; margin:6px 0 8px; }
.pill { display:inline-block; background:#eef2ff; color:#4338ca; padding:4px 12px; border-radius:999px; font-weight:700; font-size:.85rem; margin-left:6px; }
.chip { display:inline-block; background:#f1f5ff; color:#3730a3; border:1px solid #dfe5ff; padding:5px 12px; border-radius:999px; font-size:.85rem; margin:4px 4px 0 0; }
.track { background:#e5e7eb; height:10px; border-radius:999px; overflow:hidden; margin:6px 0; }
.fill { height:100%; background:linear-gradient(90deg,#6366f1,#a855f7); border-radius:999px; }

.timer { background:#fff7ed; color:#9a3412; border:1px solid #fed7aa; padding:10px 14px; border-radius:14px; font-weight:800; text-align:center; margin-bottom:12px; font-size:1.05rem; }
.timer.danger { background:#fef2f2; color:#b91c1c; border-color:#fecaca; }

.result { background:linear-gradient(135deg,#4f46e5,#db2777); color:#fff; border-radius:24px; padding:24px; text-align:center; margin-bottom:16px; box-shadow:0 12px 28px rgba(79,70,229,.3); }
.result .big { font-size:3rem; font-weight:800; line-height:1.1; }
.result .sub { font-size:1rem; opacity:.95; margin-top:4px; }
.rcard { background:#fff; border-radius:16px; padding:14px 16px; margin-bottom:10px; border-right:6px solid #cbd5e1; box-shadow:0 4px 14px rgba(30,27,75,.05); }
.rcard.ok { border-right-color:#22c55e; }
.rcard.part { border-right-color:#f59e0b; }
.rcard.bad { border-right-color:#ef4444; }
.rcard .ans { margin-top:6px; color:#374151; font-size:.95rem; line-height:1.8; }
.rcard .fb { color:#4338ca; font-size:.9rem; margin-top:4px; }

.stats { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }
.stat { background:#fff; border-radius:18px; padding:16px; text-align:center; box-shadow:0 6px 20px rgba(30,27,75,.06); }
.stat b { display:block; font-size:1.8rem; color:#312e81; }
.stat span { color:#6b7280; font-size:.9rem; }
.stat.hot b { color:#db2777; }
.unit-row { padding:10px 0; border-bottom:1px solid #eef0f8; }
.unit-head { display:flex; justify-content:space-between; font-weight:700; color:#1e1b4b; }
.empty { text-align:center; color:#6b7280; padding:30px 16px; }

.stTabs [data-baseweb="tab-list"] { gap:6px; background:#fff; padding:6px; border-radius:16px; box-shadow:0 4px 14px rgba(30,27,75,.06); }
.stTabs [data-baseweb="tab"] { border-radius:12px; padding:8px 14px; font-weight:700; }
button[kind="primary"], button[data-testid="stBaseButton-primary"] {
  background:linear-gradient(90deg,#6366f1,#8b5cf6) !important; border:none !important; color:#fff !important;
  border-radius:14px !important; font-weight:800 !important; padding:.65rem 1rem !important;
}
.stButton button { border-radius:14px; font-weight:700; }
@media (max-width:600px) { .logo { font-size:2.2rem; } }
</style>
""", unsafe_allow_html=True)


def key_box():
    default = ""
    try:
        default = st.secrets.get("GOOGLE_API_KEY", "")
    except Exception:
        pass
    st.session_state.setdefault("api_key", default)
    with st.expander("🔑 مفتاح التشغيل (مرة واحدة)", expanded=not st.session_state["api_key"]):
        st.text_input("الصق مفتاح Google AI هنا", key="api_key", type="password")
        if (st.session_state.get("api_key") or "").strip():
            st.caption("✅ المفتاح جاهز")
        else:
            st.caption("احصل عليه مجانًا من aistudio.google.com")


def step_bar():
    has_book = bool(st.session_state.get("sections"))
    has_quiz = st.session_state.get("quiz") is not None
    c1 = "done" if has_book else "active"
    c2 = "done" if has_quiz else ("active" if has_book else "")
    c3 = "active" if has_quiz else ""
    st.markdown(
        f'<div class="steps"><div class="step {c1}">1 · الكتاب</div>'
        f'<div class="step {c2}">2 · الإعداد</div>'
        f'<div class="step {c3}">3 · المراجعة</div></div>',
        unsafe_allow_html=True,
    )


# ================== الصفحات ==================
def upload_page():
    st.markdown("#### 📄 ارفع ملف المادة")
    file = st.file_uploader("اختر ملف PDF", type=["pdf"],
                            help="يجب أن يكون الملف نصيًا وليس صورًا ممسوحة")
    if file is not None:
        with st.spinner("📖 جارٍ قراءة الكتاب..."):
            sections = load_sections(file.getvalue())
        if not sections:
            st.error("لم أستطع استخراج نص من هذا الملف. جرّب ملف PDF نصيًا.")
            return
        st.session_state["sections"] = sections

    sections = st.session_state.get("sections")
    if sections:
        names = list(sections)
        chips = "".join(f'<span class="chip">{esc(n)}</span>' for n in names[:30])
        if len(names) > 30:
            chips += f'<span class="chip">+{len(names) - 30} أخرى</span>'
        st.markdown(
            f'<div class="card"><b>✅ تم استخراج {len(names)} وحدة/درس</b>'
            f'<div style="margin-top:10px">{chips}</div></div>',
            unsafe_allow_html=True,
        )
        st.info("الخطوة التالية: انتقل إلى تبويب 🎯 المراجعة")
    else:
        st.markdown(
            '<div class="card empty">📚<br>لم يُرفع كتاب بعد<br>'
            '<span class="muted">ارفع ملف PDF للمادة لتبدأ</span></div>',
            unsafe_allow_html=True,
        )


def start_page():
    sections = st.session_state.get("sections", {})
    if not sections:
        st.markdown(
            '<div class="card empty">⬆️<br>ارفع ملف الكتاب من تبويب 📄 الكتاب أولًا</div>',
            unsafe_allow_html=True,
        )
        return

    units = st.multiselect("📚 الوحدات أو الدروس", list(sections), default=list(sections)[:2])
    level = st.radio("🎚️ مستوى الصعوبة", LEVELS, index=1, horizontal=True)
    st.caption(LEVEL_DESC[level])
    types = st.multiselect(
        "🧩 أنواع الأسئلة",
        list(TYPES),
        default=list(TYPES),
        format_func=lambda k: f"{TYPE_ICONS[k]} {TYPES[k]}",
    )
    count = st.slider("🔢 عدد الأسئلة", 3, 25, 8)
    mode = st.radio("🧠 نظام المراجعة", ["تدريب مفتوح", "محاكاة اختبار بوقت"], horizontal=True)
    minutes = None
    if mode == "محاكاة اختبار بوقت":
        minutes = st.number_input("⏱️ مدة الاختبار (بالدقائق)", 1, 90, 10)

    st.markdown(
        f'<div class="card"><b>ملخص الجلسة</b><div class="muted">'
        f'{len(units)} وحدة · {count} سؤال · مستوى {level} · {mode}</div></div>',
        unsafe_allow_html=True,
    )

    if st.button("🚀 ابدأ المراجعة", type="primary"):
        if not (st.session_state.get("api_key") or "").strip():
            st.error("أدخل مفتاح التشغيل من القسم العلوي أولًا.")
        elif not units or not types:
            st.error("اختر وحدة واحدة على الأقل ونوع سؤال واحدًا على الأقل.")
        else:
            for k in [k for k in st.session_state if k.startswith("ans_")]:
                del st.session_state[k]
            try:
                questions = build_session(units, types, level, count)
            except Exception as e:
                st.error(f"تعذّر إنشاء الأسئلة: {e}")
            else:
                if not questions:
                    st.error("لم تُنشأ أسئلة، حاول مرة أخرى.")
                else:
                    st.session_state["quiz"] = {
                        "questions": questions,
                        "start": time.time(),
                        "minutes": minutes,
                        "level": level,
                        "done": False,
                        "expired": False,
                        "results": [],
                    }
                    st.rerun()


def timer_box(quiz):
    @st.fragment(run_every=1)
    def tick():
        remaining = quiz["minutes"] * 60 - (time.time() - quiz["start"])
        if remaining <= 0:
            quiz["expired"] = True
            st.rerun(scope="app")
        m, s = divmod(int(remaining), 60)
        cls = "timer danger" if remaining < 60 else "timer"
        st.markdown(f'<div class="{cls}">⏳ الوقت المتبقي: {m:02d}:{s:02d}</div>',
                    unsafe_allow_html=True)

    tick()


def quiz_page(quiz):
    if quiz.get("expired"):
        finish_quiz(quiz)
        st.rerun()

    total = len(quiz["questions"])
    answered = sum(1 for i in range(total)
                   if st.session_state.get(f"ans_{i}") not in (None, ""))
    pct = int(answered / total * 100) if total else 0
    st.markdown(
        f'<div class="card"><div><span class="pill">🎚️ {esc(quiz["level"])}</span>'
        f'<span class="pill">📝 {total} سؤال</span></div>'
        f'<div class="muted" style="margin-top:8px">أجبت عن {answered} من {total}</div>'
        f'<div class="track"><div class="fill" style="width:{pct}%"></div></div></div>',
        unsafe_allow_html=True,
    )

    if quiz["minutes"]:
        timer_box(quiz)

    for i, q in enumerate(quiz["questions"]):
        color = TYPE_COLORS[q["type"]]
        with st.container(border=True):
            st.markdown(
                f'<span class="badge" style="background:{color}">'
                f'{TYPE_ICONS[q["type"]]} {TYPES[q["type"]]}</span>'
                f'<span class="qnum">س{i + 1}</span>',
                unsafe_allow_html=True,
            )
            st.markdown(f'<div class="qtext">{esc(q["question"])}</div>', unsafe_allow_html=True)
            if q["type"] == "mcq":
                st.radio("اختر الإجابة", q["options"], index=None, key=f"ans_{i}",
                         label_visibility="collapsed")
            elif q["type"] == "tf":
                st.radio("اختر", ["صح", "خطأ"], index=None, horizontal=True,
                         key=f"ans_{i}", label_visibility="collapsed")
            else:
                st.text_area("اكتب إجابتك", key=f"ans_{i}", placeholder="اكتب إجابتك هنا...",
                             label_visibility="collapsed", height=90)

    if st.button("✅ إنهاء وتصحيح الإجابات", type="primary"):
        finish_quiz(quiz)
        st.rerun()


def finish_quiz(quiz):
    bank = load_bank()
    key = (st.session_state.get("api_key") or "").strip()
    n = len(quiz["questions"])
    answers = [str(st.session_state.get(f"ans_{i}") or "").strip() for i in range(n)]
    scores, feedback, to_grade = {}, {}, []

    for i, q in enumerate(quiz["questions"]):
        ans = answers[i]
        if q["type"] in ("mcq", "tf"):
            ok = ans == q["answer"]
            scores[i] = 1.0 if ok else 0.0
            feedback[i] = "إجابة صحيحة، أحسنت! ✅" if ok else "إجابة غير صحيحة، راجع الإجابة النموذجية ❌"
        elif not ans:
            scores[i], feedback[i] = 0.0, "لم تكتب إجابة."
        else:
            to_grade.append({"id": str(i), "question": q["question"],
                             "model_answer": q["answer"], "student_answer": ans})

    if to_grade:
        with st.spinner("🤖 جارٍ التصحيح الذكي..."):
            graded = grade_open_answers(to_grade, key)
        for item in to_grade:
            i = int(item["id"])
            scores[i], feedback[i] = graded.get(
                item["id"], (0.0, "تعذّر التصحيح الذكي، قارن إجابتك بالنموذجية.")
            )

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
    n = len(results)
    total = sum(r["score"] for r in results)
    pct = total / n * 100 if n else 0
    if pct >= 80:
        msg = "ممتاز! أنت تحفظ بإتقان 🌟"
    elif pct >= 50:
        msg = "جيد، واصل المراجعة لتتحسن 💪"
    else:
        msg = "لا بأس! التكرار المتباعد سيثبّت ما نسيته 🔁"

    st.markdown(
        f'<div class="result"><div class="big">{pct:.0f}%</div>'
        f'<div class="sub">{total:g} من {n} إجابة صحيحة</div>'
        f'<div class="sub">{msg}</div></div>',
        unsafe_allow_html=True,
    )
    if pct >= 80:
        st.balloons()

    st.markdown("#### 📋 تفاصيل الإجابات")
    for i, r in enumerate(results, 1):
        q = r["q"]
        if r["score"] >= 0.8:
            cls, icon = "ok", "✅"
        elif r["score"] > 0:
            cls, icon = "part", "🟡"
        else:
            cls, icon = "bad", "❌"
        student = esc(r["student"] or "لم تُجب")
        st.markdown(
            f'<div class="rcard {cls}"><b>{icon} س{i}: {esc(q["question"])}</b>'
            f'<div class="ans">إجابتك: {student}<br>'
            f'الإجابة الصحيحة: <b>{esc(q["answer"])}</b></div>'
            f'<div class="fb">{esc(r["feedback"])}</div></div>',
            unsafe_allow_html=True,
        )

    if st.button("🔄 اختبار جديد"):
        st.session_state.pop("quiz", None)
        st.rerun()


def progress_page():
    bank = load_bank()
    if not bank:
        st.markdown(
            '<div class="card empty">📊<br>لا توجد بيانات بعد<br>'
            '<span class="muted">ستظهر إحصائياتك هنا بعد أول اختبار</span></div>',
            unsafe_allow_html=True,
        )
        return

    today = today_str()
    total = len(bank)
    correct = sum(q["correct"] for q in bank.values())
    wrong = sum(q["wrong"] for q in bank.values())
    due = sum(1 for q in bank.values() if q["next_review"] <= today)
    st.markdown(
        f'<div class="stats">'
        f'<div class="stat"><b>{total}</b><span>سؤال محفوظ</span></div>'
        f'<div class="stat"><b>{correct}</b><span>إجابة صحيحة</span></div>'
        f'<div class="stat"><b>{wrong}</b><span>إجابة خاطئة</span></div>'
        f'<div class="stat hot"><b>{due}</b><span>للمراجعة اليوم</span></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    units = {}
    for q in bank.values():
        u = units.setdefault(q["unit"], {"count": 0, "ok": 0, "bad": 0, "due": 0})
        u["count"] += 1
        u["ok"] += q["correct"]
        u["bad"] += q["wrong"]
        if q["next_review"] <= today:
            u["due"] += 1

    rows = ""
    for name, u in units.items():
        att = u["ok"] + u["bad"]
        p = int(u["ok"] / att * 100) if att else 0
        rows += (
            f'<div class="unit-row"><div class="unit-head"><span>{esc(name)}</span>'
            f'<span>{p}%</span></div><div class="track"><div class="fill" style="width:{p}%">'
            f'</div></div><div class="muted">{u["count"]} سؤال · {u["due"]} للمراجعة اليوم</div></div>'
        )
    st.markdown(f'<div class="card">{rows}</div>', unsafe_allow_html=True)

    if st.button("🗑️ مسح سجل التقدم"):
        save_bank({})
        st.rerun()


# ================== البرنامج الرئيسي ==================
def main():
    st.set_page_config(page_title=APP_NAME, page_icon="📚", layout="centered")
    inject_css()
    st.markdown(
        f'<div class="hero"><div class="logo">{APP_NAME}</div>'
        f'<div class="hero-title">مساعدك الذكي لمراجعة المواد الحفظية</div>'
        f'<div class="hero-sub">حوّل كتابك المدرسي إلى اختبارات ذكية في ثوانٍ</div></div>',
        unsafe_allow_html=True,
    )
    key_box()
    step_bar()

    t1, t2, t3 = st.tabs(["📄 الكتاب", "🎯 المراجعة", "📊 تقدّمي"])
    with t1:
        upload_page()
    with t2:
        quiz = st.session_state.get("quiz")
        if quiz and not quiz["done"]:
            quiz_page(quiz)
        elif quiz:
            show_results(quiz)
        else:
            start_page()
    with t3:
        progress_page()


main()
