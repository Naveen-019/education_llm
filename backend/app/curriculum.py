"""Hardcoded course -> topic learning structure.

The learning path is delivered one course at a time and, inside a course, one
weak topic at a time. The curriculum order declared here is the order the
backend uses when it builds a student's sequential learning plan.
"""

import re

# Rules of the weak-topic quiz loop
PASS_MARK = 80          # percentage needed to clear a weak topic
WEAK_THRESHOLD = 80     # topic accuracy below this counts as weak
QUIZ_LENGTH = 10        # questions generated per weak-topic quiz
DAILY_QUIZ_LIMIT = 1    # quizzes a student may start per calendar day

COURSES = [
    {
        'code': 'programming',
        'name': 'Programming Fundamentals',
        'subject': 'Programming',
        'description': 'Core Python and programming concepts used across the course notes.',
        'topics': [
            'Functions',
            'Data Structures',
            'OOP',
            'Decorators',
            'Error Handling',
        ],
    },
    {
        'code': 'dbms',
        'name': 'Database Management Systems',
        'subject': 'DBMS',
        'description': 'Relational modelling, SQL and storage fundamentals.',
        'topics': [
            'Keys and Relationships',
            'Normalization',
            'SQL Joins',
            'Indexing',
            'Transactions',
        ],
    },
    {
        'code': 'bigdata',
        'name': 'Big Data Analytics',
        'subject': 'Big Data',
        'description': 'Storage, processing and querying engines for large datasets.',
        'topics': [
            'HDFS',
            'MapReduce',
            'Hive',
            'Spark',
        ],
    },
    {
        'code': 'ai',
        'name': 'Artificial Intelligence',
        'subject': 'AI',
        'description': 'Learning algorithms, model evaluation and intelligent systems.',
        'topics': [
            'Machine Learning',
            'Regression',
            'Classification',
            'Clustering',
            'Model Evaluation',
            'Search Algorithms',
            'Intelligent Agents',
            'Knowledge Representation',
            'Natural Language Processing',
        ],
    },
]

COURSE_BY_CODE = {c['code']: c for c in COURSES}

_TOPIC_INDEX = []
for _course in COURSES:
    for _pos, _topic in enumerate(_course['topics']):
        _TOPIC_INDEX.append((_course['code'], _pos, _topic))


def _norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


_NORM_TOPIC_INDEX = [
    (code, pos, topic, _norm(topic)) for code, pos, topic in _TOPIC_INDEX
]


def match_topic(raw_topic):
    """Map a free-text topic from the questions table to a curriculum topic.

    Returns ``(course_code, canonical_topic)`` or ``None`` when the topic does
    not belong to any course in the curriculum.
    """
    needle = _norm(raw_topic)
    if not needle:
        return None

    for code, _pos, topic, norm in _NORM_TOPIC_INDEX:
        if norm == needle:
            return code, topic

    alias = _ALIAS_INDEX.get(needle)
    if alias:
        return alias

    substring = [
        (len(norm), code, topic)
        for code, _pos, topic, norm in _NORM_TOPIC_INDEX
        if needle in norm or norm in needle
    ]
    if substring:
        substring.sort(reverse=True)
        _length, code, topic = substring[0]
        return code, topic

    tokens = {t for t in needle.split() if len(t) > 2}
    best = None
    for code, _pos, topic, norm in _NORM_TOPIC_INDEX:
        topic_tokens = set(norm.split())
        shared = tokens & topic_tokens
        if not shared:
            continue
        score = len(shared) / len(topic_tokens)
        if best is None or score > best[0]:
            best = (score, code, topic)
    if best:
        return best[1], best[2]
    return None


def course_topic_position(course_code, topic):
    course = COURSE_BY_CODE.get(course_code)
    if not course:
        return 999
    norm = _norm(topic)
    for pos, name in enumerate(course['topics']):
        if _norm(name) == norm:
            return pos
    return 999


# Free-text labels that should resolve to a curriculum topic but are too far
# from the canonical name for substring or token matching to catch them.
TOPIC_ALIASES = {
    'object oriented programming': 'OOP',
    'objects and classes': 'OOP',
    'classes and objects': 'OOP',
    'oops': 'OOP',
    'primary key': 'Keys and Relationships',
    'foreign key': 'Keys and Relationships',
    'keys': 'Keys and Relationships',
    'relationships': 'Keys and Relationships',
    'er diagram': 'Keys and Relationships',
    'normal forms': 'Normalization',
    '1nf': 'Normalization',
    'acid': 'Transactions',
    'acidity': 'Transactions',
    'exceptions': 'Error Handling',
    'exception': 'Error Handling',
    'exceptions handling': 'Error Handling',
    'inner join': 'SQL Joins',
    'outer join': 'SQL Joins',
    'join': 'SQL Joins',
    'indexes': 'Indexing',
    'indices': 'Indexing',
    'index': 'Indexing',
    'lists': 'Data Structures',
    'trees': 'Data Structures',
    'graphs': 'Data Structures',
    'arrays': 'Data Structures',
    'linked lists': 'Data Structures',
    'linked list': 'Data Structures',
    'queues': 'Data Structures',
    'stacks': 'Data Structures',
    'stack': 'Data Structures',
    'queue': 'Data Structures',
    'search algorithms': 'Data Structures',
    'sorting algorithms': 'Data Structures',
    'variables and data types': 'Data Structures',
    'linear regression': 'Regression',
    'logistic regression': 'Regression',
    'decision trees': 'Classification',
    'supervised learning': 'Machine Learning',
    'unsupervised learning': 'Machine Learning',
    'supervised learning algorithms': 'Machine Learning',
    'methods': 'Functions',
    'method': 'Functions',
    'function': 'Functions',
    'distributed storage': 'HDFS',
    'map reduce': 'MapReduce',
    'apache spark': 'Spark',
    'hiveql': 'Hive',
}

_ALIAS_INDEX = {}
for _course in COURSES:
    for _topic in _course['topics']:
        _ALIAS_INDEX[_norm(_topic)] = (_course['code'], _topic)
for _alias, _topic in TOPIC_ALIASES.items():
    _ALIAS_INDEX.setdefault(_norm(_alias), (None, _topic))

# Resolve an alias to its course by looking the canonical topic back up.
_ALIAS_INDEX = {
    alias: next(
        (
            (code, topic)
            for code, _pos, topic in _TOPIC_INDEX
            if topic == target
        ),
        (None, target),
    )
    for alias, (_code, target) in _ALIAS_INDEX.items()
}


def required_correct(total=QUIZ_LENGTH):
    """Number of correct answers needed to hit the pass mark."""
    import math
    return math.ceil(total * PASS_MARK / 100)


# Sub-parts of each topic. A weak topic is broken down into these so a quiz can
# show which specific part of the topic the student is being tested on, and so
# a result can be broken down per part.
SUBTOPICS = {
    # Programming
    'Functions': ['Parameters and Arguments', 'Return Values', 'Scope', 'Lambda Functions'],
    'Data Structures': ['Arrays', 'Linked Lists', 'Stacks', 'Queues', 'Trees and Graphs'],
    'OOP': ['Classes and Objects', 'Inheritance', 'Encapsulation', 'Polymorphism'],
    'Decorators': ['Function Wrappers', '@decorator Syntax', 'Arguments and *args', 'Class Decorators'],
    'Error Handling': ['try and except', 'Raising Exceptions', 'Custom Exceptions', 'finally Block'],

    # DBMS
    'Keys and Relationships': ['Primary Key', 'Foreign Key', 'Composite Key', 'ER Diagram'],
    'Normalization': ['1NF', '2NF', '3NF', 'BCNF'],
    'SQL Joins': ['INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'FULL OUTER JOIN'],
    'Indexing': ['B-Tree Index', 'Hash Index', 'Index Selection', 'Write Cost'],
    'Transactions': ['ACID Properties', 'Isolation Levels', 'Commit and Rollback', 'Deadlocks'],

    # Big Data
    'HDFS': ['Blocks and Blocksize', 'Replication Factor', 'NameNode and DataNode', 'Rack Awareness'],
    'MapReduce': ['Mapper and Reducer', 'Shuffle and Sort', 'Partitioning', 'Word Count Example'],
    'Hive': ['Partitions', 'Buckets', 'HiveQL Basics', 'SerDe and Tables'],
    'Spark': ['RDDs', 'DataFrames', 'Transformations and Actions', 'Lazy Evaluation'],

    # AI
    'Machine Learning': ['Supervised Learning', 'Unsupervised Learning', 'Training vs Test Split', 'Features and Labels'],
    'Regression': ['Linear Regression', 'Gradient Descent', 'Loss Function', 'R-squared'],
    'Classification': ['Decision Trees', 'Logistic Regression', 'Accuracy vs Precision', 'Confusion Matrix'],
    'Clustering': ['k-Means', 'Centroids', 'Choosing k', 'Distance Metrics'],
    'Model Evaluation': ['Precision', 'Recall', 'F1 Score', 'Train/Test Overfitting'],
    'Search Algorithms': ['Breadth-First Search', 'Depth-First Search', 'Heuristic Search', 'State Space'],
    'Intelligent Agents': ['Sensors and Actuators', 'Agent Environment', 'Rational Agent', 'Decision Making'],
    'Knowledge Representation': ['Production Rules', 'Semantic Networks', 'Frames', 'Inference'],
    'Natural Language Processing': ['Tokenization', 'Parsing', 'Bag of Words', 'Sentiment Analysis'],
}


def subtopics_of(topic):
    """Sub-parts of a curriculum topic (empty list when it has none)."""
    return SUBTOPICS.get(topic, [])


def _subtopic_index():
    index = {}
    for topic, parts in SUBTOPICS.items():
        for part in parts:
            index.setdefault(_norm(part), (topic, part))
    return index


_SUBTOPIC_INDEX = _subtopic_index()


def match_subtopic(raw, topic):
    """Map a free-text sub-part name onto one of ``topic``'s sub-parts."""
    needle = _norm(raw)
    parts = subtopics_of(topic)
    if not needle or not parts:
        return None
    for part in parts:
        if _norm(part) == needle:
            return part
    for part in parts:
        pnorm = _norm(part)
        if needle in pnorm or pnorm in needle:
            return part
    # A name that is a known sub-part of a *different* topic still resolves,
    # as long as it is not ambiguous within this topic.
    for part in parts:
        if _norm(part).startswith(needle) or needle.startswith(_norm(part)):
            return part
    found = _SUBTOPIC_INDEX.get(needle)
    if found and found[0] == topic:
        return found[1]
    return None
