from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import User, Question, Attempt, Document, TopicQuiz
from .schemas import (
    RegisterIn, LoginIn, TutorIn, GenerateIn, SubmitIn,
    QuizStartIn, QuizAnswerIn, QuizSubmitIn,
)
from .auth import hash_password, verify_password, create_token, current_user
from .rag import (
    COURSE_SUBJECTS, SUBJECT_ALIASES, document_courses, retrieve, seed_documents,
)
from .services import (
    tutor, make_question, grade, HINT_MODES,
    create_quiz, quiz_questions, grade_quiz_answer,
)
from .docstore import library_status
from .curriculum import (
    COURSES, COURSE_BY_CODE, PASS_MARK, QUIZ_LENGTH, WEAK_THRESHOLD,
    required_correct,
)
from .progress import (
    build_plan, quiz_payload, quiz_state, quiz_subtopics,
    serialize_quiz, student_snapshot,
    topic_performance, cleared_topics, weak_topics, match_topic,
)


# --------------------------------------------------
# FastAPI Application
# --------------------------------------------------

app = FastAPI(
    title="LLM Personalized Education API",
    version="1.0.0"
)


# --------------------------------------------------
# CORS Configuration
# --------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# Startup
# --------------------------------------------------

def migrate(db: Session):
    """Bring an existing database up to the current schema.

    ``create_all`` only creates missing tables, so columns added after the
    first release have to be applied here.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if 'questions' in tables:
        columns = {c['name'] for c in inspector.get_columns('questions')}
        if 'explanation' not in columns:
            db.execute(text('ALTER TABLE questions ADD COLUMN explanation TEXT NULL'))
        if 'subtopic' not in columns:
            db.execute(text('ALTER TABLE questions ADD COLUMN subtopic VARCHAR(120) NULL'))
        if 'quiz_id' not in columns:
            db.execute(text('ALTER TABLE questions ADD COLUMN quiz_id INT NULL'))
        db.commit()

    if 'questions' in tables:
        indexes = {i['name'] for i in inspector.get_indexes('questions')}
        if 'ix_questions_quiz_id' not in indexes:
            try:
                db.execute(
                    text('CREATE INDEX ix_questions_quiz_id ON questions (quiz_id)')
                )
                db.commit()
            except Exception:
                db.rollback()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

    db = next(get_db())
    try:
        migrate(db)
        seed_documents(db)
    finally:
        db.close()


# --------------------------------------------------
# Root
# --------------------------------------------------

@app.get("/")
def root():
    return {
        "message": "LLM Education API is running"
    }


# --------------------------------------------------
# Authentication - Register
# --------------------------------------------------

@app.post("/api/auth/register")
def register(
    data: RegisterIn,
    db: Session = Depends(get_db)
):
    existing_user = db.query(User).filter_by(
        email=data.email
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )

    user = User(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "token": create_token(user.id),
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role
        }
    }


# --------------------------------------------------
# Authentication - Login
# --------------------------------------------------

@app.post("/api/auth/login")
def login(
    data: LoginIn,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter_by(
        email=data.email
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    if not verify_password(
        data.password,
        user.password_hash
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    return {
        "token": create_token(user.id),
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role
        }
    }


# --------------------------------------------------
# Current User
# --------------------------------------------------

@app.get("/api/me")
def me(
    user=Depends(current_user)
):
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role
    }


# --------------------------------------------------
# Available subjects
# --------------------------------------------------

@app.get("/api/subjects")
def subjects(
    db: Session = Depends(get_db)
):
    rows = db.query(Document.subject).distinct().all()
    stored = {r[0] for r in rows if r[0]}

    # The four courses always come first, because that is what the quiz and
    # learning path work in. Any extra tag found in the knowledge base is
    # appended so an uploaded note is still selectable in the tutor.
    subs = list(COURSE_SUBJECTS)
    for extra in sorted(stored):
        canonical = document_courses(extra)
        if canonical and canonical[0] in subs:
            continue
        if extra not in subs:
            subs.append(extra)
    return {
        "subjects": subs,
        # Hint-first works for every subject, so the modes are always both
        # available to the client.
        "modes": list(HINT_MODES),
    }


# --------------------------------------------------
# AI Tutor
# --------------------------------------------------

@app.post("/api/tutor")
def ask(
    data: TutorIn,
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    result = tutor(
        db,
        data.question,
        data.subject,
        data.mode
    )
    return result


@app.get("/api/knowledge-base")
def knowledge_base(
    user=Depends(current_user)
):
    """What the tutor currently reads from, for the UI to display."""
    return library_status()


# --------------------------------------------------
# Generate Question
# --------------------------------------------------

@app.post("/api/questions/generate")
def generate(
    data: GenerateIn,
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    question, explanation = make_question(
        db,
        data.subject,
        data.topic,
        data.difficulty
    )

    return {
        "id": question.id,
        "subject": question.subject,
        "topic": question.topic,
        "difficulty": question.difficulty,
        "question": question.question,
        "options": __import__("json").loads(
            question.options_json
        ),
        "explanation": explanation
    }


# --------------------------------------------------
# Submit Answer
# --------------------------------------------------

@app.post("/api/attempts")
def submit(
    data: SubmitIn,
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    question = db.get(
        Question,
        data.question_id
    )

    if not question:
        raise HTTPException(
            status_code=404,
            detail="Question not found"
        )

    score, feedback = grade(
        db,
        user.id,
        question,
        data.answer,
        data.hints_used
    )

    return {
        "score": score,
        "feedback": feedback,
        "correct_answer": question.answer
    }


# --------------------------------------------------
# Dashboard
# --------------------------------------------------

def student_progress(db: Session, student_id: int) -> dict:
    attempts = db.query(Attempt).filter_by(
        user_id=student_id
    ).all()

    total = len(attempts)

    avg = (
        round(
            sum(a.score for a in attempts) / total,
            1
        )
        if total
        else 0
    )

    correct = sum(1 for a in attempts if a.score == 100)

    plan = build_plan(db, student_id)
    perf = topic_performance(db, student_id)
    cleared = cleared_topics(db, student_id)

    topic_stats = []
    snapshot = student_snapshot(db, student_id)
    for entry in snapshot["topics"]:
        topic_stats.append({
            "topic": entry["topic"],
            "accuracy": entry["accuracy"],
            "attempts": entry["attempts"],
            "cleared": entry["cleared"],
            "status": entry["status"],
            "subtopics": entry["subtopics"],
        })

    # Remaining weak topics in learning order: course by course, and inside a
    # course one topic at a time.
    ordered_weak = []
    for course in plan["courses"]:
        for topic in course["topics"]:
            if topic["status"] in ("pending", "current"):
                ordered_weak.append({
                    "topic": topic["topic"],
                    "accuracy": topic["accuracy"],
                    "attempts": topic["attempts"],
                    "course_code": course["code"],
                    "course_name": course["name"],
                    "status": topic["status"],
                    "subtopics": topic.get("subtopics", []),
                })

    last_active = (
        max(a.created_at for a in attempts)
        if attempts
        else None
    )

    return {
        "overall_score": avg,
        "attempted": total,
        "correct": correct,
        "topic_stats": topic_stats,
        "weak_topics": ordered_weak,
        "strong_topics": plan["strong_topics"],
        "cleared_count": snapshot["cleared_count"],
        "strong_count": snapshot["strong_count"],
        "weak_count": snapshot["weak_count"],
        "current_topic": plan["current"],
        "cleared_topics": plan["cleared_topics"],
        "learning_path": plan,
        "last_active": last_active,
        "recommendations": [
            (
                f'Start the {QUIZ_LENGTH}-question quiz for '
                f'"{plan["current"]["topic"]}" and score {PASS_MARK}% to clear it.'
            )
            if plan["current"] and index == 0
            else (
                f'Revise {topic["topic"]} and practise again.'
                if topic["status"] != "current"
                else f'Finish the quiz for "{topic["topic"]}" to move on.'
            )
            for index, topic in enumerate(ordered_weak[:3])
        ]
    }


def student_status(progress: dict) -> str:
    if progress["attempted"] == 0:
        return "No activity"
    score = progress["overall_score"]
    if score >= 80:
        return "Excellent"
    if score >= 60:
        return "On track"
    if score >= 40:
        return "Needs support"
    return "At risk"


@app.get("/api/dashboard")
def dashboard(
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    return student_progress(db, user.id)


# --------------------------------------------------
# Teacher - Classroom Dashboard
# --------------------------------------------------

@app.get("/api/teacher/students")
def teacher_students(
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    if user.role != "teacher":
        raise HTTPException(
            status_code=403,
            detail="Teacher role required"
        )

    students = db.query(User).filter_by(
        role="student"
    ).order_by(User.created_at).all()

    items = []
    total_attempts = 0
    total_correct = 0
    scored = []

    for student in students:
        progress = student_progress(db, student.id)
        status = student_status(progress)

        total_attempts += progress["attempted"]
        total_correct += progress["correct"]
        if progress["attempted"]:
            scored.append(progress["overall_score"])

        items.append({
            "id": student.id,
            "name": student.name,
            "email": student.email,
            "joined": student.created_at,
            "overall_score": progress["overall_score"],
            "attempted": progress["attempted"],
            "correct": progress["correct"],
            "weak_topics": [
                t["topic"] for t in progress["weak_topics"]
            ],
            # Cleared topics are listed separately so the teacher can see what
            # the student has finished; a cleared topic is never in weak_topics.
            "cleared_topics": sorted(
                t["topic"] for t in progress["learning_path"]["courses"]
                for t in t["topics"] if t["status"] == "cleared"
            ),
            "strong_topics": [
                t["topic"] for t in progress["strong_topics"]
            ],
            "current_topic": (
                progress["current_topic"]["topic"]
                if progress["current_topic"] else None
            ),
            "cleared_count": progress["cleared_count"],
            "weak_count": progress["weak_count"],
            "strong_count": progress["strong_count"],
            "path_percent": progress["learning_path"]["overall_percent"],
            "last_active": progress["last_active"],
            "status": status
        })

    active = sum(1 for i in items if i["attempted"] > 0)

    return {
        "summary": {
            "total_students": len(items),
            "active_students": active,
            "class_average": (
                round(sum(scored) / len(scored), 1)
                if scored
                else 0
            ),
            "correct_rate": (
                round(total_correct / total_attempts * 100, 1)
                if total_attempts
                else 0
            ),
            "at_risk": sum(
                1 for i in items
                if i["status"] in ("At risk", "Needs support")
            )
        },
        "students": items
    }


@app.get("/api/teacher/students/{student_id}")
def teacher_student_detail(
    student_id: int,
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    if user.role != "teacher":
        raise HTTPException(
            status_code=403,
            detail="Teacher role required"
        )

    student = db.get(User, student_id)
    if not student or student.role != "student":
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    progress = student_progress(db, student.id)

    attempts = db.query(Attempt).filter_by(
        user_id=student.id
    ).order_by(Attempt.created_at.desc()).limit(10).all()

    recent = []
    for attempt in attempts:
        question = db.get(Question, attempt.question_id)
        recent.append({
            "topic": question.topic if question else "Unknown",
            "subtopic": question.subtopic if question else None,
            "subject": question.subject if question else "Unknown",
            "difficulty": question.difficulty if question else "Unknown",
            "score": attempt.score,
            "correct": attempt.score == 100,
            "hints_used": attempt.hints_used,
            "date": attempt.created_at
        })

    # Every topic the student has touched, with a status the UI can colour:
    # completed (green), strong, or still weak. A cleared topic is completed.
    topics = progress["learning_path"]["courses"]
    topic_rows = [
        {
            "topic": t["topic"],
            "course_code": course["code"],
            "course_name": course["name"],
            "accuracy": t["accuracy"],
            "best_score": t["best_score"],
            "status": "completed" if t["status"] == "cleared" else (
                "weak" if t["status"] in ("pending", "current") else t["status"]
            ),
            "subtopics": t.get("subtopics", []),
        }
        for course in topics
        for t in course["topics"]
    ]
    for entry in topic_rows:
        if entry["status"] == "weak" and entry["accuracy"] >= WEAK_THRESHOLD:
            entry["status"] = "strong"

    return {
        "id": student.id,
        "name": student.name,
        "email": student.email,
        "joined": student.created_at,
        "progress": progress,
        "topics": topic_rows,
        "completed_topics": [
            t for t in topic_rows if t["status"] == "completed"
        ],
        "weak_topics": [
            t for t in topic_rows if t["status"] == "weak"
        ],
        "status": student_status(progress),
        "recent_attempts": recent
    }


# --------------------------------------------------
# Learning Path - courses one by one, topics one by one
# --------------------------------------------------

@app.get("/api/learning-path")
def learning_path(
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    return build_plan(db, user.id)


# --------------------------------------------------
# Weak-topic quiz
#
# One active weak topic at a time, one quiz per student per day.
# Clearing a topic (>= PASS_MARK) removes it from the weak list and promotes
# the next weak topic in curriculum order.
# --------------------------------------------------

def public_questions(db: Session, quiz: TopicQuiz):
    """Quiz questions without the answers, plus per-question progress."""
    rows = quiz_questions(db, quiz)
    ids = [q.id for q in rows]

    scores = {}
    if ids:
        answered = db.query(Attempt).filter(
            Attempt.user_id == quiz.user_id,
            Attempt.question_id.in_(ids)
        ).all()
        for attempt in answered:
            scores[attempt.question_id] = int(attempt.score)

    return [
        {
            "id": q.id,
            "question": q.question,
            "subtopic": q.subtopic,
            "options": __import__("json").loads(q.options_json or "[]"),
            "difficulty": q.difficulty,
            "answered": q.id in scores,
            "score": scores.get(q.id),
        }
        for q in rows
    ]


def get_own_quiz(db: Session, user, quiz_id: int) -> TopicQuiz:
    quiz = db.get(TopicQuiz, quiz_id)
    if not quiz or quiz.user_id != user.id:
        raise HTTPException(
            status_code=404,
            detail="Quiz not found"
        )
    return quiz


@app.get("/api/quiz/today")
def quiz_today(
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    """Daily lock state plus the weak topic currently being worked on."""
    plan = build_plan(db, user.id)
    state = quiz_state(db, user.id)

    return {
        "current": plan["current"],
        "pass_mark": PASS_MARK,
        "quiz_length": QUIZ_LENGTH,
        "required_correct": required_correct(),
        "daily": quiz_payload(db, user.id),
        "open_quiz": (
            {
                **serialize_quiz(state["open_quiz"], db),
                "questions": public_questions(db, state["open_quiz"]),
            }
            if state["open_quiz"]
            else None
        ),
        "all_courses_completed": plan["all_courses_completed"],
    }


@app.post("/api/quiz/start")
def quiz_start(
    data: QuizStartIn,
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    state = quiz_state(db, user.id)

    # An unfinished quiz from earlier today is resumed, not duplicated.
    if state["open_quiz"]:
        quiz = state["open_quiz"]
        return {
            "resumed": True,
            "quiz": serialize_quiz(quiz, db),
            "questions": public_questions(db, quiz),
            "plan": build_plan(db, user.id),
        }

    if state["locked"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f'Daily quiz limit reached. You have already taken today\'s '
                f'weak-topic quiz. The next one unlocks on '
                f'{state["unlocks_at"]}.'
            )
        )

    plan = build_plan(db, user.id)
    current = plan["current"]

    if not current:
        raise HTTPException(
            status_code=400,
            detail=(
                'No weak topic is waiting right now. Practise a few '
                'questions in Adaptive Practice so your weak topics can be '
                'detected.'
            )
        )

    # A quiz is only ever offered for the active course, one course at a time.
    if data.course_code and data.course_code != current["course_code"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f'Finish "{current["course_name"]}" first. Quizzes unlock '
                f'one course at a time.'
            )
        )

    course = COURSE_BY_CODE[current["course_code"]]
    difficulty = "easy" if current["accuracy"] < 50 else "medium"

    quiz, offline = create_quiz(
        db,
        user.id,
        course,
        current["topic"],
        difficulty,
        QUIZ_LENGTH
    )

    return {
        "resumed": False,
        "offline": offline,
        "quiz": serialize_quiz(quiz, db),
        "questions": public_questions(db, quiz),
        "plan": build_plan(db, user.id),
    }


@app.post("/api/quiz/answer")
def quiz_answer(
    data: QuizAnswerIn,
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    quiz = get_own_quiz(db, user, data.quiz_id)

    if quiz.status != "in_progress":
        raise HTTPException(
            status_code=409,
            detail="This quiz has already been submitted"
        )

    question = db.get(Question, data.question_id)

    if not question or question.quiz_id != quiz.id:
        raise HTTPException(
            status_code=400,
            detail="Question does not belong to this quiz"
        )

    score, feedback, duplicate = grade_quiz_answer(
        db,
        quiz,
        question,
        data.answer,
        data.hints_used
    )

    return {
        "score": score,
        "correct": score == 100,
        "feedback": feedback,
        "already_answered": duplicate,
        "answered": quiz.answered,
        "correct_count": quiz.correct,
        "total": quiz.total,
        "remaining": max(0, (quiz.total or 0) - (quiz.answered or 0)),
    }


@app.post("/api/quiz/submit")
def quiz_submit(
    data: QuizSubmitIn,
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    quiz = get_own_quiz(db, user, data.quiz_id)

    if quiz.status != "in_progress":
        # Submitting twice is harmless; return the same verdict again.
        total = quiz.total or 0
        score = round((quiz.correct or 0) / total * 100, 1) if total else 0
        plan = build_plan(db, user.id)
        return {
            "score": score,
            "correct": quiz.correct,
            "total": total,
            "pass_mark": PASS_MARK,
            "passed": quiz.status == "passed",
            "already_submitted": True,
            "topic": quiz.topic,
            "course_name": quiz.course_name,
            "subtopics": quiz_subtopics(db, quiz),
            "review": quiz_review(db, quiz),
            "plan": plan,
        }

    total = quiz.total or 0
    correct = quiz.correct or 0
    score = round(correct / total * 100, 1) if total else 0

    # Grade everything the student selected in one go, so the whole quiz is
    # scored atomically and no per-question feedback leaks before the end.
    if data.answers:
        for entry in data.answers:
            question = db.get(Question, entry.question_id)
            if not question or question.quiz_id != quiz.id:
                continue
            grade_quiz_answer(
                db,
                quiz,
                question,
                entry.answer,
                entry.hints_used
            )
        db.refresh(quiz)
        correct = quiz.correct or 0
        score = round(correct / total * 100, 1) if total else 0

    passed = score >= PASS_MARK

    quiz.score = score
    quiz.status = "passed" if passed else "failed"
    quiz.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(quiz)

    plan = build_plan(db, user.id)

    return {
        "score": score,
        "correct": correct,
        "total": total,
        "pass_mark": PASS_MARK,
        "required_correct": required_correct(total),
        "passed": passed,
        "already_submitted": False,
        "topic": quiz.topic,
        "course_name": quiz.course_name,
        "subtopics": quiz_subtopics(db, quiz),
        "review": quiz_review(db, quiz),
        "next_topic": plan["current"],
        "strong_topics": plan["strong_topics"],
        "plan": plan,
    }


def quiz_review(db: Session, quiz: TopicQuiz):
    """Answers, explanations and explanations of what to revise."""
    review = []
    for q in quiz_questions(db, quiz):
        attempt = (db.query(Attempt)
                   .filter_by(user_id=quiz.user_id, question_id=q.id)
                   .order_by(Attempt.id.desc())
                   .first())
        review.append({
            "question_id": q.id,
            "question": q.question,
            "subtopic": q.subtopic,
            "correct_answer": q.answer,
            "your_answer": attempt.user_answer if attempt else None,
            "correct": bool(attempt and attempt.score == 100),
            "explanation": q.explanation or ""
        })
    return review


@app.get("/api/quiz/history")
def quiz_history(
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    from .progress import quiz_history as _history
    return {
        "items": [
            serialize_quiz(q, db) for q in _history(db, user.id)
        ]
    }


# --------------------------------------------------
# Teacher Reviews
# --------------------------------------------------

@app.get("/api/teacher/reviews")
def reviews(
    db: Session = Depends(get_db),
    user=Depends(current_user)
):
    if user.role != "teacher":
        raise HTTPException(
            status_code=403,
            detail="Teacher role required"
        )

    return {
        "items": []
    }
