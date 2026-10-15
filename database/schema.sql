CREATE DATABASE IF NOT EXISTS llm_education;
USE llm_education;

-- Tables are also created automatically by SQLAlchemy on first backend start.
-- This file is provided for project documentation/manual setup.

CREATE TABLE IF NOT EXISTS users (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(120) NOT NULL,
  email VARCHAR(180) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(30) DEFAULT 'student',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
  id INT PRIMARY KEY AUTO_INCREMENT,
  title VARCHAR(255) NOT NULL,
  subject VARCHAR(120),
  content LONGTEXT NOT NULL,
  source_page VARCHAR(50),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- One weak-topic quiz inside the course -> topic learning path.
-- status: in_progress -> passed | failed
-- quiz_date backs the "one quiz per student per day" lock.
-- Created before questions because questions.quiz_id points at it.
CREATE TABLE IF NOT EXISTS topic_quizzes (
  id INT PRIMARY KEY AUTO_INCREMENT,
  user_id INT NOT NULL,
  course_code VARCHAR(60) NOT NULL,
  course_name VARCHAR(160),
  subject VARCHAR(120) NOT NULL,
  topic VARCHAR(120) NOT NULL,
  total INT DEFAULT 10,
  correct INT DEFAULT 0,
  answered INT DEFAULT 0,
  score FLOAT,
  status VARCHAR(20) DEFAULT 'in_progress',
  quiz_date DATE NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  completed_at DATETIME NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX ix_topic_quizzes_user_date ON topic_quizzes (user_id, quiz_date);

CREATE TABLE IF NOT EXISTS questions (
  id INT PRIMARY KEY AUTO_INCREMENT,
  subject VARCHAR(120) NOT NULL,
  topic VARCHAR(120) NOT NULL,
  difficulty VARCHAR(30) NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  options_json TEXT,
  explanation TEXT,
  subtopic VARCHAR(120),
  quiz_id INT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (quiz_id) REFERENCES topic_quizzes(id)
);

CREATE INDEX ix_questions_quiz_id ON questions (quiz_id);

CREATE TABLE IF NOT EXISTS attempts (
  id INT PRIMARY KEY AUTO_INCREMENT,
  user_id INT NOT NULL,
  question_id INT NOT NULL,
  user_answer TEXT,
  score FLOAT DEFAULT 0,
  hints_used INT DEFAULT 0,
  feedback TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (question_id) REFERENCES questions(id)
);
