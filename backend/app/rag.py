import re
from sqlalchemy.orm import Session
from .models import Document

SEED_DOCS = [
    ('Hadoop Basics', 'Big Data', 'HDFS stores large files across multiple machines. Data is split into blocks and replicated for fault tolerance. Data locality allows computation to run close to where data is stored.', '1'),
    ('Hive Basics', 'Big Data', 'Apache Hive is a data warehouse system for querying and analyzing large datasets using HiveQL. Partitioning divides a table into parts based on column values, while bucketing groups rows into a fixed number of files using a hash of a column.', '2'),
    ('Python FastAPI', 'Programming', 'FastAPI is a Python framework for building APIs using type hints. A route maps an HTTP method and URL path to a Python function. Pydantic models validate request data.', '1'),
    ('Database Fundamentals', 'DBMS', 'A primary key uniquely identifies a row. A foreign key connects one table to another. Normalization reduces unnecessary duplication and improves data integrity.', '3'),
    ('Machine Learning Basics', 'AI', 'Machine learning trains a model on examples instead of hand-written rules. Supervised learning uses labelled data, while unsupervised learning finds structure in unlabelled data. A model is trained on training data and evaluated on held-out test data.', '1'),
    ('Regression and Classification', 'AI', 'Regression predicts a continuous value, for example gradient descent optimises a loss function to minimise the error of a prediction. Classification predicts a discrete label, for example a decision tree that splits data on feature values to separate classes.', '2'),
    ('Clustering and Evaluation', 'AI', 'Clustering groups similar data points together, for example the k-means algorithm that assigns points to k centroids and iterates until the assignments stabilise. Model evaluation measures performance using accuracy, precision and recall, and the confusion matrix shows true and false positives and negatives.', '3'),
    ('Search and Intelligent Agents', 'AI', 'Breadth-first search explores a search space level by level while depth-first search follows one branch as deep as possible before backtracking. An intelligent agent perceives its environment through sensors and acts through actuators. Knowledge representation encodes facts and rules, for example in production rule systems used for natural language processing.', '4'),
]

def seed_documents(db: Session):
    if db.query(Document).count() == 0:
        for title, subject, content, page in SEED_DOCS:
            db.add(Document(title=title, subject=subject, content=content, source_page=page))
        db.commit()

SUBJECT_ALIASES = {
    'programming': {'python', 'data structures'},
}

# Uploaded notes are rarely tagged with the exact course name, so their subject
# is mapped onto one of the four courses. Without this a document tagged
# "Data Structures" or "Machine Learning" was filtered out of every retrieval
# and the tutor reported "no matching course material".
DOC_SUBJECT_MAP = {
    'python': 'Programming',
    'data structures': 'Programming',
    'functions': 'Programming',
    'oop': 'Programming',
    'decorators': 'Programming',
    'error handling': 'Programming',
    'programming': 'Programming',

    'dbms': 'DBMS',
    'database': 'DBMS',
    'database management': 'DBMS',
    'sql': 'DBMS',
    'normalization': 'DBMS',
    'indexing': 'DBMS',
    'transactions': 'DBMS',
    'keys': 'DBMS',

    'hadoop': 'Big Data',
    'big data': 'Big Data',
    'bigdata': 'Big Data',
    'hdfs': 'Big Data',
    'mapreduce': 'Big Data',
    'map reduce': 'Big Data',
    'hive': 'Big Data',
    'spark': 'Big Data',

    'ai': 'AI',
    'artificial intelligence': 'AI',
    'machine learning': 'AI',
    'ml': 'AI',
    'regression': 'AI',
    'classification': 'AI',
    'clustering': 'AI',
    'neural': 'AI',
    'deep learning': 'AI',
    'nlp': 'AI',
    'natural language': 'AI',
}

COURSE_SUBJECTS = ('Programming', 'DBMS', 'Big Data', 'AI')


def normalize_subjects(subject):
    """Course names a document tagged `subject` should be retrievable under."""
    if not subject:
        return None
    key = subject.strip().lower()
    canonical = DOC_SUBJECT_MAP.get(key)
    if canonical:
        return {canonical.lower()}
    return {key} | SUBJECT_ALIASES.get(key, set())


def document_courses(subject):
    """Best-matching course for a document's subject tag, heading or filename.

    Matching is case-insensitive and handles multi-word names, so both
    "Artificial Intelligence" and "hive_notes" resolve correctly. When a piece
    of text mentions several courses, the one mentioned *first* wins, which is
    what a reader would treat as the topic of that section.

    Returns an empty list when nothing is recognised, which lets the caller
    fall back to a neutral subject instead of inventing one from the
    capitalised name.
    """
    if not subject:
        return []
    text = ' '.join(_TOKEN.findall(subject.lower()))
    if not text:
        return []

    # Longest keys first so "artificial intelligence" wins over a stray "ai".
    candidates = sorted(COURSE_SUBJECTS, key=len, reverse=True)
    candidates += sorted(DOC_SUBJECT_MAP, key=len, reverse=True)

    best = None
    for key in candidates:
        course = key if key in COURSE_SUBJECTS else DOC_SUBJECT_MAP[key]
        if ' ' in key:
            found = text.find(key)
        else:
            # A single-word key must match a whole word, not a substring, so
            # "ai" does not fire on "chain" or "maintain".
            found = -1
            for match in re.finditer(rf'\b{re.escape(key)}\b', text):
                found = match.start()
                break
        if found >= 0 and (best is None or found < best[0]):
            best = (found, course)
    return [best[1]] if best else []

_TOKEN = re.compile(r'[a-z0-9]+')

# Words that appear in most course notes carry no retrieval signal, so they are
# ignored. Without this, a note about stacks matches a question about search
# simply because both mention "search" or "data".
_STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'but', 'by', 'can', 'do', 'does',
    'for', 'from', 'has', 'have', 'how', 'i', 'if', 'in', 'into', 'is', 'it',
    'its', 'of', 'on', 'or', 'our', 'so', 'that', 'the', 'their', 'then',
    'there', 'these', 'they', 'this', 'to', 'was', 'we', 'were', 'what',
    'when', 'where', 'which', 'while', 'who', 'why', 'will', 'with', 'would',
    'you', 'your', 'notes', 'note', 'students', 'student', 'material',
    'educational', 'introductory', 'provide', 'provides', 'these', 'topic',
    'example', 'examples', 'using', 'used', 'use', 'about', 'also', 'any',
}


def _tokens(text):
    return set(_TOKEN.findall((text or '').lower())) - _STOPWORDS


def retrieve(db: Session, query: str, subject: str | None = None, limit=3):
    """Rank documents and course files against the query.

    Two sources are searched together: the rows in the ``documents`` table
    (the built-in course notes) and the files in the project's ``docs``
    folder, which is the primary knowledge base. Both are scored with the same
    inverse-document-frequency weighting, so a section from ``docs`` competes
    fairly with a seeded note.

    Rare words count for much more than common ones, and a hit in the title
    counts double, so the section that is actually *about* the question wins
    instead of whichever one happens to share a common word with it.

    Subject filtering is a soft preference rather than a hard gate: a document
    whose tag does not match the course is only skipped when nothing relevant
    was found otherwise, so a mismatched or missing tag can never make a
    relevant note invisible to the tutor.
    """
    from . import docstore  # imported lazily: docstore depends on this module

    words = _tokens(query)
    if not words:
        return []

    entries = []
    for row in db.query(Document).all():
        entries.append({
            'id': f'db:{row.id}',
            'title': row.title or '',
            'content': row.content or '',
            'subject': row.subject or '',
            'source': 'Built-in course notes',
            'page': row.source_page,
            'origin': 'db',
        })
    for chunk in docstore.index()[0]:
        entries.append({
            'id': chunk['id'],
            'title': chunk['title'],
            'content': chunk['content'],
            'subject': chunk['subject'],
            'source': chunk['source'],
            'page': None,
            'origin': chunk.get('origin', 'file'),
        })

    if not entries:
        return []

    subs = normalize_subjects(subject)
    title_tokens = {e['id']: _tokens(e['title']) for e in entries}
    body_tokens = {e['id']: _tokens(e['content']) for e in entries}

    # A term present in most documents discriminates nothing.
    frequency = {}
    for terms in body_tokens.values():
        for term in terms:
            frequency[term] = frequency.get(term, 0) + 1
    ceiling = max(1, len(entries) * 0.6)

    def weight(term):
        return 1.0 / (1.0 + frequency.get(term, 0) / ceiling)

    scored = []
    fallback = []
    for entry in entries:
        body = body_tokens[entry['id']]
        title = title_tokens[entry['id']]
        total = 0.0
        for term in words:
            if term in body:
                total += weight(term)
            if term in title:
                total += weight(term) * 2
        if total <= 0:
            continue
        if subs and entry['subject'].strip().lower() not in subs:
            fallback.append((total, entry))
        else:
            scored.append((total, entry))

    if not scored and fallback:
        scored = fallback

    # Past answers are a supplement to the notes, never a peer of them: a
    # recorded answer can quote the material almost verbatim, so scoring alone
    # would let it displace the section it was written from. Course material is
    # ranked as one block first, with history filling whatever room is left.
    scored.sort(key=lambda x: (x[1].get('origin') == 'history', -x[0]))
    return [dict(entry, score=round(total, 3)) for total, entry in scored[:limit]]
