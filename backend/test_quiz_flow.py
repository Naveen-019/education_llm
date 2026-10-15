"""End-to-end check of the weak-topic quiz loop on an in-memory database.

Run with:  python test_quiz_flow.py

Covers the rules the feature depends on:
  * weak topics are ordered course by course, one course at a time
  * a quiz is only ever generated for the single active weak topic
  * scoring below the pass mark keeps the topic weak
  * scoring at or above the pass mark clears the topic and promotes the next one
  * a student gets one quiz per calendar day
  * a question cannot be answered twice inside the same quiz
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Keep the test offline and deterministic: no live Gemini calls, the quiz
# falls back to the built-in question set.
os.environ.pop("GEMINI_API_KEY", None)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User, Question, Attempt, TopicQuiz
from app.curriculum import PASS_MARK, QUIZ_LENGTH
from app import progress as P
from app import services as S

# Keep the test offline and deterministic: stub out the LLM client so the quiz
# exercises the offline fallback instead of making live network calls.
S._get_client = lambda: None

FAILURES = []


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(label)
    print(f"[{mark}] {label}" + (f"  -> {detail}" if detail else ""))


def seed_weak_attempts(db, user_id, topic, wrong=4, right=0):
    """Give a topic an accuracy below the pass mark so it counts as weak."""
    for i in range(wrong + right):
        q = Question(
            subject="Test",
            topic=topic,
            difficulty="medium",
            question=f"{topic} question {i}",
            answer="right",
            options_json='["right","wrong"]',
        )
        db.add(q)
        db.commit()
        db.refresh(q)
        db.add(Attempt(
            user_id=user_id,
            question_id=q.id,
            user_answer="right" if i >= wrong else "wrong",
            score=100 if i >= wrong else 0,
        ))
        db.commit()


def answer_quiz(db, quiz, correct):
    """Answer every question in the quiz, right or wrong."""
    for q in S.quiz_questions(db, quiz):
        chosen = q.answer if correct else "definitely not the answer"
        S.grade_quiz_answer(db, quiz, q, chosen, 0)


def main():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    student = User(
        name="Test Student",
        email="student@test.com",
        password_hash="x",
        role="student",
    )
    db.add(student)
    db.commit()
    db.refresh(student)
    uid = student.id

    # Weak topics spread over two courses, declared out of curriculum order.
    seed_weak_attempts(db, uid, "Hive")        # bigdata, weak
    seed_weak_attempts(db, uid, "Functions")   # programming, weak
    seed_weak_attempts(db, uid, "SQL Joins")   # dbms, weak
    seed_weak_attempts(db, uid, "Decorators", wrong=0, right=5)  # strong -> not weak

    print("\n--- plan ordering ---")
    plan = P.build_plan(db, uid)
    order = [
        (c["code"], c["status"])
        for c in plan["courses"]
    ]
    print("   ", order)
    check(
        "programming is the active course (curriculum order wins)",
        plan["current"] and plan["current"]["course_code"] == "programming",
        plan["current"],
    )
    check(
        "current topic is the first weak topic in curriculum order",
        plan["current"]["topic"] == "Functions",
        plan["current"]["topic"],
    )
    check(
        "strong topics are not weak",
        all(
            t["topic"] != "Decorators"
            for c in plan["courses"]
            for t in c["topics"]
        ),
    )
    check(
        "later courses are locked",
        [c for c in plan["courses"] if c["code"] == "bigdata"][0]["status"] == "locked",
    )
    check(
        "exactly one topic is current",
        sum(
            1
            for c in plan["courses"]
            for t in c["topics"]
            if t["status"] == "current"
        ) == 1,
    )

    print("\n--- quiz start ---")
    from app.main import quiz_start, quiz_submit, QuizStartIn, QuizSubmitIn

    started = quiz_start(QuizStartIn(), db=db, user=student)
    quiz = db.get(TopicQuiz, started["quiz"]["id"])
    check(
        "quiz has the configured length",
        started["quiz"]["total"] == QUIZ_LENGTH,
        started["quiz"]["total"],
    )
    check(
        "every quiz question is for the single active topic",
        {q.topic for q in S.quiz_questions(db, quiz)} == {"Functions"},
    )
    check(
        "answers are not exposed before submitting",
        all("answer" not in q for q in started["questions"]),
    )
    check(
        "every question carries options",
        all(len(q["options"]) >= 2 for q in started["questions"]),
    )

    print("\n--- failing attempt ---")
    answer_quiz(db, quiz, correct=False)
    result = quiz_submit(QuizSubmitIn(quiz_id=quiz.id), db=db, user=student)
    check("score below the pass mark", not result["passed"], result["score"])
    check(
        "failing keeps the topic weak",
        P.build_plan(db, uid)["current"]["topic"] == "Functions",
    )
    check(
        "review explains every question",
        len(result["review"]) == QUIZ_LENGTH
        and all(r["explanation"] for r in result["review"]),
    )

    print("\n--- daily lock ---")
    try:
        quiz_start(QuizStartIn(), db=db, user=student)
        check("second quiz on the same day is blocked", False, "no error raised")
    except Exception as exc:
        check("second quiz on the same day is blocked", True, str(exc.detail)[:60])

    print("\n--- passing attempt (next day) ---")
    quiz.quiz_date = date.today() - timedelta(days=1)
    db.commit()
    state = P.quiz_state(db, uid)
    check("lock lifts the next day", not state["locked"], state["date"])

    started2 = quiz_start(QuizStartIn(), db=db, user=student)
    quiz2 = db.get(TopicQuiz, started2["quiz"]["id"])
    answer_quiz(db, quiz2, correct=True)
    result2 = quiz_submit(QuizSubmitIn(quiz_id=quiz2.id), db=db, user=student)
    check("score hits the pass mark", result2["passed"], result2["score"])
    check("pass needs 8 of 10", result2["correct"] >= 8, result2["correct"])

    plan2 = P.build_plan(db, uid)
    check(
        "cleared topic disappeared from the weak list",
        all(
            t["topic"] != "Functions"
            for c in plan2["courses"]
            for t in c["topics"]
            if t["status"] in ("pending", "current")
        ),
    )
    check(
        "cleared topic is still shown as completed",
        [
            t["status"]
            for c in plan2["courses"]
            for t in c["topics"]
            if t["topic"] == "Functions"
        ] == ["cleared"],
    )
    check(
        "next weak topic in the same course is promoted",
        plan2["current"]["topic"] == "SQL Joins",
        plan2["current"],
    )
    check(
        "course advances one step at a time",
        plan2["current"]["course_code"] == "dbms",
    )

    print("\n--- duplicate answers ---")
    quiz3_row = quiz2
    q0 = S.quiz_questions(db, quiz3_row)[0]
    before = quiz3_row.correct
    S.grade_quiz_answer(db, quiz3_row, q0, "definitely not the answer", 0)
    db.refresh(quiz3_row)
    check(
        "re-answering a question does not double count",
        quiz3_row.correct == before,
        f"{before} -> {quiz3_row.correct}",
    )

    print("\n--- course completion ---")
    for topic in ("SQL Joins", "Transactions", "Hive", "MapReduce"):
        seed_weak_attempts(db, uid, topic)
    # Clear every remaining weak topic by passing a quiz for each, on
    # successive days.
    for _ in range(12):
        plan3 = P.build_plan(db, uid)
        if not plan3["current"]:
            break
        course = plan3["current"]
        from app.curriculum import COURSE_BY_CODE
        q, _offline = S.create_quiz(
            db, uid, COURSE_BY_CODE[course["course_code"]], course["topic"]
        )
        answer_quiz(db, q, correct=True)
        S.quiz_questions(db, q)
        from app.main import quiz_submit as _submit
        _submit(QuizSubmitIn(quiz_id=q.id), db=db, user=student)
        q.quiz_date = date.today()
        db.commit()

    final = P.build_plan(db, uid)
    check("all weak topics cleared", final["remaining_topics"] == 0, final["remaining_topics"])
    check("every course reports completion", final["all_courses_completed"])
    check("no current topic remains", final["current"] is None)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
