"""
DSA Tracker  -  11 Oct 2026 -> 31 Dec 2026
Run:  streamlit run app.py

* 2 questions/day, in the topic order defined in questions.py
* Tick a question = done. Missed / failed questions are automatically re-slotted
  into the remaining days of THIS week (spread evenly); if the week is full they
  go to NEXT week.
* Upload/paste your code -> saved to  submissions/<QID>_<slug>/<timestamp>.<ext>
* Progress is stored in  data/progress.json
"""
import json
from collections import defaultdict
import base64
import hmac
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import pandas as pd
import streamlit as st

from questions import QUESTIONS, TOPIC_ORDER

# ----------------------------------------------------------------- config
START = date(2026, 10, 11)
END = date(2026, 12, 31)
PER_DAY = 2          # fresh questions per day
DAY_CAP = 4          # max questions on one day (2 fresh + up to 2 backlog)

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
SUBS_DIR = BASE / "submissions"
PROGRESS_FILE = DATA_DIR / "progress.json"
LANG_EXT = {"C++": ".cpp", "Python": ".py", "Java": ".java", "C": ".c",
            "JavaScript": ".js", "Other": ".txt"}

try:
    DATA_DIR.mkdir(exist_ok=True)
    SUBS_DIR.mkdir(exist_ok=True)
except OSError:
    pass

for i, q in enumerate(QUESTIONS):
    q["orig"] = START + timedelta(days=i // PER_DAY)
QMAP = {q["id"]: q for q in QUESTIONS}


# ----------------------------------------------------------------- storage
# Local mode  : files in ./data and ./submissions
# GitHub mode : (when GITHUB_TOKEN + GITHUB_REPO are in Streamlit secrets) the same files are
#               saved to a separate branch ("data") of your repo, so nothing is lost when the
#               free host restarts, and saving never redeploys the app.
API = "https://api.github.com"


def now_ist() -> datetime:
    """Current Indian time (the cloud server runs in UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def today_ist() -> date:
    return now_ist().date()


def gh_conf():
    try:
        s = st.secrets
        if "GITHUB_TOKEN" in s and "GITHUB_REPO" in s:
            return {"tok": s["GITHUB_TOKEN"], "repo": s["GITHUB_REPO"],
                    "branch": s.get("DATA_BRANCH", "data")}
    except Exception:
        pass
    return None


def _h(tok):
    return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}


def _fail(r, what):
    raise RuntimeError(f"GitHub error while {what}: {r.status_code} {r.text[:200]}")


@st.cache_resource(show_spinner=False)
def gh_ready(tok, repo, branch):
    """Make sure the data branch exists (created from the default branch)."""
    r = requests.get(f"{API}/repos/{repo}/branches/{branch}", headers=_h(tok), timeout=20)
    if r.status_code == 200:
        return True
    if r.status_code != 404:
        _fail(r, "checking the data branch (is the token/repo name right?)")
    info = requests.get(f"{API}/repos/{repo}", headers=_h(tok), timeout=20)
    if info.status_code != 200:
        _fail(info, "reading the repo")
    default = info.json()["default_branch"]
    ref = requests.get(f"{API}/repos/{repo}/git/ref/heads/{default}", headers=_h(tok), timeout=20)
    if ref.status_code != 200:
        _fail(ref, "reading the default branch")
    mk = requests.post(f"{API}/repos/{repo}/git/refs", headers=_h(tok), timeout=20,
                       json={"ref": f"refs/heads/{branch}", "sha": ref.json()["object"]["sha"]})
    if mk.status_code not in (200, 201):
        _fail(mk, "creating the data branch")
    return True


@st.cache_data(ttl=30, show_spinner=False)
def gh_tree(tok, repo, branch):
    r = requests.get(f"{API}/repos/{repo}/git/trees/{branch}", params={"recursive": "1"},
                     headers=_h(tok), timeout=30)
    if r.status_code != 200:
        _fail(r, "listing files")
    return {t["path"]: t["sha"] for t in r.json().get("tree", []) if t["type"] == "blob"}


@st.cache_data(max_entries=300, show_spinner=False)
def gh_blob(tok, repo, sha):
    r = requests.get(f"{API}/repos/{repo}/git/blobs/{sha}", headers=_h(tok), timeout=30)
    if r.status_code != 200:
        _fail(r, "reading a file")
    return base64.b64decode(r.json()["content"])


def store_read(path):
    c = gh_conf()
    if c:
        gh_ready(c["tok"], c["repo"], c["branch"])
        sha = gh_tree(c["tok"], c["repo"], c["branch"]).get(path)
        return gh_blob(c["tok"], c["repo"], sha) if sha else None
    f = BASE / path
    return f.read_bytes() if f.exists() else None


def store_write(path, data: bytes, msg="update"):
    c = gh_conf()
    if c:
        gh_ready(c["tok"], c["repo"], c["branch"])
        r = None
        for _ in range(2):                      # retry once if the file changed under us
            sha = gh_tree(c["tok"], c["repo"], c["branch"]).get(path)
            body = {"message": msg, "branch": c["branch"],
                    "content": base64.b64encode(data).decode()}
            if sha:
                body["sha"] = sha
            r = requests.put(f"{API}/repos/{c['repo']}/contents/{path}",
                             headers=_h(c["tok"]), json=body, timeout=30)
            gh_tree.clear()
            if r.status_code in (200, 201):
                return
            if r.status_code not in (409, 422):
                break
        _fail(r, "saving a file")
    f = BASE / path
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(f)


def store_list(prefix):
    c = gh_conf()
    if c:
        gh_ready(c["tok"], c["repo"], c["branch"])
        return sorted(p for p in gh_tree(c["tok"], c["repo"], c["branch"]) if p.startswith(prefix))
    base = BASE / prefix
    if not base.exists():
        return []
    return sorted(str(p.relative_to(BASE)).replace("\\", "/") for p in base.rglob("*") if p.is_file())


def load_progress() -> dict:
    raw = store_read("data/progress.json")
    if raw:
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            return {}
    return {}


def save_progress(prog: dict) -> None:
    store_write("data/progress.json", json.dumps(prog, indent=2).encode(), "Update progress")


def list_submissions(q):
    return sorted(store_list(f"submissions/{q['id']}_{q['slug']}/"), reverse=True)


def save_submission(q, lang, upload, pasted):
    if upload is not None:
        ext = re.sub(r"[^A-Za-z0-9.]", "", Path(upload.name).suffix)[:10] or LANG_EXT[lang]
        data = upload.getvalue()
    elif pasted and pasted.strip():
        ext, data = LANG_EXT[lang], pasted.encode()
    else:
        return None
    rel = f"submissions/{q['id']}_{q['slug']}/{now_ist():%Y%m%d_%H%M%S}{ext}"
    store_write(rel, data, f"Save solution {q['id']}")
    return rel


# ----------------------------------------------------------------- scheduler
def week_bounds(d: date):
    start = d - timedelta(days=d.weekday())          # Monday
    return start, start + timedelta(days=6)          # Sunday


def build_plan(prog: dict, today: date):
    """
    Returns (assigned: {qid: date}, missed: set(qid)).
    - done questions sit on the day they were completed
    - pending, not-yet-due questions stay on their original day
    - missed (past due, or marked 'failed' today) are re-slotted:
        1) remaining days of the current week, least-loaded day first
        2) if that week is full, next week, and so on
    """
    assigned, count, missed = {}, defaultdict(int), []
    for q in QUESTIONS:
        p = prog.get(q["id"], {})
        if p.get("done"):
            d = date.fromisoformat(p.get("done_on") or q["orig"].isoformat())
            assigned[q["id"]] = d
            count[d] += 1
            continue
        failed_today = today.isoformat() in p.get("failed", [])
        if q["orig"] < today or failed_today:
            missed.append((q, failed_today))
        else:
            assigned[q["id"]] = q["orig"]
            count[q["orig"]] += 1

    for q, failed_today in missed:                      # oldest first
        start = today + timedelta(days=1) if failed_today else today
        _, week_end = week_bounds(start)
        cands = [start + timedelta(i) for i in range((week_end - start).days + 1)]
        cands = [d for d in cands if count[d] < DAY_CAP]
        nxt = week_end + timedelta(days=1)              # next Monday
        while not cands:                                # roll into next week(s)
            cands = [nxt + timedelta(i) for i in range(7) if count[nxt + timedelta(i)] < DAY_CAP]
            nxt += timedelta(days=7)
        d = min(cands, key=lambda x: (count[x], x))
        assigned[q["id"]] = d
        count[d] += 1
    return assigned, {q["id"] for q, _ in missed}


# ----------------------------------------------------------------- callbacks
def toggle_done(qid, key, today):
    prog = load_progress()
    p = prog.setdefault(qid, {})
    p["done"] = bool(st.session_state[key])
    p["done_on"] = today.isoformat() if p["done"] else None
    save_progress(prog)


def mark_failed(qid, today):
    prog = load_progress()
    p = prog.setdefault(qid, {})
    p.setdefault("failed", [])
    if today.isoformat() not in p["failed"]:
        p["failed"].append(today.isoformat())
    save_progress(prog)


def fmt(d: date) -> str:
    return d.strftime("%a %d %b")


# ----------------------------------------------------------------- UI pieces
DIFF_ICON = {"Easy": "🟢", "Medium": "🟡", "Hard": "🔴"}


def render_question(q, plan_date, is_missed, prefix, today, prog):
    qid, p = q["id"], prog.get(q["id"], {})
    done = bool(p.get("done"))
    c1, c2, c3 = st.columns([0.7, 7, 2.3])
    key = f"{prefix}_chk_{qid}"
    c1.checkbox("done", value=done, key=key, label_visibility="collapsed",
                on_change=toggle_done, args=(qid, key, today))
    title = f"[{q['title']}]({q['url']})"
    title = f"~~{title}~~" if done else f"**{title}**"
    tags = [f"`{q['topic']}`", f"{DIFF_ICON[q['diff']]} {q['diff']}"]
    if not done and plan_date != q["orig"]:
        tags.append(f"🔁 moved from {fmt(q['orig'])}")
    if is_missed and not done:
        tags.append("⚠️ backlog")
    if p.get("failed"):
        tags.append(f"❌ {len(p['failed'])} failed attempt(s)")
    c2.markdown(f"{q['id']} · {title}  \n{' · '.join(tags)}")
    n = len(list_submissions(q))
    c3.caption(f"📁 {n} code file(s)" if n else "no code yet")

    with st.expander("Code / actions", expanded=False):
        if not done and plan_date <= today + timedelta(days=0):
            st.button("❌ Couldn't solve it - move to later", key=f"{prefix}_fail_{qid}",
                      on_click=mark_failed, args=(qid, today))
        with st.form(key=f"{prefix}_form_{qid}", clear_on_submit=True):
            lang = st.selectbox("Language", list(LANG_EXT), key=f"{prefix}_lang_{qid}")
            up = st.file_uploader("Upload your solution file", key=f"{prefix}_up_{qid}")
            pasted = st.text_area("...or paste code", height=150, key=f"{prefix}_paste_{qid}")
            mark = st.checkbox("Also mark as solved ✅", value=True, key=f"{prefix}_mk_{qid}")
            if st.form_submit_button("💾 Save code"):
                path = save_submission(q, lang, up, pasted)
                if path is None:
                    st.warning("Upload a file or paste some code first.")
                else:
                    if mark:
                        pr = load_progress()
                        pp = pr.setdefault(qid, {})
                        pp["done"], pp["done_on"] = True, today.isoformat()
                        save_progress(pr)
                    st.success(f"Saved: {path}")
                    st.rerun()
        subs = list_submissions(q)
        if subs:
            pick = st.selectbox("Saved versions", subs, format_func=lambda p: Path(p).name,
                                key=f"{prefix}_ver_{qid}")
            blob = store_read(pick) or b""
            st.code(blob.decode(errors="replace"), language=None)
            st.download_button("⬇️ Download", blob, file_name=Path(pick).name,
                               key=f"{prefix}_dl_{qid}")


# ----------------------------------------------------------------- pages
def page_today(today, prog, assigned, missed):
    st.header(f"📅 Today - {today.strftime('%A, %d %B %Y')}")
    if today < START:
        st.info(f"Plan starts on {fmt(START)} - {(START - today).days} day(s) to go. Here is Day 1:")
        view_day = START
    else:
        view_day = today
    items = [QMAP[qid] for qid, d in assigned.items() if d == view_day]
    items.sort(key=lambda q: (q["orig"] == view_day, q["orig"], q["id"]))
    if not items:
        st.success("No questions today - buffer / revision day. Revise your saved code!")
        return
    done_n = sum(1 for q in items if prog.get(q["id"], {}).get("done"))
    st.progress(done_n / len(items), text=f"{done_n}/{len(items)} done today")
    backlog_today = [q for q in items if q["orig"] != view_day and not prog.get(q["id"], {}).get("done")]
    if backlog_today:
        st.warning(f"{len(backlog_today)} backlog question(s) are due today - clear them first.")
    for q in items:
        render_question(q, view_day, q["id"] in missed, "today", today, prog)
        st.divider()


def page_timetable(today, prog, assigned, missed):
    st.header("🗓️ Timetable")
    weeks, d = [], week_bounds(START)[0]
    while d <= END + timedelta(days=21):
        weeks.append(d)
        d += timedelta(days=7)
    labels = {w: f"Week {i + 1}:  {fmt(max(w, START))} → {fmt(w + timedelta(days=6))}"
              for i, w in enumerate(weeks)}
    cur = week_bounds(max(today, START))[0]
    wk = st.selectbox("Week", weeks, index=weeks.index(cur) if cur in weeks else 0,
                      format_func=lambda w: labels[w])
    only_pending = st.toggle("Show only pending", value=False)

    by_day = defaultdict(list)
    for qid, dd in assigned.items():
        by_day[dd].append(QMAP[qid])

    for i in range(7):
        day = wk + timedelta(days=i)
        if day < START:
            continue
        qs = sorted(by_day.get(day, []), key=lambda q: (q["orig"] == day, q["orig"], q["id"]))
        if only_pending:
            qs = [q for q in qs if not prog.get(q["id"], {}).get("done")]
        head = f"### {fmt(day)}" + ("  🔵 TODAY" if day == today else "")
        if day > END:
            head += "  ⚠️ (after 31 Dec deadline)"
        st.markdown(head)
        if not qs:
            st.caption("Buffer / revision day" if day <= END else "—")
        for q in qs:
            render_question(q, day, q["id"] in missed, f"tt{day.isoformat()}", today, prog)
        st.divider()

    rows = [{"Date": assigned[q["id"]], "Original date": q["orig"], "ID": q["id"],
             "Topic": q["topic"], "Question": q["title"], "Difficulty": q["diff"],
             "Done": bool(prog.get(q["id"], {}).get("done")), "Link": q["url"]}
            for q in QUESTIONS]
    csv = pd.DataFrame(rows).sort_values(["Date", "ID"]).to_csv(index=False)
    st.download_button("⬇️ Export full timetable (CSV)", csv, file_name="dsa_timetable.csv")


def page_tracker(today, prog, assigned, missed):
    st.header("📊 Tracker & Alerts")
    total = len(QUESTIONS)
    done_total = sum(1 for q in QUESTIONS if prog.get(q["id"], {}).get("done"))
    due_before_today = sum(1 for q in QUESTIONS if q["orig"] < today)
    done_days = {date.fromisoformat(p["done_on"]) for p in prog.values() if p.get("done") and p.get("done_on")}
    streak, d = 0, today if today in done_days else today - timedelta(days=1)
    while d in done_days:
        streak += 1
        d -= timedelta(days=1)

    m = st.columns(4)
    m[0].metric("Solved", f"{done_total}/{total}")
    m[1].metric("Due before today", due_before_today)
    m[2].metric("Backlog", len(missed))
    m[3].metric("Streak 🔥", f"{streak} day(s)")
    st.progress(done_total / total if total else 0)

    # -------- alerts
    st.subheader("🚨 Missed questions - must cover")
    if not missed:
        st.success("No backlog. You're on track!")
    else:
        this_ws, _ = week_bounds(today)
        rows = []
        for qid in sorted(missed, key=lambda x: QMAP[x]["orig"]):
            q, nd = QMAP[qid], assigned[qid]
            rows.append({"Question": f"{q['id']} {q['title']}", "Topic": q["topic"],
                         "Missed on": fmt(q["orig"]), "Cover on": fmt(nd),
                         "Days left": (nd - today).days,
                         "When": "this week" if week_bounds(nd)[0] == this_ws else "next week+"})
        st.error(f"{len(missed)} question(s) pending from earlier days. They were re-slotted as below:")
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)
        late = [q for q in missed if assigned[q] > END]
        if late:
            st.warning(f"{len(late)} backlog question(s) now fall after 31 Dec - catch up faster!")

    pending_after = [d for qid, d in assigned.items() if d > END and not prog.get(qid, {}).get("done")]
    if pending_after:
        st.warning(f"{len(pending_after)} question(s) are scheduled after {fmt(END)}.")

    # -------- week by week
    st.subheader("Week by week")
    rows, w = [], week_bounds(START)[0]
    i = 1
    while w <= END:
        we = w + timedelta(days=6)
        qs = [q for q in QUESTIONS if w <= q["orig"] <= we]
        if qs:
            done = sum(1 for q in qs if prog.get(q["id"], {}).get("done"))
            miss = sum(1 for q in qs if q["id"] in missed)
            rows.append({"Week": f"W{i}", "Dates": f"{fmt(max(w, START))} - {fmt(we)}",
                         "Planned": len(qs), "Done": done, "Missed (pending)": miss,
                         "Completion": f"{round(100 * done / len(qs))}%",
                         "Status": "✅ clear" if done == len(qs) else
                                   ("⚠️ backlog" if miss else ("🔵 in progress" if we >= today else "—"))})
        w += timedelta(days=7)
        i += 1
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    # -------- last 14 days
    st.subheader("Solved per day (last 14 days)")
    days = [today - timedelta(days=k) for k in range(13, -1, -1)]
    cnt = defaultdict(int)
    for p in prog.values():
        if p.get("done") and p.get("done_on"):
            cnt[date.fromisoformat(p["done_on"])] += 1
    st.bar_chart(pd.DataFrame({"Solved": [cnt[d] for d in days]}, index=[fmt(d) for d in days]))

    # -------- per topic
    st.subheader("Topic progress")
    for t in TOPIC_ORDER:
        qs = [q for q in QUESTIONS if q["topic"] == t]
        done = sum(1 for q in qs if prog.get(q["id"], {}).get("done"))
        first, last = min(q["orig"] for q in qs), max(q["orig"] for q in qs)
        st.progress(done / len(qs), text=f"{t}: {done}/{len(qs)}   (planned {fmt(first)} → {fmt(last)})")


def page_profile(today, prog, assigned, missed):
    st.header("👤 My Profile - Actual vs Ideal")
    st.subheader(f"As of {today.strftime('%A, %d %B %Y')}")
    if today < START:
        st.info(f"The plan starts on {fmt(START)}. Everything below will fill up from that day.")

    total = len(QUESTIONS)
    is_done = lambda q: bool(prog.get(q["id"], {}).get("done"))
    elapsed = max(0, (min(today, END) - START).days + 1)
    ideal_now = sum(1 for q in QUESTIONS if q["orig"] <= today)
    actual = sum(1 for q in QUESTIONS if is_done(q))
    gap = ideal_now - actual
    done_days = {date.fromisoformat(p["done_on"]) for p in prog.values()
                 if p.get("done") and p.get("done_on")}
    active = len([d for d in done_days if START <= d <= today])
    consistency = round(100 * active / elapsed) if elapsed else 0

    m = st.columns(4)
    m[0].metric("You have solved", actual)
    m[1].metric("Ideal by today", ideal_now)
    m[2].metric("Gap", f"{abs(gap)} {'behind' if gap > 0 else 'ahead' if gap < 0 else ''}".strip(),
                delta=f"{-gap:+d}" if gap else None)
    m[3].metric("Active days", f"{active}/{elapsed} ({consistency}%)")

    if elapsed == 0:
        pass
    elif gap <= 0:
        st.success("✅ You are on track or ahead of the plan. Keep this pace.")
    elif gap <= 4:
        st.warning(f"⚠️ Slightly behind: {gap} question(s) short of the ideal. Clear the backlog this week.")
    else:
        st.error(f"🚨 Behind by {gap} questions. Use the Tracker & Alerts page and cover the backlog first.")

    # ---- projection
    st.subheader("Where this pace takes you")
    remaining = total - actual
    days_left = max((END - today).days + 1, 1) if today <= END else 0
    ideal_finish = max(q["orig"] for q in QUESTIONS)
    c = st.columns(3)
    c[0].metric("Ideal finish date", fmt(ideal_finish))
    if actual and elapsed:
        pace = actual / elapsed
        proj = today + timedelta(days=int(-(-remaining // pace))) if remaining else today
        c[1].metric("Projected finish (your pace)", proj.strftime("%a %d %b %Y"), delta=f"{pace:.1f} q/day")
        if proj > END:
            st.warning(f"At {pace:.1f} questions/day you finish after {fmt(END)}.")
    else:
        c[1].metric("Projected finish (your pace)", "-")
    need = remaining / days_left if days_left else remaining
    c[2].metric("Needed per day from now", f"{need:.1f}" if remaining else "0")

    # ---- chart
    st.subheader("Ideal vs actual (cumulative)")
    days, d = [], START
    while d <= END:
        days.append(d)
        d += timedelta(days=1)
    done_dates = sorted(date.fromisoformat(p["done_on"]) for p in prog.values()
                        if p.get("done") and p.get("done_on"))
    rows = []
    for d in days:
        rows.append({"Date": d,
                     "Ideal plan": sum(1 for q in QUESTIONS if q["orig"] <= d),
                     "You": sum(1 for x in done_dates if x <= d) if d <= today else None})
    st.line_chart(pd.DataFrame(rows).set_index("Date"))

    # ---- topics
    st.subheader("Topic by topic")
    rows = []
    for t in TOPIC_ORDER:
        qs = [q for q in QUESTIONS if q["topic"] == t]
        ideal_t = sum(1 for q in qs if q["orig"] <= today)
        done_t = sum(1 for q in qs if is_done(q))
        finish = max(q["orig"] for q in qs)
        if done_t == len(qs):
            status = "✅ completed"
        elif ideal_t == 0 and done_t == 0:
            status = "⏳ not started (planned)"
        elif done_t >= ideal_t:
            status = "🟢 on track"
        else:
            status = f"🔴 {ideal_t - done_t} behind"
        rows.append({"Topic": t, "Total": len(qs), "Ideal by today": ideal_t,
                     "You solved": done_t, "Ideal finish": fmt(finish), "Status": status})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    # ---- difficulty
    st.subheader("Difficulty mix")
    rows = []
    for diff in ["Easy", "Medium", "Hard"]:
        qs = [q for q in QUESTIONS if q["diff"] == diff]
        rows.append({"Difficulty": f"{DIFF_ICON[diff]} {diff}",
                     "Ideal by today": sum(1 for q in qs if q["orig"] <= today),
                     "You solved": sum(1 for q in qs if is_done(q)),
                     "Total in plan": len(qs)})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    ideal_topics = [t for t in TOPIC_ORDER
                    if all(q["orig"] <= today for q in QUESTIONS if q["topic"] == t)]
    your_topics = [t for t in TOPIC_ORDER
                   if all(is_done(q) for q in QUESTIONS if q["topic"] == t)]
    st.subheader("Topics fully covered")
    st.markdown(f"**Ideal plan by today:** {', '.join(ideal_topics) or 'none yet'}  \n"
                f"**You:** {', '.join(your_topics) or 'none yet'}")


# ----------------------------------------------------------------- main
def check_password() -> bool:
    try:
        pw = st.secrets.get("APP_PASSWORD")
    except Exception:
        pw = None
    if not pw:
        if gh_conf():
            st.warning("No APP_PASSWORD set - anyone with the link can edit your tracker.")
        return True
    if st.session_state.get("auth"):
        return True
    entered = st.text_input("Password", type="password")
    if entered:
        if hmac.compare_digest(entered.encode(), str(pw).encode()):
            st.session_state["auth"] = True
            st.rerun()
        else:
            st.error("Wrong password")
    return False


def main():
    st.set_page_config(page_title="DSA Tracker", page_icon="🧠", layout="wide")
    st.title("🧠 DSA Tracker")
    if not check_password():
        st.stop()

    with st.sidebar:
        page = st.radio("Go to", ["Today", "Timetable", "Tracker & Alerts", "My Profile"])
        with st.expander("🧪 Simulate a date"):
            sim = st.checkbox("Pretend today is...")
            today = st.date_input("Date", value=max(today_ist(), START)) if sim else today_ist()
        st.caption("☁️ Saving to GitHub (data branch)" if gh_conf() else "💾 Saving on this computer")
        st.caption(f"Plan: {fmt(START)} → {fmt(END)} · {len(QUESTIONS)} questions · {PER_DAY}/day")

    prog = load_progress()
    assigned, missed = build_plan(prog, today)

    with st.sidebar:
        if missed:
            st.error(f"⚠️ {len(missed)} backlog question(s)!")
        else:
            st.success("No backlog 🎉")

    if missed and page != "Tracker & Alerts":
        st.error(f"⚠️ You have {len(missed)} missed question(s). They are re-scheduled in the "
                 "coming days - open **Tracker & Alerts** to see exactly when.")

    {"Today": page_today, "Timetable": page_timetable,
     "Tracker & Alerts": page_tracker, "My Profile": page_profile}[page](today, prog, assigned, missed)


main()
