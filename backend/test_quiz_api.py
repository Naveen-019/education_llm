"""HTTP contract check for the weak-topic quiz flow.

Run with:  python test_quiz_api.py

Exercises the endpoints the frontend calls and asserts on the exact field
names the React components read, so a backend change cannot silently break the
UI.
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Question, Attempt, TopicQuiz
from app import services as S
import app.database as database
import app.main as main

S._get_client = lambda: None  # stay offline

FAILURES = []


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


def main_test():
    # StaticPool keeps every thread on the same in-memory database.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Point the app at the test database.
    main.engine = engine
    database.engine = engine
    database.SessionLocal = TestingSession
    main.get_db = database.get_db
    import app.auth as auth
    auth.get_db = database.get_db

    client = TestClient(main.app)

    # Register a student.
    res = client.post("/api/auth/register", json={
        "name": "Api Student",
        "email": "api@test.com",
        "password": "secret123",
        "role": "student",
    })
    check("register succeeds", res.status_code == 200, res.text[:120])
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Seed weak attempts directly.
    db = TestingSession()
    uid = res.json()["user"]["id"]
    for topic, subject in (("Functions", "Programming"), ("SQL Joins", "DBMS"), ("Hive", "Big Data")):
        for i in range(4):
            q = Question(subject=subject, topic=topic, difficulty="medium",
                         question=f"{topic} q{i}", answer="a", options_json='["a","b"]')
            db.add(q)
            db.commit()
            db.refresh(q)
            db.add(Attempt(user_id=uid, question_id=q.id, user_answer="b", score=0))
            db.commit()
    db.close()

    # --- /api/learning-path contract ---
    res = client.get("/api/learning-path", headers=headers)
    check("learning-path 200", res.status_code == 200, res.text[:150])
    plan = res.json()
    for field in ("courses", "current", "pass_mark", "quiz_length", "required_correct",
                  "total_weak_topics", "cleared_topics", "remaining_topics", "overall_percent", "quiz"):
        check(f"learning-path has '{field}'", field in plan)
    check("current topic is Functions", plan["current"]["topic"] == "Functions", plan["current"])
    first = plan["courses"][0]
    for field in ("code", "name", "subject", "status", "topics", "cleared", "total", "progress_percent"):
        check(f"course has '{field}'", field in first)
    check("first course active", first["status"] == "active", first["status"])
    check("quiz payload has lock info", "locked" in plan["quiz"] and "limit" in plan["quiz"])

    # --- /api/dashboard contract ---
    res = client.get("/api/dashboard", headers=headers)
    check("dashboard 200", res.status_code == 200, res.text[:150])
    dash = res.json()
    for field in ("weak_topics", "current_topic", "learning_path", "topic_stats", "cleared_topics"):
        check(f"dashboard has '{field}'", field in dash)
    check("weak_topics entry shape", all(
        {"topic", "accuracy", "attempts", "course_code", "course_name", "status"} <= set(w)
        for w in dash["weak_topics"]
    ))
    check("exactly one current topic in queue", sum(
        1 for w in dash["weak_topics"] if w["status"] == "current"
    ) == 1)

    # --- /api/quiz/today ---
    res = client.get("/api/quiz/today", headers=headers)
    check("quiz/today 200", res.status_code == 200, res.text[:150])
    today = res.json()
    for field in ("current", "pass_mark", "quiz_length", "required_correct", "daily", "open_quiz"):
        check(f"quiz/today has '{field}'", field in today)
    check("no open quiz yet", today["open_quiz"] is None)
    check("daily limit is 1", today["daily"]["limit"] == 1)

    # A quiz must be refused for any course other than the active one. This
    # only reaches the check while no quiz is open, so it runs before start.
    res = client.post("/api/quiz/start", json={"course_code": "bigdata"}, headers=headers)
    check("cannot jump to another course", res.status_code == 409, res.status_code)
    check("no quiz row created by the rejected start",
          client.get("/api/quiz/today", headers=headers).json()["open_quiz"] is None)

    # --- start ---
    res = client.post("/api/quiz/start", json={}, headers=headers)
    check("quiz start 200", res.status_code == 200, res.text[:200])
    started = res.json()
    check("quiz not resumed on first start", started["resumed"] is False)
    check("10 questions returned", len(started["questions"]) == 10, len(started["questions"]))
    check("no answers leaked", all("answer" not in q for q in started["questions"]))
    check("all questions on the active topic", {q["id"] for q in started["questions"]} ==
          {q.id for q in S.quiz_questions(db, db.get(TopicQuiz, started["quiz"]["id"]))})
    quiz_id = started["quiz"]["id"]

    # start again -> resume, not a new quiz
    res = client.post("/api/quiz/start", json={}, headers=headers)
    check("second start resumes the same quiz", res.json()["resumed"] is True)
    check("resume returns the same quiz id", res.json()["quiz"]["id"] == quiz_id)

    # wrong course is rejected
    res = client.post("/api/quiz/start", json={"course_code": "bigdata"}, headers=headers)
    check("resume wins over the course check", res.status_code == 200 and res.json()["resumed"] is True)

    # --- submit all wrong ---
    questions = started["questions"]
    answers = [{"question_id": q["id"], "answer": "__nope__", "hints_used": 0} for q in questions]
    res = client.post("/api/quiz/submit", json={"quiz_id": quiz_id, "answers": answers}, headers=headers)
    check("submit 200", res.status_code == 200, res.text[:200])
    failed = res.json()
    for field in ("score", "correct", "total", "pass_mark", "passed", "topic", "review", "next_topic", "plan"):
        check(f"submit result has '{field}'", field in failed)
    check("failed below the pass mark", failed["passed"] is False, failed["score"])
    check("topic still current after failure", failed["next_topic"]["topic"] == "Functions")

    # --- daily lock after finishing ---
    res = client.post("/api/quiz/start", json={}, headers=headers)
    check("daily lock blocks a new quiz", res.status_code == 403, res.status_code)
    check("lock message mentions the limit", "limit" in res.json()["detail"].lower(),
          res.json()["detail"][:70])

    res = client.get("/api/quiz/today", headers=headers)
    check("today reports locked", res.json()["daily"]["locked"] is True)
    check("last_result recorded", res.json()["daily"]["last_result"]["topic"] == "Functions")

    # --- next day, pass ---
    db = TestingSession()
    quiz = db.get(TopicQuiz, quiz_id)
    quiz.quiz_date = date.today() - timedelta(days=1)
    db.commit()
    quiz2, _ = S.create_quiz(db, uid, main.COURSE_BY_CODE["programming"], "Functions", "medium", 10)
    db.close()

    res = client.get("/api/quiz/today", headers=headers)
    check("lock lifts tomorrow", res.json()["daily"]["locked"] is False)

    # answer correctly by reading the real answers from the db
    db = TestingSession()
    q2 = db.get(TopicQuiz, quiz2.id)
    real = S.quiz_questions(db, q2)
    answers = [{"question_id": r.id, "answer": r.answer, "hints_used": 0} for r in real]
    db.close()

    res = client.post("/api/quiz/submit", json={"quiz_id": quiz2.id, "answers": answers}, headers=headers)
    check("second submit 200", res.status_code == 200, res.text[:200])
    passed = res.json()
    check("passed at the pass mark", passed["passed"] is True, passed["score"])
    check("review exposes the correct answer", all("correct_answer" in r for r in passed["review"]))
    check("cleared topic promoted the next one", passed["next_topic"]["topic"] == "SQL Joins",
          passed["next_topic"])
    check("next topic moved to the dbms course", passed["next_topic"]["course_code"] == "dbms")

    # submitting twice is safe
    res = client.post("/api/quiz/submit", json={"quiz_id": quiz2.id, "answers": []}, headers=headers)
    check("re-submit is idempotent", res.status_code == 200 and res.json()["already_submitted"] is True)

    # --- final state ---
    res = client.get("/api/learning-path", headers=headers)
    plan = res.json()
    prog = [c for c in plan["courses"] if c["code"] == "programming"][0]
    check("cleared topic shows as completed", any(
        t["topic"] == "Functions" and t["status"] == "cleared" for t in prog["topics"]
    ))
    check("cleared count incremented", plan["cleared_topics"] == 1, plan["cleared_topics"])
    check("Functions no longer in the weak list", all(
        t["topic"] != "Functions" for c in plan["courses"] for t in c["topics"]
        if t["status"] in ("pending", "current")
    ))

    # --- sub-parts ---
    plan_topics = [t for c in plan["courses"] for t in c["topics"]]
    functions = next((t for t in plan_topics if t["topic"] == "Functions"), None)
    check("cleared topic lists its sub-parts", bool(functions and functions.get("subtopics")),
          str(functions and functions.get("subtopics"))[:60])
    check("sub-part names come from the curriculum",
          bool(functions) and {sp["part"] for sp in functions["subtopics"]} ==
          {"Parameters and Arguments", "Return Values", "Scope", "Lambda Functions"})

    res = client.get("/api/dashboard", headers=headers)
    dash = res.json()
    check("dashboard reports strong topics", isinstance(dash.get("strong_topics"), list))
    check("cleared topic appears in strong topics",
          "Functions" in [t["topic"] for t in dash["strong_topics"]],
          str([t["topic"] for t in dash["strong_topics"]]))
    check("dashboard has a cleared count", dash.get("cleared_count") == 1, dash.get("cleared_count"))
    check("cleared topic is not in weak detection",
          "Functions" not in [t["topic"] for t in dash["weak_topics"]],
          str([t["topic"] for t in dash["weak_topics"]]))

    # Questions generated for a topic are tagged with a sub-part.
    # Backdate today's quizzes so the daily lock does not block a fresh start.
    db = TestingSession()
    for q in db.query(TopicQuiz).filter_by(user_id=uid).all():
        q.quiz_date = date.today() - timedelta(days=1)
    db.commit()
    db.close()

    res = client.post("/api/quiz/start", json={}, headers=headers)
    body = res.json()
    tagged = [q for q in body["questions"] if q.get("subtopic")]
    check("quiz start returns subtopic per question", len(tagged) > 0,
          f"{len(tagged)}/{len(body['questions'])} tagged")
    check("start advertises the sub-parts covered",
          len(body["quiz"].get("subtopics_expected") or []) > 0,
          str(body["quiz"].get("subtopics_expected"))[:60])

    # --- teacher dashboard ---
    tres = client.post("/api/auth/register", json={
        "name": "Teach One", "email": "teach1@test.com",
        "password": "teachpass1", "role": "teacher",
    })
    th = {"Authorization": f"Bearer {tres.json()['token']}"}
    res = client.get("/api/teacher/students", headers=th)
    check("teacher list loads", res.status_code == 200)
    trow = next((s for s in res.json()["students"] if s["id"] == uid), None)
    check("teacher row is present", trow is not None)
    check("teacher sees the cleared topic separately", "Functions" in trow["cleared_topics"],
          str(trow["cleared_topics"]))
    check("teacher cleared topic is not in weak list",
          "Functions" not in trow["weak_topics"], str(trow["weak_topics"]))
    check("teacher sees path progress", trow["path_percent"] > 0, trow["path_percent"])
    check("teacher sees the cleared count", trow["cleared_count"] == 1, trow["cleared_count"])

    res = client.get(f"/api/teacher/students/{uid}", headers=th)
    detail = res.json()
    check("teacher detail loads", res.status_code == 200)
    check("detail marks the topic completed",
          any(t["topic"] == "Functions" and t["status"] == "completed" for t in detail["topics"]))
    check("detail completed list is separate from weak",
          any(t["topic"] == "Functions" for t in detail["completed_topics"])
          and all(t["topic"] != "Functions" for t in detail["weak_topics"]))
    check("detail exposes sub-parts per topic",
          any(t.get("subtopics") for t in detail["topics"]))
    check("recent attempts report a sub-part",
          all("subtopic" in a for a in detail["recent_attempts"]))

    # --- auth required ---
    res = client.get("/api/quiz/today")
    check("quiz requires auth", res.status_code in (401, 403), res.status_code)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("All API checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main_test())
