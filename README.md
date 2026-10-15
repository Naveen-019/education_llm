# LLM Personalized Education System

Final-year project based on the supplied feature-analysis document for **“Large Language Models for Education: A Survey and Outlook”**.

## Stack
- Frontend: React + Vite
- Backend: FastAPI + SQLAlchemy
- Database: MySQL
- AI: Google Gemini through `GEMINI_API_KEY`
- RAG: Two independent answers per question, one from the model and one grounded in the `docs/` folder

## Three main additions
1. **Student Performance Dashboard** — scores, attempts, strengths and weak topics.
2. **Weak Topic Detection + Adaptive Difficulty** — performance is analyzed per topic and the next quiz difficulty is adjusted.
3. **Personalized Learning Path + Source-backed Tutor** — recommended study steps and RAG-style source snippets are returned with tutor answers.

## Weak topic quiz loop (courses one by one, topics one by one)

Weak topics are no longer just a list. They are turned into a **sequential
learning structure**: courses are worked through one at a time, and inside a
course only **one weak topic is active at a time**.

| Rule | Where it lives |
|---|---|
| Curriculum of courses → ordered topics | `backend/app/curriculum.py` (`COURSES`) |
| A topic is weak when accuracy is below 80% | `backend/app/progress.py` (`weak_topics`) |
| Weak topics grouped into courses, ordered by curriculum order | `backend/app/progress.py` (`build_plan`) |
| Only the first uncleared weak topic is active | `backend/app/progress.py` (`build_plan`) |
| LLM generates 10 questions for that one topic | `backend/app/services.py` (`create_quiz`) |
| Score ≥ 80% (8 of 10) clears the topic and promotes the next one | `backend/app/main.py` (`quiz_submit`) |
| One quiz per student per calendar day | `backend/app/progress.py` (`quiz_state`) |

### How a student moves through it

1. Attempts are grouped per topic from the `attempts` + `questions` tables.
2. Topics below 80% accuracy become weak topics and are placed inside their course.
3. The dashboard shows the **current course** and the **current weak topic**, plus the
   queue of what comes next.
4. Starting the quiz generates **10 questions on that one topic only**.
5. Scoring **80% or more** clears the topic — it disappears from the weak list, the
   dashboard shows it as completed, and the next weak topic becomes active.
6. Scoring below 80% keeps the topic weak so it can be retried later.
7. Only **one quiz per day** is allowed, so progress is spread across days.

`/api/learning-path` returns the whole structure (courses with per-topic
`cleared` / `current` / `pending` / `locked` status and a completion percentage).

### Quiz endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/quiz/today` | Daily lock state + the current weak topic |
| `POST` | `/api/quiz/start` | Generate/resume the 10-question quiz for the active topic |
| `POST` | `/api/quiz/answer` | Record one answer (live progress) |
| `POST` | `/api/quiz/submit` | Grade the quiz, clear the topic if the pass mark is met |
| `GET` | `/api/quiz/history` | Past weak-topic quizzes |

## Features included
- Student registration/login (demo JWT-style token)
- AI tutor with hint-first mode
- Source-backed educational retrieval
- Question generation
- Quiz submission and automatic grading
- Weak-topic detection
- One-by-one course and topic learning path
- 10-question LLM-generated weak-topic quiz with an 80% pass mark
- One quiz per student per day
- Adaptive difficulty
- Personalized learning path
- Performance dashboard
- Teacher console — see all students, their scores, weak topics and recent attempts (role-aware dashboard)

## Run
### 1. Database
Create a MySQL database and run:

```sql
CREATE DATABASE llm_education;
```

Then copy `.env.example` to `.env` and set your MySQL credentials.

### 2. Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

API: http://localhost:8000/docs

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open the URL shown by Vite, normally http://localhost:5173.

## Optional AI
Get a free key from Google AI Studio (https://aistudio.google.com/apikey) and set `GEMINI_API_KEY` in `backend/.env`. Without a key, the application uses a deterministic demo tutor/question generator so the project still runs for demonstration.

## Demo accounts
After startup, use the Register page to create a student (or a teacher by choosing the Teacher role). The dashboard will automatically create a learning profile.

Seeded demo accounts (`database/seed_250.sql`, password `demo123`):
- Students: `student1@example.com` … `student8@example.com`
- Teachers: `anil.teacher@example.com`, `meena.teacher@example.com`

## Project structure

```text
llm_education_system/
├── backend/
│   ├── app/
│   │   ├── main.py         # API endpoints, including the weak-topic quiz
│   │   ├── models.py       # users, documents, questions, attempts, topic_quizzes
│   │   ├── schemas.py      # request bodies
│   │   ├── database.py
│   │   ├── auth.py
│   │   ├── rag.py
│   │   ├── curriculum.py   # course -> topic learning structure
│   │   ├── progress.py     # weak-topic detection, course plan, daily lock
│   │   └── services.py     # tutor, question + 10-question quiz generation
│   ├── test_quiz_flow.py   # end-to-end check of the quiz loop
│   ├── test_quiz_api.py    # HTTP contract check for the quiz endpoints
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── api.js
│   │   └── main.jsx
│   └── package.json
├── database/
│   └── schema.sql
└── docs/
    └── project_scope.md
```

## Tests

```bash
cd backend
python test_tutor.py         # AI tutor: answer mode, hint mode, 429 retry
python test_kb.py            # docs folder reading, subject tagging, RAG ranking
python test_quiz_flow.py     # weak-topic loop rules
python test_quiz_api.py      # endpoint + response contract
```

All run offline against an in-memory database, so no MySQL or API key is needed.
Use `python test_tutor.py --live` to exercise the real Gemini call.

## The knowledge base (RAG)

The AI Tutor reads one fixed folder: **`docs/`** in the project root. There is
no upload screen and no upload API — the folder *is* the knowledge base, and
whatever it contains is indexed on the next question.

```
docs/
  EduLLM_RAG_Course_Knowledge_Base.docx   course material for all four subjects
  project_scope.md                        what the project is for
  tutor_history.md                        written by the app (see below)
```

`DOCS_PATH` overrides the location if you want to point it elsewhere.

`.docx`, `.pdf`, `.md`, `.txt`, `.rst` and `.csv` are read; Word lock files
(`.~lock…`, `~$…`) are ignored. Each file is split on its own headings, and the
heading levels are respected: a top-level heading names the course, so the
numbered sections underneath it are filed under that course automatically.
Nothing needs tagging by hand.

Sections that match a question are re-read whenever a file in the folder
changes, so editing or dropping in a `.docx` takes effect immediately with no
restart and no re-import step.

### Two answers for every question

**Ask** returns two replies to the same question, generated by two separate
requests:

1. **From the AI** — the model's own explanation, with no document in the
   prompt, so it is the general-knowledge answer.
2. **From your documents** — a second reply written *only* from the sections
   listed underneath it, citing each point as `[1]`, `[2]`, so it can be traced
   back to the material.

Seeing both side by side is the point: where the course material agrees with
general knowledge, and where it does not, is visible at a glance.

**Get Hint** still returns exactly one clue and withholds the answer, so the
two buttons never blur together.

Every question and its answer are appended to `docs/tutor_history.md`, which is
indexed like any other file. The knowledge base therefore grows with what is
actually asked. History is ranked *below* the course material even when it
quotes a note almost verbatim, so a past answer can never displace the section
it was written from.

### How retrieval works

Two things make retrieval work rather than silently return nothing:

* **Subject tags are mapped onto the four courses.** A note tagged
  "Data Structures", "Machine Learning" or "Artificial Intelligence" used to be
  filtered out of every search, which is why the tutor reported *"no matching
  course material"* for material that was sitting right there. Subject filtering
  is a soft preference: a mismatched or missing tag can never hide a relevant
  note, it only has to fall back.
* **Ranking uses inverse-document-frequency.** Raw word overlap ranked whichever
  note happened to share a common word first — a question about breadth-first
  search matched a note about stacks. Rare words now count for much more, and a
  title hit counts double.

The folder and the seeded notes in MySQL are scored together, so a section in
`docs/` competes fairly with the built-in course notes.

## AI tutor

`POST /api/tutor` takes `question`, `subject` and `mode`:

| Mode | Behaviour |
|---|---|
| `answer` | Two replies: `answer` from the model's own knowledge, `document_answer` grounded in `docs/` and citing `[1]`, `[2]` |
| `hint` | One focused clue in `hint`; `answer` stays empty so the solution is withheld |

The response also carries `sources` (each with `title`, `file`, `page`,
`subject`), `grounded` (did anything in the knowledge base match) and `offline`
(did the model answer at all). `GET /api/knowledge-base` reports the folder
path, the files found and the number of indexed sections.

Both modes work for **every** subject, and they are two separate buttons in the
UI: **Ask** always requests `answer`, **Get Hint** always requests `hint`. The
shared mode toggle that previously made "Ask" return a hint has been removed,
and answer mode strips a leading `Hint:` label from the model output so the two
can never read the same.

Ask issues **two** requests, so the 20-per-day Gemini free tier is spent twice
as fast; rate limits are expected and the tutor falls back to the documents
while they last.
`llm_text` handles this, and the two kinds of `429` are told apart because
conflating them is what made the tutor look broken:

* **short throttle** — *"Quota exceeded. Please retry in 2s."* These are
  **retried on the same model**, because they clear within seconds and the API
  even tells you how long to wait.
* **allowance spent** — *"You exceeded your current quota, please check your
  plan and billing details."* Nothing can fix this but billing or a daily
  reset, so retrying it only burns the budget in silence. The tutor stops
  immediately, marks the key as blocked for 90 seconds so a second call in the
  same request fails instantly, and returns the document answer straight away.
  Measured on an exhausted key: **45.6s of silence became 0.9s.**

A `404` means Google has retired that model name, so the next candidate is used
immediately instead of retrying a name that can never work. Total retry time is
capped by `LLM_RETRY_BUDGET` (default 12 seconds) so the student never waits on
a dead API.

The response reports `llm_status`: `ok`, or `quota` when the daily allowance is
spent, or `error` when the API was otherwise unreachable. The UI says which, so
a silent panel always explains itself.

Models are tried in order: `GEMINI_MODEL` from `.env` first, then
`gemini-3.8-flash`, `gemini-3.6-flash` and `gemini-flash-latest`. Pinning a
single model name is what previously forced the tutor into offline mode after
Google retired it, so the fallback list is deliberately built in.

Both replies are deliberately short. The prompts cap them at roughly three
sentences, forbid restating the question and forbid trailing offers of further
help, and ask the document reply to ignore material that does not answer the
question asked. Without the model, the document panel quotes only the sentences
of a section that actually share wording with the question, and skips sections
of *example questions*, which state the problem rather than answer it.

If the model stays unavailable, the response is built from the retrieved course
notes and the payload sets `offline: true`, which the UI surfaces.

## Sub-parts and topic strength

Every curriculum topic is broken into sub-parts (`SUBTOPICS` in
`app/curriculum.py`) — for example *Hive* becomes Partitions, Buckets, HiveQL
Basics and SerDe/Tables. A weak topic now shows its sub-parts wherever it
appears:

* the dashboard and learning path list each sub-part with its own accuracy,
* the quiz header lists the sub-parts being covered, and every question carries
  a sub-part badge,
* the result screen shows a per-sub-part breakdown with each part marked strong
  or needing work,
* the teacher dashboard shows the same breakdown per student.

The 10 questions of a quiz are spread across the topic's sub-parts rather than
being drawn from one corner of it. The LLM is asked to tag each question with a
`part`; untagged questions are matched back to a sub-part from their wording, so
every question always carries one.

A topic moves from weak to strong as soon as it reaches the pass mark, and a
passed topic is removed from weak-topic detection entirely. The teacher sees the
split in two places: `cleared_topics` (green, marked completed) and
`weak_topics`, with `path_percent` tracking overall course progress.
