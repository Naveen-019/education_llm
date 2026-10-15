"""Weak-topic detection, the sequential course plan, and the daily quiz lock.

The rules implemented here:

* A topic is *weak* when its quiz accuracy is below ``WEAK_THRESHOLD``.
* Weak topics are grouped into courses and ordered by curriculum order, so a
  student always works through courses one by one.
* Inside a course only the first uncleared weak topic is *active* — a quiz is
  only ever offered for that one topic.
* Clearing a topic (pass mark reached) removes it from the weak list and the
  next weak topic becomes active.
* A student may start only one quiz per calendar day.
"""

from datetime import date, timedelta

from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from .curriculum import (
    COURSES,
    DAILY_QUIZ_LIMIT,
    PASS_MARK,
    QUIZ_LENGTH,
    WEAK_THRESHOLD,
    course_topic_position,
    match_topic,
    required_correct,
    subtopics_of,
)
from .models import Attempt, Question, TopicQuiz


def topic_performance(db: Session, user_id: int):
    """Average score and attempt count per raw topic string."""
    rows = (
        db.query(Question.topic, func.avg(Attempt.score), func.count(Attempt.id))
        .join(Attempt, Attempt.question_id == Question.id)
        .filter(Attempt.user_id == user_id)
        .group_by(Question.topic)
        .all()
    )
    return {
        topic: {
            'accuracy': round(float(avg or 0), 1),
            'attempts': int(count or 0),
        }
        for topic, avg, count in rows
    }


def cleared_topics(db: Session, user_id: int):
    """Canonical topics this student has already passed a quiz on."""
    rows = (
        db.query(TopicQuiz.topic)
        .filter(TopicQuiz.user_id == user_id, TopicQuiz.status == 'passed')
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def topic_best_scores(db: Session, user_id: int):
    rows = (
        db.query(TopicQuiz.topic, func.max(TopicQuiz.score))
        .filter(TopicQuiz.user_id == user_id)
        .group_by(TopicQuiz.topic)
        .all()
    )
    return {topic: round(float(score or 0), 1) for topic, score in rows}


def subtopic_performance(db: Session, user_id: int):
    """Average score per (topic, sub-part) pair.

    ``None`` subtopics (questions from before sub-parts existed, or topics with
    no defined sub-parts) are grouped under the empty string and skipped by
    callers.
    """
    rows = (
        db.query(
            Question.topic,
            Question.subtopic,
            func.avg(Attempt.score),
            func.count(Attempt.id),
        )
        .join(Attempt, Attempt.question_id == Question.id)
        .filter(Attempt.user_id == user_id, Question.subtopic.isnot(None))
        .group_by(Question.topic, Question.subtopic)
        .all()
    )
    result = {}
    for topic, subtopic, avg, count in rows:
        if not subtopic:
            continue
        matched = match_topic(topic) or (None, topic)
        canonical = matched[1]
        result.setdefault(canonical, {})[subtopic] = {
            'accuracy': round(float(avg or 0), 1),
            'attempts': int(count or 0),
        }
    return result


def strong_topics(db: Session, user_id: int):
    """Topics this student is now strong in (accuracy at or above the pass mark).

    This is the complement of :func:`weak_topics`: a topic moves here as soon as
    it reaches the threshold, and a cleared topic always counts as strong.
    """
    perf = topic_performance(db, user_id)
    cleared = cleared_topics(db, user_id)
    best = topic_best_scores(db, user_id)

    grouped = {}
    for raw, stats in perf.items():
        if not stats['attempts']:
            continue
        matched = match_topic(raw)
        if not matched:
            continue
        code, topic = matched
        # A cleared topic is strong even if older attempts averaged lower.
        accuracy = PASS_MARK if topic in cleared else stats['accuracy']
        if accuracy < WEAK_THRESHOLD:
            continue
        entry = grouped.get(topic)
        if entry is None or accuracy > entry['accuracy']:
            grouped[topic] = {
                'topic': topic,
                'course_code': code,
                'accuracy': round(accuracy, 1),
                'attempts': stats['attempts'],
                'best_score': best.get(topic),
                'cleared': topic in cleared,
            }

    topics = list(grouped.values())
    for entry in topics:
        entry['position'] = course_topic_position(entry['course_code'], entry['topic'])
    topics.sort(key=lambda t: t['position'])
    return topics


def _subtopic_detail(topic, per_topic):
    """Attach each of ``topic``'s sub-parts with its own score and status."""
    parts = []
    scores = per_topic.get(topic, {})
    for part in subtopics_of(topic):
        stats = scores.get(part)
        if stats is None:
            parts.append({'part': part, 'accuracy': None, 'attempts': 0, 'status': 'untested'})
        else:
            parts.append({
                'part': part,
                'accuracy': stats['accuracy'],
                'attempts': stats['attempts'],
                'status': 'strong' if stats['accuracy'] >= WEAK_THRESHOLD else 'weak',
            })
    return parts


def weak_topics(db: Session, user_id: int, include_cleared: bool = False):
    """Detected weak topics, weakest first.

    Cleared topics are excluded by default so a passed topic never reappears in
    weak-topic detection.
    """
    perf = topic_performance(db, user_id)
    cleared = cleared_topics(db, user_id)
    best = topic_best_scores(db, user_id)
    per_topic = subtopic_performance(db, user_id)

    grouped = {}
    for raw, stats in perf.items():
        if not stats['attempts']:
            continue
        if stats['accuracy'] >= WEAK_THRESHOLD:
            continue

        matched = match_topic(raw)
        if not matched:
            continue
        code, topic = matched
        if topic in cleared and not include_cleared:
            continue

        entry = grouped.get(topic)
        if entry is None:
            grouped[topic] = {
                'topic': topic,
                'course_code': code,
                'accuracy': stats['accuracy'],
                'attempts': stats['attempts'],
                'best_score': best.get(topic),
            }
        else:
            # Several raw topic strings can collapse onto one curriculum
            # topic; keep the weakest result.
            if stats['accuracy'] < entry['accuracy']:
                entry['accuracy'] = stats['accuracy']
                entry['attempts'] = stats['attempts']

    topics = list(grouped.values())
    for entry in topics:
        entry['position'] = course_topic_position(entry['course_code'], entry['topic'])
        entry['subtopics'] = _subtopic_detail(entry['topic'], per_topic)
    topics.sort(key=lambda t: (t['accuracy'], t['position']))
    return topics


def course_weak_map(db: Session, user_id: int, include_cleared: bool = False):
    """Weak topics bucketed per course, each bucket in curriculum order."""
    buckets = {course['code']: [] for course in COURSES}
    for entry in weak_topics(db, user_id, include_cleared=include_cleared):
        buckets.setdefault(entry['course_code'], []).append(entry)
    for entries in buckets.values():
        entries.sort(key=lambda e: e['position'])
    return buckets


def quiz_history(db: Session, user_id: int, limit: int = 20):
    return (
        db.query(TopicQuiz)
        .filter(TopicQuiz.user_id == user_id)
        .order_by(TopicQuiz.id.desc())
        .limit(limit)
        .all()
    )


def quiz_state(db: Session, user_id: int):
    """Daily-lock state for the current calendar day."""
    today = date.today()
    todays = (
        db.query(TopicQuiz)
        .filter(TopicQuiz.user_id == user_id, TopicQuiz.quiz_date == today)
        .order_by(TopicQuiz.id.desc())
        .all()
    )

    open_quiz = next((q for q in todays if q.status == 'in_progress'), None)
    finished = [q for q in todays if q.status != 'in_progress']

    return {
        'date': today.isoformat(),
        'limit': DAILY_QUIZ_LIMIT,
        'attempts_today': len(finished) + (1 if open_quiz else 0),
        'quizzes_today': todays,
        'open_quiz': open_quiz,
        'last_result': finished[0] if finished else None,
        'resumable': open_quiz is not None,
        'locked': bool(todays) and open_quiz is None,
        'unlocks_at': (today + timedelta(days=1)).isoformat(),
    }


def quiz_subtopics(db: Session, quiz: TopicQuiz):
    """Per-sub-part breakdown of one finished quiz.

    Returns one row per sub-part the quiz tested, each with its own score, so a
    result can show which parts of the topic are strong and which still need
    work.
    """
    rows = (
        db.query(
            Question.subtopic,
            func.count(Question.id),
            # `Attempt.score` is stored as 0 or 100, so it must be counted with
            # a case expression. Summing it directly would report 300 correct
            # for three right answers.
            func.coalesce(
                func.sum(case((Attempt.score == 100, 1), else_=0)),
                0,
            ),
            func.count(Attempt.id),
        )
        .outerjoin(Attempt, and_(
            Attempt.question_id == Question.id,
            Attempt.user_id == quiz.user_id,
        ))
        .filter(Question.quiz_id == quiz.id)
        .group_by(Question.subtopic)
        .all()
    )

    breakdown = []
    for subtopic, asked, correct_count, graded in rows:
        if not subtopic:
            continue
        asked = int(asked or 0)
        correct = int(correct_count or 0)
        accuracy = round(correct / asked * 100, 1) if asked else 0.0
        breakdown.append({
            'part': subtopic,
            'asked': asked,
            'answered': int(graded or 0),
            'correct': correct,
            'accuracy': accuracy,
            # A part is only strong when it was actually cleared on its own
            # terms: a single question cannot be 80% correct.
            'status': 'strong' if asked and correct >= max(1, asked * PASS_MARK / 100) else 'weak',
        })
    breakdown.sort(key=lambda b: b['part'].lower())
    return breakdown


def serialize_quiz(quiz: TopicQuiz, db: Session = None):
    if quiz is None:
        return None
    payload = {
        'id': quiz.id,
        'course_code': quiz.course_code,
        'course_name': quiz.course_name,
        'subject': quiz.subject,
        'topic': quiz.topic,
        'total': quiz.total,
        'correct': quiz.correct,
        'answered': quiz.answered,
        'score': quiz.score,
        'status': quiz.status,
        'passed': quiz.status == 'passed',
        'date': quiz.quiz_date.isoformat() if quiz.quiz_date else None,
        'created_at': quiz.created_at.isoformat() if quiz.created_at else None,
        'completed_at': quiz.completed_at.isoformat() if quiz.completed_at else None,
    }
    # The sub-parts this quiz is meant to cover are known as soon as it starts,
    # so the student can see the breakdown while answering.
    payload['subtopics_expected'] = subtopics_of(quiz.topic)
    if db is not None and quiz.status != 'in_progress':
        # The scored per-part breakdown only exists once the quiz is graded.
        payload['subtopics'] = quiz_subtopics(db, quiz)
    return payload


def quiz_payload(db: Session, user_id: int):
    state = quiz_state(db, user_id)
    return {
        'date': state['date'],
        'limit': state['limit'],
        'attempts_today': state['attempts_today'],
        'locked': state['locked'],
        'resumable': state['resumable'],
        'unlocks_at': state['unlocks_at'],
        'open_quiz': serialize_quiz(state['open_quiz'], db),
        'last_result': serialize_quiz(state['last_result'], db),
    }


def build_plan(db: Session, user_id: int):
    """The one-by-one course structure for a student.

    Every course is returned in curriculum order. Courses before the active one
    are completed, the active course holds exactly one current topic, and later
    courses stay locked until the earlier ones are cleared.
    """
    buckets = course_weak_map(db, user_id, include_cleared=True)
    cleared = cleared_topics(db, user_id)
    best = topic_best_scores(db, user_id)
    per_topic = subtopic_performance(db, user_id)

    active_code = None
    for course in COURSES:
        pending = [
            e for e in buckets.get(course['code'], [])
            if e['topic'] not in cleared
        ]
        if pending:
            active_code = course['code']
            break

    courses = []
    current = None
    total_topics = 0
    cleared_count = 0

    for course in COURSES:
        topics = []
        seen = set()
        for entry in buckets.get(course['code'], []):
            if entry['topic'] in seen:
                continue
            seen.add(entry['topic'])
            topics.append({
                'topic': entry['topic'],
                'accuracy': entry['accuracy'],
                'attempts': entry['attempts'],
                'position': entry['position'],
                'status': 'cleared' if entry['topic'] in cleared else 'pending',
                'best_score': best.get(entry['topic']),
                'subtopics': _subtopic_detail(entry['topic'], per_topic),
            })
        topics.sort(key=lambda t: t['position'])

        pending_topics = [t for t in topics if t['status'] == 'pending']
        done = len(topics) - len(pending_topics)

        if not topics:
            status = 'clear'
        elif not pending_topics:
            status = 'completed'
        elif course['code'] == active_code:
            status = 'active'
        else:
            status = 'locked'

        if status == 'active' and current is None:
            for topic in topics:
                if topic['status'] == 'pending':
                    topic['status'] = 'current'
                    current = {
                        'course_code': course['code'],
                        'course_name': course['name'],
                        'subject': course['subject'],
                        'topic': topic['topic'],
                        'accuracy': topic['accuracy'],
                        'attempts': topic['attempts'],
                        'subtopics': topic['subtopics'],
                    }
                    break

        total_topics += len(topics)
        cleared_count += done

        courses.append({
            'code': course['code'],
            'name': course['name'],
            'subject': course['subject'],
            'description': course['description'],
            'status': status,
            'topics': topics,
            'cleared': done,
            'total': len(topics),
            'progress_percent': (
                round(done / len(topics) * 100, 1) if topics else 100.0
            ),
        })

    return {
        'courses': courses,
        'current': current,
        'pass_mark': PASS_MARK,
        'quiz_length': QUIZ_LENGTH,
        'required_correct': required_correct(),
        'total_weak_topics': total_topics,
        'cleared_topics': cleared_count,
        'remaining_topics': total_topics - cleared_count,
        'strong_topics': strong_topics(db, user_id),
        'overall_percent': (
            round(cleared_count / total_topics * 100, 1) if total_topics else 0.0
        ),
        'all_courses_completed': total_topics > 0 and cleared_count == total_topics,
        'quiz': quiz_payload(db, user_id),
    }


def student_snapshot(db: Session, user_id: int):
    """One payload covering a student's full learning state.

    Used by the student dashboard and by the teacher dashboard so both show the
    same numbers the moment a quiz is submitted.
    """
    perf = topic_performance(db, user_id)
    cleared = cleared_topics(db, user_id)
    best = topic_best_scores(db, user_id)
    per_topic = subtopic_performance(db, user_id)

    attempts = sum(s['attempts'] for s in perf.values())
    correct = sum(
        round(s['accuracy'] * s['attempts'] / 100) for s in perf.values()
    )

    topics = []
    for raw, stats in perf.items():
        matched = match_topic(raw)
        if not matched or not stats['attempts']:
            continue
        code, topic = matched
        accuracy = PASS_MARK if topic in cleared else stats['accuracy']
        topics.append({
            'topic': topic,
            'course_code': code,
            'accuracy': round(accuracy, 1),
            'attempts': stats['attempts'],
            'best_score': best.get(topic),
            'cleared': topic in cleared,
            'status': 'completed' if topic in cleared else (
                'strong' if accuracy >= WEAK_THRESHOLD else 'weak'
            ),
            'subtopics': _subtopic_detail(topic, per_topic),
        })

    seen = set()
    unique = []
    for entry in topics:
        key = entry['topic']
        if key in seen:
            # Keep the strongest reading for a topic seen via two raw strings.
            prior = next(t for t in unique if t['topic'] == key)
            if entry['accuracy'] > prior['accuracy']:
                prior.update(entry)
            continue
        seen.add(key)
        unique.append(entry)

    for entry in unique:
        entry['position'] = course_topic_position(entry['course_code'], entry['topic'])
    unique.sort(key=lambda t: t['position'])

    return {
        'overall_score': round(
            correct / attempts * 100, 1) if attempts else 0.0,
        'attempted': attempts,
        'correct': correct,
        'cleared_count': len(cleared),
        'strong_count': sum(1 for t in unique if t['status'] in ('strong', 'completed')),
        'weak_count': sum(1 for t in unique if t['status'] == 'weak'),
        'topics': unique,
    }
