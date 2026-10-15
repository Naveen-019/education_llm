from datetime import date

from sqlalchemy import (
    Column, Integer, String, Text, Float, Date, DateTime, ForeignKey, Index
)
from sqlalchemy.sql import func
from .database import Base
from .curriculum import QUIZ_LENGTH

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    email = Column(String(180), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(30), default='student')
    created_at = Column(DateTime, server_default=func.now())

class Document(Base):
    __tablename__ = 'documents'
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    subject = Column(String(120))
    content = Column(Text, nullable=False)
    source_page = Column(String(50))
    created_at = Column(DateTime, server_default=func.now())

class TopicQuiz(Base):
    """One weak-topic quiz sitting inside the course -> topic learning path.

    ``status`` moves in_progress -> passed | failed. A passed row is what makes
    a weak topic disappear from the student's plan and unlock the next topic.
    ``quiz_date`` backs the hard "one course per day" lock.
    """
    __tablename__ = 'topic_quizzes'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    course_code = Column(String(60), nullable=False)
    course_name = Column(String(160))
    subject = Column(String(120), nullable=False)
    topic = Column(String(120), nullable=False)
    total = Column(Integer, default=QUIZ_LENGTH)
    correct = Column(Integer, default=0)
    answered = Column(Integer, default=0)
    score = Column(Float)
    status = Column(String(20), default='in_progress')
    quiz_date = Column(Date, nullable=False, default=date.today)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)

    __table_args__ = (
        Index('ix_topic_quizzes_user_date', 'user_id', 'quiz_date'),
    )

class Question(Base):
    __tablename__ = 'questions'
    id = Column(Integer, primary_key=True)
    subject = Column(String(120), nullable=False)
    topic = Column(String(120), nullable=False)
    difficulty = Column(String(30), nullable=False)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    options_json = Column(Text)
    explanation = Column(Text)
    subtopic = Column(String(120))
    quiz_id = Column(Integer, ForeignKey('topic_quizzes.id'), index=True)
    created_at = Column(DateTime, server_default=func.now())

class Attempt(Base):
    __tablename__ = 'attempts'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    question_id = Column(Integer, ForeignKey('questions.id'), nullable=False)
    user_answer = Column(Text)
    score = Column(Float, default=0)
    hints_used = Column(Integer, default=0)
    feedback = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
