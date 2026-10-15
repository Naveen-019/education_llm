from pydantic import BaseModel, EmailStr, Field, model_validator
from typing import Optional

class RegisterIn(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=6)
    role: Optional[str] = 'student'

    @model_validator(mode='after')
    def _valid_role(self):
        self.role = (self.role or 'student').strip().lower()
        if self.role not in ('student', 'teacher'):
            raise ValueError('Role must be student or teacher')
        return self

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class TutorIn(BaseModel):
    question: str
    subject: Optional[str] = 'General'
    mode: str = 'hint'

class GenerateIn(BaseModel):
    subject: str
    topic: str
    difficulty: str = 'medium'

class SubmitIn(BaseModel):
    question_id: int
    answer: str
    hints_used: int = 0

class QuizStartIn(BaseModel):
    course_code: Optional[str] = None

class QuizAnswerIn(BaseModel):
    quiz_id: int
    question_id: int
    answer: str
    hints_used: int = 0

class QuizAnswerEntry(BaseModel):
    question_id: int
    answer: str
    hints_used: int = 0

class QuizSubmitIn(BaseModel):
    quiz_id: int
    answers: Optional[list[QuizAnswerEntry]] = None
