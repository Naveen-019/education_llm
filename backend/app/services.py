import json, os, re, time
from datetime import datetime
from sqlalchemy.orm import Session
from .models import Question, Attempt, TopicQuiz
from .rag import retrieve, SUBJECT_ALIASES
from .curriculum import subtopics_of, match_subtopic

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None

_client = None

def _get_client():
    global _client
    key = os.getenv('GEMINI_API_KEY')
    if not key or not genai:
        return None
    if _client is None:
        _client = genai.Client(api_key=key)
    return _client

def _status_code(exc):
    """Best-effort numeric HTTP status for a Google GenAI or HTTP error.

    ``google.genai`` errors expose ``code``/``status`` as *string* names such as
    ``'RESOURCE_EXHAUSTED'``; the numeric code only appears inside ``details``.
    """
    details = getattr(exc, 'details', None)
    if isinstance(details, dict):
        error = details.get('error')
        if isinstance(error, dict):
            for key in ('code', 'status'):
                value = error.get(key)
                if isinstance(value, int):
                    return value

    for attr in ('status_code', 'http_status'):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


# Models tried in order. Google retires model names and changes per-key quota
# without warning, and a single hard-coded name is what silently forced the
# tutor into offline mode before. The first entry wins; the rest are only
# reached when one is unavailable, so this costs nothing in the normal case.
_DEFAULT_MODELS = ('gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-flash-latest')
_last_model = None


def _model_candidates():
    """Configured model first, then known fallbacks, with no duplicates."""
    chosen = [os.getenv('GEMINI_MODEL', '').strip()]
    chosen.extend(_DEFAULT_MODELS)
    seen = set()
    ordered = []
    for name in chosen:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _swap_model(exc) -> bool:
    """True only when the model itself is unusable, so the caller should switch.

    A 404 means Google has retired the model name, which will never recover on
    its own. Transient 429/503 are deliberately *not* treated this way: the
    free tier reports brief quota throttling as RESOURCE_EXHAUSTED, and those
    clear within seconds, so they must be retried on the same model first.
    """
    code = _status_code(exc)
    if code == 404:
        return True
    name = str(
        getattr(exc, 'code', '') or getattr(exc, 'status', '') or ''
    ).upper()
    return name in ('NOT_FOUND',)


# Transient failures worth retrying: rate limits and server-side errors.
_RETRYABLE_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_RETRYABLE_NAMES = {
    'RESOURCE_EXHAUSTED',
    'INTERNAL',
    'UNAVAILABLE',
    'DEADLINE_EXCEEDED',
    'ABORTED',
}


def _retryable(exc) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    code = _status_code(exc)
    if code is not None:
        return code in _RETRYABLE_CODES

    name = str(
        getattr(exc, 'code', '') or getattr(exc, 'status', '') or ''
    ).upper()
    if name in _RETRYABLE_NAMES:
        return True

    text = str(getattr(exc, 'message', '') or exc).upper()
    return any(marker in text for marker in (
        '429', 'RESOURCE_EXHAUSTED', 'RATE LIMIT',
        'TIMEOUT', 'UNAVAILABLE', 'INTERNAL ERROR',
    ))


def _retry_delay(exc, fallback: float) -> float:
    """Honour the "please retry in Ns" hint the API sends with a 429."""
    text = str(getattr(exc, 'message', '') or exc)
    match = re.search(r'retry in\s+([0-9]+(?:\.[0-9]+)?)\s*s', text, re.I)
    if match:
        try:
            return min(60.0, max(1.0, float(match.group(1)) + 1.5))
        except ValueError:
            pass
    return fallback


# A 429 from the free tier means two very different things, and treating them
# the same is what made the tutor look broken:
#
#   "...quota... Please retry in 2s"        short throttle, clears in seconds
#   "You exceeded your current quota, please
#    check your plan and billing details"  the daily allowance is gone
#
# The second one cannot be fixed by waiting, so retrying it only burns the
# whole budget in silence before the caller falls back anyway.
_HARD_QUOTA_MARKERS = (
    'check your plan and billing',
    'billing details',
    'quota for the current project',
    'exceeded your current quota, please',
)

# Once the allowance is known to be spent, remember it briefly so a second call
# in the same request fails instantly instead of hanging all over again.
_quota_blocked_until = 0.0
QUOTA_BLOCK_SECONDS = 90.0


def _quota_exhausted(exc) -> bool:
    """True when the daily allowance is spent, which waiting will not fix."""
    text = str(getattr(exc, 'message', '') or exc).lower()
    return any(marker in text for marker in _HARD_QUOTA_MARKERS)


def quota_blocked() -> bool:
    return time.monotonic() < _quota_blocked_until


def _mark_quota_exhausted():
    global _quota_blocked_until
    _quota_blocked_until = time.monotonic() + QUOTA_BLOCK_SECONDS


def llm_text(prompt: str, max_tokens: int = 500, attempts: int = 6, budget: float | None = None):
    """Call the LLM, retrying transient failures.

    Transient 429/503 are retried on the same model because the free tier
    reports brief throttling as a quota error that clears in seconds. A 404
    means the model name has been retired, so the next candidate is used
    instead of retrying.

    The total time spent retrying is capped so a rate-limited API never leaves
    the student waiting: once the budget is spent the caller falls back to the
    offline course-note response.
    """
    client = _get_client()
    if not client:
        return None

    if quota_blocked():
        # Already known that the allowance is spent. Failing now keeps the
        # student on the document answer straight away instead of waiting.
        return None

    if budget is None:
        try:
            budget = float(os.getenv('LLM_RETRY_BUDGET', '12'))
        except ValueError:
            budget = 12.0

    global _last_model
    models = _model_candidates()
    contents = (
        "You are an educational tutor for students. Answer only the question "
        "that is actually asked, in the fewest words that fully answer it. "
        "Never pad with restatements, filler, or offers of further help. Be "
        "accurate and encourage the student's own reasoning.\n\n"
        f"{prompt}"
    )
    started = time.monotonic()

    for index, model in enumerate(models):
        for attempt in range(attempts):
            try:
                r = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        max_output_tokens=max_tokens,
                        temperature=0.4,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    )
                )
                text = (r.text or '').strip()
                if text:
                    _last_model = model
                    return text
                # An empty completion is worth one more try.
                if attempt >= attempts - 1:
                    break
            except Exception as exc:
                # A model-level failure is checked first: retrying a retired
                # model or one the key has no quota for just burns the budget.
                if _swap_model(exc):
                    break
                if _quota_exhausted(exc):
                    # Spent for the day: stop all three candidates at once.
                    _mark_quota_exhausted()
                    return None
                if not _retryable(exc):
                    return None
                if attempt >= attempts - 1:
                    break
                remaining = budget - (time.monotonic() - started)
                if remaining <= 1:
                    return None
                time.sleep(min(_retry_delay(exc, 4.0 * (2 ** attempt)), remaining))
        # Out of budget on this model: stop rather than burn the whole budget on
        # every candidate.
        if budget - (time.monotonic() - started) <= 1:
            break
    return None

HINT_MODES = ('hint', 'answer')

STOPWORDS = {
    'how', 'what', 'why', 'which', 'where', 'when', 'who', 'the', 'a', 'an',
    'and', 'or', 'for', 'to', 'of', 'in', 'on', 'is', 'are', 'do', 'does',
    'write', 'solve', 'explain', 'using', 'with', 'this', 'that', 'question',
    'can', 'could', 'please', 'show', 'give', 'step', 'by', 'me', 'it', 'you',
    'code', 'program', 'want', 'need', 'i', 'we', 'my'
}

def _question_keywords(question: str, limit: int = 3):
    tokens = re.findall(r'[a-zA-Z0-9]+', question.lower())
    tokens = [t for t in tokens if len(t) > 2 and t not in STOPWORDS]
    freq = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    return [t for t, _ in sorted(freq.items(), key=lambda x: (x[1], len(x[0])), reverse=True)[:limit]]

def _fallback_hint(question: str, subject: str, docs) -> str:
    focus = _question_keywords(question)
    for d in docs:
        # Pointing a student at a page of example questions helps nobody, and
        # the answer history is a chat log rather than something to revise from.
        if re.search(r'example|question|exercise|practice', d.get('title', ''), re.I):
            continue
        if d.get('origin') == 'history':
            continue
        excerpt = _focused_excerpt(
            d.get('content', ''), question, 2, require_match=True
        )
        if not excerpt:
            continue
        page = d.get('page')
        where = f" (page {page})" if page else ""
        return (
            f'Hint: re-read "{d["title"]}"{where} for: {excerpt}\n'
            "Now identify which part of the problem uses that idea, then try it "
            "on a smaller example before answering."
        )
    topic = focus[0] if focus else 'the concept involved'
    return (
        f'Hint: break the question into smaller steps about "{topic}". '
        "List what is given and what is asked, recall the definition, then apply it "
        "step by step. The AI service is unavailable, so try answering and check manually."
    )

def _focused_excerpt(content: str, question: str, max_sentences: int = 3,
                     require_match: bool = False) -> str:
    """Keep only the sentences of a section that bear on the question.

    A matched section can be a page long while the student asked a single
    question about it. Quoting the whole section answers the wrong thing, so
    the sentences that share the question's keywords are kept and the rest is
    dropped.

    With ``require_match`` a section that shares nothing with the question
    yields nothing at all, rather than its opening sentences. That is what
    keeps a section of *example questions* from being presented as if it were
    the answer to the student's question.
    """
    text = ' '.join((content or '').split())
    if not text:
        return ''

    sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= max_sentences:
        return ' '.join(sentences)

    wanted = set(_question_keywords(question, limit=12))
    scored = []
    for i, sentence in enumerate(sentences):
        overlap = len(wanted & set(_question_keywords(sentence, limit=12)))
        # A sentence that is itself a question states the problem, it does not
        # answer it, so it only counts when there is nothing else to quote.
        if sentence.endswith('?'):
            overlap = 0
        scored.append((overlap, -i, i))
    scored.sort(reverse=True)

    keep = sorted(i for score, _, i in scored[:max_sentences] if score > 0)
    if not keep:
        if require_match:
            return ''
        keep = list(range(min(max_sentences, len(sentences))))
    return ' '.join(sentences[i] for i in keep)


def _fallback_document_answer(question, subject, docs):
    """Answer from the document text when the model is unavailable.

    The point of this panel is what the knowledge base says about the question
    that was asked, so only the matching sentences of each section are quoted
    rather than the sections in full.
    """
    parts = []
    for d in docs:
        # Sections of practice questions restate the problem; quoting them as
        # though they were the answer is worse than saying nothing.
        if re.search(r'example|question|exercise|practice',
                     d.get('title', ''), re.I):
            continue
        where = f" — {d.get('source')}" if d.get('source') else ''
        # Only a section that actually shares wording with the question can
        # answer it, and two is enough: more than that stops being an answer.
        excerpt = _focused_excerpt(d.get('content', ''), question, 3, require_match=True)
        if excerpt:
            parts.append(f"{d['title']}{where}\n{excerpt}")
        if len(parts) == 2:
            break
    if not parts:
        return (
            f"The {subject} material does not cover this question. Try naming "
            "the specific term you are stuck on."
        )
    body = '\n\n'.join(f'[{i}] {p}' for i, p in enumerate(parts, 1))
    return (
        f"{body}\n\nThe AI service is unavailable, so this is quoted straight "
        f"from the {subject} material."
    )


def tutor(db: Session, question: str, subject: str, mode: str):
    """Answer a student question.

    In ``answer`` mode two replies are produced from the same question:

    * ``answer`` — the model's own explanation, using general knowledge and no
      document context;
    * ``document_answer`` — a second, independent reply written only from the
      sections of the project's ``docs`` folder that match the question, with
      the sources it used attached.

    The two are generated by separate requests on purpose, so a student can
    see where the course material agrees with general knowledge and where it
    differs. Every exchange is appended to the knowledge base so the folder
    grows with what is actually asked.

    ``hint`` mode returns a single clue and deliberately withholds the answer.
    """
    mode = mode if mode in HINT_MODES else 'answer'
    question = (question or '').strip()
    subject = (subject or '').strip() or 'General'

    if not question:
        return {
            'mode': mode, 'answer': '', 'document_answer': '', 'hint': '',
            'sources': [], 'grounded': False, 'offline': True,
        }

    # Everything in the knowledge base is scored together: the docs folder
    # first, then the built-in course notes.
    entries = retrieve(db, question, subject, limit=4)
    docs = [
        {
            'title': e.get('title'),
            'content': e.get('content', ''),
            'page': e.get('page'),
            'subject': e.get('subject'),
            'source': e.get('source'),
        }
        for e in entries
    ]
    doc_sources = [
        {
            'title': d['title'],
            'page': d['page'] or '1',
            'subject': d['subject'],
            'file': d['source'],
        }
        for d in docs
    ]

    if mode == 'hint':
        prompt = (
            f"Subject: {subject}\nStudent question: {question}\n"
            "Give ONE focused hint for THIS exact question in 1-2 sentences. "
            "Do NOT give the full answer. Point to the key concept involved or "
            "ask one guiding question so the student can work it out themselves."
        )
        hint = llm_text(prompt, max_tokens=150)
        hint_from_llm = bool(hint)
        if not hint:
            hint = _fallback_hint(question, subject, entries)
        try:
            from .docstore import record_exchange
            record_exchange(question, subject, hint, doc_sources, kind='hint')
        except Exception:
            pass
        return {
            'mode': 'hint', 'answer': '', 'document_answer': '', 'hint': hint,
            'sources': doc_sources, 'grounded': bool(docs), 'offline': not hint,
            'llm_status': 'ok' if hint_from_llm else (
                'quota' if quota_blocked() else 'error'
            ),
        }

    # --- reply 1: the model on its own ---
    general_prompt = (
        f"Subject: {subject}\nQuestion: {question}\n"
        "Answer this exact question and nothing more, using your own knowledge "
        "of the subject. Rules: reply with at most 3 short sentences or a "
        "compact list; do not restate the question; do not add background the "
        "question did not ask for; do not add closing offers of further help."
    )
    answer = llm_text(general_prompt, max_tokens=220)
    llm_replied = bool(answer)
    # Answer mode must never read as a hint.
    stripped = re.sub(r'^\s*hint\s*[:\-]\s*', '', answer or '', flags=re.I)
    if stripped and not answer.lstrip().lower().startswith(
        ('give a hint', "here's a hint", 'here is a hint')
    ):
        answer = stripped

    # --- reply 2: the same question, answered from the documents only ---
    document_answer = ''
    if docs:
        context = '\n\n'.join(
            f"[{i}] {d['title']}" + (f" (page {d['page']})" if d.get('page') else '')
            + f"\n{d['content']}"
            for i, d in enumerate(docs, 1)
        )
        document_prompt = (
            f"Subject: {subject}\nQuestion: {question}\n\n"
            f"Course material:\n{context}\n\n"
            "Answer this exact question using ONLY the material above. Rules: "
            "at most 3 short sentences, or a compact list if the answer has "
            "parts; quote or closely paraphrase the material that answers the "
            "question and ignore the rest; cite the section number as [1] or "
            "[2]; if the material does not answer the question, reply exactly "
            "with 'Not covered in the course material.' and nothing else."
        )
        document_answer = llm_text(document_prompt, max_tokens=220)
        if document_answer:
            llm_replied = True
        if not document_answer:
            document_answer = _fallback_document_answer(question, subject, docs)
        if document_answer.lstrip().lower().startswith('hint:'):
            document_answer = re.sub(
                r'^\s*hint\s*[:\-]\s*', '', document_answer, flags=re.I
            )

    if not answer:
        if docs:
            # Fall back to the material, but only the part that answers the
            # question, so this reply is no longer than a real one.
            picked = []
            for d in docs:
                excerpt = _focused_excerpt(
                    d.get('content', ''), question, 2, require_match=True
                )
                if excerpt:
                    picked.append(excerpt)
                if len(picked) == 2:
                    break
            answer = (
                '\n'.join(picked)
                + "\n\nThe AI service is unavailable, so this is taken from "
                  f"the {subject} material."
            ) if picked else (
                f"The AI service is unavailable and the {subject} material did "
                "not cover this question."
            )
        else:
            answer = (
                f"No matching course material was found for \"{subject}\". "
                "Describe the problem with more detail (variables, rules, or "
                "steps you tried) and try again, or pick another subject."
            )

    try:
        from .docstore import record_exchange
        record_exchange(question, subject, answer, doc_sources, kind='answer')
    except Exception:
        # Losing the audit trail must never break the answer.
        pass

    return {
        'mode': 'answer',
        'answer': answer,
        'document_answer': document_answer,
        'hint': '',
        'sources': doc_sources,
        'grounded': bool(docs),
        'offline': not llm_replied,
        'llm_status': 'ok' if llm_replied else (
            'quota' if quota_blocked() else 'error'
        ),
    }


def make_question(db: Session, subject: str, topic: str, difficulty: str):
    docs=retrieve(db,topic,subject)
    context=' '.join(d.content for d in docs)
    prompt=f"Create one {difficulty} multiple-choice educational question for subject {subject}, topic {topic}. Context: {context}. Return JSON with question, options (4 strings), answer (exact option), explanation."
    raw=llm_text(prompt)
    if raw:
        try:
            data=json.loads(raw[raw.find('{'):raw.rfind('}')+1])
        except Exception:
            data=None
    else: data=None
    if not data:
        data={'question':f'Which statement best describes {topic}?','options':[f'{topic} is a core concept used in {subject}.',f'{topic} is unrelated to {subject}.','{topic} means only memorization.','None of the above.'],'answer':f'{topic} is a core concept used in {subject}.','explanation':f'{topic} should be understood from the course material rather than memorized without context.'}
    q=Question(subject=subject,topic=topic,difficulty=difficulty,question=data['question'],answer=data['answer'],options_json=json.dumps(data.get('options',[])))
    db.add(q); db.commit(); db.refresh(q)
    return q, data.get('explanation','')

def grade(db: Session, user_id:int, question:Question, answer:str, hints:int):
    correct=answer.strip().lower()==question.answer.strip().lower()
    score=100 if correct else 0
    feedback='Correct. Good work.' if correct else f"Not quite. Review the topic '{question.topic}' and try again."
    a=Attempt(user_id=user_id,question_id=question.id,user_answer=answer,score=score,hints_used=hints,feedback=feedback)
    db.add(a); db.commit()
    return score, feedback


# --------------------------------------------------
# Weak-topic quiz: LLM generates the full question set
# --------------------------------------------------

_FENCE = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*$', re.IGNORECASE)


def _extract_json(raw: str):
    """Pull the outermost JSON array (or object) out of an LLM reply."""
    if not raw:
        return None
    text = _FENCE.sub('', raw.strip())
    for opener, closer in (('[', ']'), ('{', '}')):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    return None


def _as_option_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = re.split(r'\s*(?:\||\n)\s*', value)
        return [p.strip(' -•\t') for p in parts if p.strip(' -•\t')]
    return []


def _clean_item(item, topic=None):
    """Normalise one LLM question. Returns None if it is not usable.

    The answer must equal one of the options exactly, otherwise automatic
    grading silently breaks, so anything that cannot be repaired is dropped.
    """
    if not isinstance(item, dict):
        return None
    text = str(item.get('question') or '').strip()
    options = _as_option_list(item.get('options'))
    answer = str(item.get('answer') or '').strip()
    if not text or len(options) < 2 or not answer:
        return None

    match = None
    for opt in options:
        if opt.lower() == answer.lower():
            match = opt
            break
    if match is None:
        for opt in options:
            if answer.lower() in opt.lower() or opt.lower() in answer.lower():
                match = opt
                break
    if match is None and len(options) == 1:
        match = options[0]
    if match is None:
        return None

    # De-duplicate options while preserving the correct answer.
    seen = set()
    unique = []
    for opt in options:
        key = opt.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(opt)
    if match not in unique:
        unique.append(match)

    part = match_subtopic(item.get('part'), topic) if topic else None

    return {
        'question': text,
        'options': unique[:4] if len(unique) <= 4 else unique,
        'answer': match,
        'explanation': str(item.get('explanation') or '').strip(),
        'subtopic': part,
    }


_TEMPLATES = [
    ('Which statement best describes {topic}?',
     ['{topic} is a core concept used throughout {subject}.',
      '{topic} is unrelated to {subject}.',
      '{topic} only matters for memorisation, never for practice.',
      '{topic} is always optional in {subject}.'],
     0),
    ('What is the main purpose of {topic} in {subject}?',
     ['To solve a specific class of problems in {subject}.',
      'To store passwords safely.',
      'To replace the need for testing.',
      'To measure internet speed.'],
     0),
    ('Which one is NOT a characteristic of {topic}?',
     ['It applies only to large datasets.',
      'It is explained in the {subject} course notes.',
      'It is assessed in quizzes.',
      'It has nothing to do with {subject}.'],
     3),
    ('Why is {topic} important when learning {subject}?',
     ['Because later {subject} work depends on it.',
      'Because it replaces the need for practice.',
      'Because it is unrelated to later work.',
      'Because it is only needed for memorisation.'],
     0),
    ('Which statement about {topic} is correct?',
     ['It should be applied with an understanding of when it fits.',
      'It should be memorised without context.',
      'It never appears in assessments.',
      'It only applies outside {subject}.'],
     0),
    ('When you face a {subject} problem, how should you use {topic}?',
     ['Identify where it applies, then apply it step by step.',
      'Skip it and guess the answer.',
      'Use it only after the deadline.',
      'Ignore it entirely.'],
     0),
    ('A learner struggles with {topic}. What is the best next step?',
     ['Review the {subject} notes for {topic}, then practise {topic} questions.',
      'Move on to an unrelated subject.',
      'Stop practising permanently.',
      'Wait for the next exam without revising.'],
     0),
    ('Which topic must be understood before {topic} feels complete?',
     ['The supporting ideas from earlier in {subject}.',
      'Nothing at all.',
      'Only unrelated material.',
      'Advanced topics never help.'],
     0),
    ('How is {topic} best checked for understanding?',
     ['By attempting quiz questions on {topic} and reviewing mistakes.',
      'By reading the title page only.',
      'By skipping all feedback.',
      'By avoiding practice.'],
     0),
    ('Complete the statement: {topic} in {subject} is best described as',
     ['a concept you apply to solve problems.',
      'a random fact with no use.',
      'a topic that is never tested.',
      'something only teachers need.'],
     0),
]


def _offline_items(topic: str, subject: str, count: int, offset: int = 0, parts=None):
    """Deterministic stand-in used when the LLM is unavailable.

    Keeps the demo fully playable: every item still has exactly one option
    that matches its answer, so automatic grading and topic scoring work, and
    each item is tagged with the sub-part it covers.
    """
    parts = parts or []
    items = []
    for i in range(count):
        part = parts[i % len(parts)] if parts else None
        label = part or topic
        stem, options, correct_idx = _TEMPLATES[(offset + i) % len(_TEMPLATES)]
        text = stem.format(topic=label, subject=subject)
        if part and part != topic:
            text = f'[{part}] {text}'
        filled = [o.format(topic=label, subject=subject) for o in options]
        answer = filled[correct_idx % len(filled)]
        # Rotate the options so the correct one is not always first. The answer
        # is carried as text, so rotating the list cannot invalidate it.
        shift = (offset + i) % len(filled)
        rotated = filled[shift:] + filled[:shift]
        items.append({
            'question': text,
            'options': rotated,
            'answer': answer,
            'explanation': (
                f'Read the {subject} course notes on {topic}'
                + (f' (see the "{part}" section)' if part else '')
                + ', then work through the explanation for each missed question.'
            ),
            'subtopic': part,
        })
    return items


def _plan_parts(topic: str, count: int):
    """Spread `count` questions evenly across the topic's sub-parts."""
    parts = subtopics_of(topic)
    if not parts:
        return []
    # Repeat the cycle when there are fewer sub-parts than questions so every
    # sub-part is still covered.
    plan = [parts[i % len(parts)] for i in range(count)]
    return plan


def _generate_items(db: Session, subject: str, topic: str, difficulty: str, count: int, offset: int = 0):
    """Ask the LLM for `count` questions, falling back to the offline set."""
    docs = retrieve(db, topic, subject, limit=4)
    context = ' '.join(
        f"[{d['title']}, page {d.get('page') or 1}] {d['content']}" for d in docs
    ) or (
        f'No course note matched "{topic}". Use standard {subject} fundamentals.'
    )

    plan = _plan_parts(topic, count)
    parts = subtopics_of(topic)
    if parts:
        coverage = (
            f'The topic "{topic}" has these sub-parts: '
            + ', '.join(parts)
            + '. Cover every sub-part, spreading the questions across them.'
        )
        part_rule = (
            ' Add a "part" key naming which sub-part the question tests, '
            'copied exactly from the list above.'
        )
    else:
        coverage = ''
        part_rule = ''

    prompt = (
        f"Create exactly {count} distinct multiple-choice questions for subject "
        f"{subject}, topic \"{topic}\", difficulty {difficulty}. "
        f"They form one graded quiz, so do not repeat a question.\n"
        f"{coverage}\n"
        f"Course context:\n{context}\n"
        'Return ONLY a JSON array. Each element must be an object with keys '
        '"question" (string), "options" (array of exactly 4 strings), '
        '"answer" (string copied exactly from one of the options) and '
        f'"explanation" (one short sentence).{part_rule} No markdown, no commentary.'
    )
    raw = llm_text(prompt, max_tokens=2000)

    parsed = _extract_json(raw)
    if isinstance(parsed, dict):
        parsed = parsed.get('questions') or parsed.get('quiz') or []
    if not isinstance(parsed, list):
        parsed = []

    items = []
    seen = set()
    for entry in parsed:
        cleaned = _clean_item(entry, topic)
        if not cleaned:
            continue
        key = cleaned['question'].lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(cleaned)
        if len(items) == count:
            break

    if len(items) < count:
        have = {i['question'].lower() for i in items}
        for extra in _offline_items(topic, subject, count - len(items), len(items)):
            if extra['question'].lower() not in have:
                items.append(extra)

    # Any question the LLM left untagged gets the planned sub-part for its slot,
    # and an untagged question is matched to whichever planned part it fits best.
    used = {i['subtopic'] for i in items if i.get('subtopic')}
    spare = [p for p in plan if p not in used]
    for index, item in enumerate(items):
        if item.get('subtopic'):
            continue
        if not spare:
            break
        # Prefer a part whose words appear in the question text.
        picked = None
        words = set(re.findall(r'[a-z0-9]+', item['question'].lower()))
        for part in spare:
            overlap = words & set(re.findall(r'[a-z0-9]+', part.lower()))
            if overlap:
                picked = part
                break
        item['subtopic'] = picked or spare.pop(0)
        if picked:
            spare.remove(picked)

    return items, not bool(raw)


def create_quiz(db: Session, user_id: int, course: dict, topic: str, difficulty: str = 'medium', count: int = 10):
    """Create a weak-topic quiz row plus its `count` question rows."""
    subject = course['subject']
    items, offline = _generate_items(db, subject, topic, difficulty, count)

    quiz = TopicQuiz(
        user_id=user_id,
        course_code=course['code'],
        course_name=course['name'],
        subject=subject,
        topic=topic,
        total=len(items),
        correct=0,
        answered=0,
        status='in_progress',
    )
    db.add(quiz)
    db.commit()
    db.refresh(quiz)

    for item in items:
        db.add(Question(
            subject=subject,
            topic=topic,
            difficulty=difficulty,
            question=item['question'],
            answer=item['answer'],
            options_json=json.dumps(item['options']),
            explanation=item.get('explanation', ''),
            subtopic=item.get('subtopic'),
            quiz_id=quiz.id,
        ))
    db.commit()
    db.refresh(quiz)
    return quiz, offline


def quiz_questions(db: Session, quiz: TopicQuiz):
    rows = (db.query(Question)
            .filter_by(quiz_id=quiz.id)
            .order_by(Question.id.asc())
            .all())
    return rows


def grade_quiz_answer(db: Session, quiz: TopicQuiz, question: Question, answer: str, hints: int = 0):
    """Grade one answer, keeping the quiz counters and the attempts table in sync."""
    existing = (db.query(Attempt)
                .filter_by(user_id=quiz.user_id, question_id=question.id)
                .order_by(Attempt.id.desc())
                .first())
    if existing:
        # Already answered in this quiz — never count the same question twice.
        return int(existing.score), existing.feedback, True

    score, feedback = grade(db, quiz.user_id, question, answer, hints)
    quiz.answered = (quiz.answered or 0) + 1
    quiz.correct = (quiz.correct or 0) + (1 if score == 100 else 0)
    db.commit()
    return score, feedback, False
