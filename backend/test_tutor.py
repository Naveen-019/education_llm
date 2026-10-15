"""Checks the AI tutor: answer mode and hint mode, over HTTP.

Run with:  python test_tutor.py

Verifies the two things the tutor must get right:
  * "Ask" returns an LLM-written answer for the question
  * "Hint-first" returns a hint, and it works for every subject

The LLM is stubbed by default so the test is offline and deterministic. Pass
--live to call Gemini for real.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.rag import seed_documents
from app import services as S
import app.database as database
import app.main as main

LIVE = "--live" in sys.argv

FAILURES = []


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


def build_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    main.engine = engine
    database.engine = engine
    database.SessionLocal = session
    main.get_db = database.get_db
    import app.auth as auth
    auth.get_db = database.get_db

    db = session()
    seed_documents(db)
    db.close()
    return TestClient(main.app), session


class _QuotaClient:
    """Client whose every call fails the way an exhausted daily quota does."""

    def __init__(self, exc):
        self.models = self
        self.exc = exc
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        raise self.exc


def main_test():
    if not LIVE:
        S._get_client = lambda: None  # deterministic offline stub

    client, session = build_client()

    res = client.post("/api/auth/register", json={
        "name": "Tutor Student",
        "email": "tutor@test.com",
        "password": "secret123",
        "role": "student",
    })
    check("register succeeds", res.status_code == 200, res.text[:100])
    headers = {"Authorization": f"Bearer {res.json()['token']}"}

    # --- /subjects contract ---
    res = client.get("/api/subjects")
    check("subjects 200", res.status_code == 200)
    subjects = res.json()
    check("subjects lists available subjects", len(subjects["subjects"]) > 0, subjects["subjects"])
    check("subjects advertises both modes",
          set(subjects.get("modes", [])) == {"hint", "answer"}, subjects.get("modes"))
    check("no subject is hint-restricted", "hint_subjects" not in subjects)

    # --- answer mode ---
    res = client.post("/api/tutor", json={
        "question": "How does Hive partitioning improve query performance?",
        "subject": "Big Data",
        "mode": "answer",
    }, headers=headers)
    check("answer mode 200", res.status_code == 200, res.text[:150])
    answer = res.json()
    check("answer returns text", bool(answer["answer"]), f"{len(answer['answer'] or '')} chars")
    check("answer returns sources list", isinstance(answer["sources"], list))
    check("answer is not a hint", not (answer["answer"] or "").startswith("Hint:"))
    check("offline flag is a bool", isinstance(answer["offline"], bool))
    if LIVE:
        check("live: LLM actually answered", answer["offline"] is False, answer["answer"][:60])
    else:
        check("offline: flagged as offline", answer["offline"] is True)

    # --- hint mode on every subject ---
    for subject in subjects["subjects"]:
        res = client.post("/api/tutor", json={
            "question": "Explain the key idea for this topic.",
            "subject": subject,
            "mode": "hint",
        }, headers=headers)
        check(f"hint mode 200 for {subject}", res.status_code == 200, res.text[:100])
        hint = res.json()
        check(f"hint returns text for {subject}", bool(hint["hint"]),
              f"{len(hint['hint'] or '')} chars")
        check(f"hint withholds the answer for {subject}", not hint["answer"])
        if not LIVE:
            check(f"hint is a hint for {subject}",
                  (hint["hint"] or "").startswith("Hint:"),
                  (hint["hint"] or "")[:50])

        # hint and answer must never be the same text
        ans = client.post("/api/tutor", json={
            "question": "Explain the key idea for this topic.",
            "subject": subject,
            "mode": "answer",
        }, headers=headers).json()
        check(f"hint differs from answer for {subject}",
              hint["hint"] != ans["answer"])

    # --- answer mode returns two independent replies ---
    res = client.post("/api/tutor", json={
        "question": "What is normalization and why is it used?",
        "subject": "DBMS",
        "mode": "answer",
    }, headers=headers)
    body = res.json()
    check("answer mode 200", res.status_code == 200, res.text[:120])
    check("model answer present", bool(body["answer"]), (body["answer"] or "")[:60])
    check("document answer present", bool(body["document_answer"]),
          (body["document_answer"] or "")[:60])
    check("the two answers are different text",
          body["answer"] != body["document_answer"])
    check("sources are listed", isinstance(body["sources"], list))
    check("knowledge base was grounded", body["grounded"] is True)
    check("sources name the file they came from",
          all("file" in s for s in body["sources"]),
          str(body["sources"][:1])[:120])

    # --- a spent daily quota must fail fast, not hang ---
    # A 429 that says "check your plan and billing" is a spent allowance, not
    # short-term throttling. Retrying it burned the whole budget in silence,
    # and answer mode paid that cost twice.
    import time as _time
    from google.genai import errors as _errors

    S._quota_blocked_until = 0.0
    hard = _errors.ClientError('RESOURCE_EXHAUSTED', {'error': {
        'code': 429,
        'message': 'You exceeded your current quota, please check your plan and '
                   'billing details.',
    }}, None)
    check("hard quota error is recognised", S._quota_exhausted(hard))
    check("transient throttle is not", not S._quota_exhausted(_errors.ClientError(
        'RESOURCE_EXHAUSTED', {'error': {'code': 429,
        'message': 'Resource has been exhausted. Please retry in 2s.'}}, None)))

    S._get_client = lambda: _QuotaClient(hard)
    started = _time.monotonic()
    out = S.llm_text("Anything", max_tokens=50)
    spent = _time.monotonic() - started
    check("spent quota returns immediately", spent < 5.0, f"{spent:.1f}s")
    check("spent quota returns no text", out is None)
    check("quota is remembered for later calls", S.quota_blocked())
    started = _time.monotonic()
    S.llm_text("Anything again", max_tokens=50)
    check("second call short-circuits", _time.monotonic() - started < 0.5)
    S._quota_blocked_until = 0.0

    # --- the document answer must stay on the question asked ---
    saved_getter_doc = S._get_client
    S._get_client = lambda: None
    try:
        r = S.tutor(session(), "What are the disadvantages of artificial intelligence?",
                    "AI", "answer")
    finally:
        S._get_client = saved_getter_doc
        S._quota_blocked_until = 0.0
    doc = r["document_answer"]
    check("document answer is short", len(doc) < 900, f"{len(doc)} chars")
    check("document answer drops question-only sections",
          "Example Student Questions" not in doc, doc[:120])
    check("document answer keeps the matching section",
          "Disadvantages" in doc, doc[:120])
    check("document answer quotes at most two sections",
          doc.count("— EduLLM") <= 2, str(doc.count("— EduLLM")))

    h = S.tutor(session(), "What is a primary key?", "DBMS", "hint")
    check("hint status is reported honestly, not assumed ok",
          h["llm_status"] in ("ok", "quota", "error"), h["llm_status"])
    check("hint never points at a page of example questions",
          "Example Student Questions" not in (h["hint"] or ""), (h["hint"] or "")[:120])
    check("hint never points at the answer history",
          "Answer History" not in (h["hint"] or ""), (h["hint"] or "")[:120])
    check("hint still withholds the answer", not h["answer"])

    # --- bad mode falls back to answer, does not crash ---
    res = client.post("/api/tutor", json={
        "question": "What is normalization?",
        "subject": "DBMS",
        "mode": "nonsense",
    }, headers=headers)
    check("unknown mode still answers", res.status_code == 200 and bool(res.json()["answer"]))

    # --- empty question is handled ---
    res = client.post("/api/tutor", json={"question": "   ", "subject": "DBMS", "mode": "answer"},
                      headers=headers)
    check("empty question does not 500", res.status_code == 200, res.status_code)

    # --- requires auth ---
    res = client.post("/api/tutor", json={"question": "hi", "subject": "DBMS", "mode": "answer"})
    check("tutor requires auth", res.status_code in (401, 403), res.status_code)

    # --- rate limiting must be retried, not silently swallowed ---
    retry_checks(client, session, subjects["subjects"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("All tutor checks passed." + ("" if LIVE else "  (offline stub; use --live for Gemini)"))
    return 0


def retry_checks(client, session, subject_list):
    """A 429 must be retried until it recovers, not fall back on the first hit.

    This is the regression test for the bug where ``ClientError.code`` is the
    string 'RESOURCE_EXHAUSTED', so the old numeric check never matched and the
    LLM was never actually called.
    """
    from google.genai import errors

    class FakeResp:
        def __init__(self, text):
            self.text = text

    class FakeModels:
        def __init__(self, failures):
            self.failures = failures
            self.seen = 0
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            if self.seen < self.failures:
                self.seen += 1
                raise errors.ClientError(
                    'RESOURCE_EXHAUSTED',
                    {'error': {'code': 429, 'message': 'Quota exceeded. Please retry in 0.05s.'}},
                    None,
                )
            # Answer the way the real model would: differently per mode, so a
            # test can prove Ask and Get Hint do not return the same thing.
            prompt = kwargs.get('contents') or kwargs.get('prompt') or ''
            if isinstance(prompt, list):
                prompt = ' '.join(
                    getattr(c, 'text', str(c)) for c in prompt
                )
            if 'ONE focused hint' in str(prompt):
                return FakeResp('Recovered LLM hint about partitioning.')
            return FakeResp('Recovered LLM answer about partitioning.')

    # Any earlier check that hit the live API may have marked the allowance as
    # spent; clear it so these checks exercise the retry path for real.
    S._quota_blocked_until = 0.0

    class FakeClient:
        def __init__(self, failures):
            self.models = FakeModels(failures)

    saved_client = S._client
    saved_getter = S._get_client
    try:
        for failures, label in ((1, '429 once'), (2, '429 twice'), (3, '429 three times')):
            fake = FakeClient(failures)
            S._get_client = lambda fake=fake: fake
            res = S.tutor(
                session(), 'How does Hive partitioning work?', 'Big Data', 'answer'
            )
            answer, offline = res['answer'], res['offline']
            check(f"{label} then success: LLM answer returned", offline is False, f"calls={fake.models.calls}")
            check(f"{label} then success: recovered text used",
                  answer.startswith('Recovered LLM'), answer[:50])
            # Answer mode sends two requests (general + document-grounded), and
            # the fake client fails the first `failures` calls of each.
            check(f"{label} then success: retried, not skipped",
                  fake.models.calls >= failures + 1, f"calls={fake.models.calls}")

        fake = FakeClient(99)
        S._get_client = lambda: fake
        res = S.tutor(session(), 'How does Hive partitioning work?', 'Big Data', 'answer')
        answer, offline = res['answer'], res['offline']
        check("permanent 429 falls back instead of failing", offline is True)
        check("permanent 429 still returns readable text", len(answer) > 50, f"{len(answer)} chars")

        fake = FakeClient(2)
        S._get_client = lambda: fake
        res = S.tutor(session(), 'How does Hive partitioning work?', 'Big Data', 'hint')
        check("hint mode also recovers from 429", res['offline'] is False, res['hint'][:50])

        # A non-retryable failure must not burn the retry budget.
        class HardFail(FakeModels):
            def generate_content(self, **kwargs):
                self.calls += 1
                raise errors.ClientError('INVALID_ARGUMENT', {'error': {'code': 400, 'message': 'bad'}}, None)

        class HardClient(FakeClient):
            def __init__(self):
                self.models = HardFail(0)

        hard = HardClient()
        S._get_client = lambda: hard
        S.tutor(session(), 'How does Hive partitioning work?', 'Big Data', 'answer')
        # Two requests in answer mode, each failing on its first attempt: a
        # non-retryable error must not be retried.
        check("400 fails fast without retrying", hard.models.calls == 2,
              f"calls={hard.models.calls}")

        # Answer mode must never read as a hint, even when the model opens with
        # one. "Ask" and "Get Hint" have to stay visibly different.
        class HintPrefixed:
            def __init__(self):
                self.calls = 0

            def generate_content(self, **kwargs):
                self.calls += 1
                return type("R", (), {
                    "text": "Hint: recall the default HDFS block size."
                })()

        class HintClient:
            def __init__(self):
                self.models = HintPrefixed()

        S._get_client = lambda: HintClient()
        answer = S.tutor(
            session(), 'What is the default HDFS block size?', 'Big Data', 'answer'
        )['answer']
        check("answer mode strips a leading Hint label",
              not answer.lstrip().lower().startswith('hint:'), answer[:40])
        check("answer mode keeps the content", 'block size' in answer.lower(),
              answer[:60])

        # The two modes must remain distinct for the same question.
        S._get_client = lambda: FakeClient(0)
        ask_text = S.tutor(
            session(), 'What is the default HDFS block size?', 'Big Data', 'answer')['answer']
        hint_text = S.tutor(
            session(), 'What is the default HDFS block size?', 'Big Data', 'hint')['hint']
        check("ask and hint differ for the same question",
              ask_text != hint_text, f"{len(ask_text)} vs {len(hint_text)} chars")

        # A transient quota 429 must be retried, not abandoned: the free tier
        # reports brief throttling as RESOURCE_EXHAUSTED and it clears.
        check("transient 429 is retried, not swapped", not S._swap_model(
            errors.ClientError('RESOURCE_EXHAUSTED',
                               {'error': {'code': 429,
                                          'message': 'exceeded your current quota. Please retry in 2s.'}},
                               None)))
        check("transient 503 is retried, not swapped", not S._swap_model(
            errors.ClientError('UNAVAILABLE',
                               {'error': {'code': 503, 'message': 'high demand'}}, None)))

        # A retired model name must switch immediately instead of retrying.
        check("retired model 404 triggers a model swap", S._swap_model(
            errors.ClientError('NOT_FOUND',
                               {'error': {'code': 404, 'message': 'no longer available'}}, None)))

        # A 404 on the first model must fall through to a working one.
        class FirstModelGone:
            def __init__(self):
                self.seen = []

            def generate_content(self, **kwargs):
                self.seen.append(kwargs.get('model'))
                if len(self.seen) == 1:
                    raise errors.ClientError('NOT_FOUND',
                                             {'error': {'code': 404, 'message': 'retired'}}, None)
                return FakeResp('Answer from the fallback model.')

        class MultiModelClient:
            def __init__(self):
                self.models = FirstModelGone()

        multi = MultiModelClient()
        saved_models = S._model_candidates
        try:
            S._model_candidates = lambda: ['retired-model', 'good-model']
            S._get_client = lambda: multi
            text = S.llm_text('hi', max_tokens=20, attempts=3, budget=5)
            check("falls through to the next model after a 404",
                  text == 'Answer from the fallback model.', repr(text))
            check("the retired model was tried exactly once",
                  multi.models.seen == ['retired-model', 'good-model'],
                  str(multi.models.seen))
        finally:
            S._model_candidates = saved_models
    finally:
        S._client = saved_client
        S._get_client = saved_getter


if __name__ == "__main__":
    sys.exit(main_test())
