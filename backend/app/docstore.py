"""The course knowledge base: everything under the project's ``docs`` folder.

The folder is the single source of truth for document-grounded answers. Files
are read live rather than mirrored into the database, so editing or replacing a
file takes effect on the next question with no reindex step.

Supported: ``.docx``, ``.md``, ``.txt``, ``.pdf``.

Every file is split into chunks along its own headings, because a knowledge
base written as "Artificial Intelligence / 1. Definition / 2. Applications" is
only useful for retrieval if each numbered section can be recalled on its own.
"""

import os
import re
import threading
import time
from pathlib import Path

from .rag import document_courses

DEFAULT_DOCS_DIR = Path(__file__).resolve().parents[2] / 'docs'

SUPPORTED_SUFFIXES = {'.docx', '.md', '.markdown', '.txt', '.pdf', '.rst', '.csv'}

# Long enough to hold a numbered section, short enough to stay in the prompt.
CHUNK_WORDS = 260
OVERLAP_WORDS = 50

# Where tutor answers are appended so the knowledge base grows with use.
HISTORY_FILE = 'tutor_history.md'

_lock = threading.Lock()
_cache = {'stamp': None, 'chunks': [], 'files': []}


def docs_dir():
    """The configured docs folder, created if it is missing."""
    raw = os.getenv('DOCS_PATH', '').strip()
    path = Path(raw).expanduser() if raw else DEFAULT_DOCS_DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _stamp(path: Path):
    """Cheap fingerprint of the folder: names, sizes and modification times."""
    parts = []
    try:
        for entry in sorted(path.rglob('*')):
            if entry.is_file() and not entry.name.startswith('.~lock'):
                stat = entry.stat()
                parts.append((str(entry), stat.st_size, int(stat.st_mtime)))
    except OSError:
        return None
    return tuple(parts)


# ---------------------------------------------------------------- reading

def _read_docx(path: Path):
    """Return [{'heading', 'level', 'text'}] using the document's own styles.

    Word heading levels are preserved, because in a course knowledge base a
    level-1 heading names the subject and a level-2 heading is one of its
    numbered sections. Keeping the level is what lets each section be filed
    under the right course.
    """
    try:
        import docx
    except ImportError:
        return []
    try:
        document = docx.Document(str(path))
    except Exception:
        return []

    sections = []
    heading = path.stem
    parent = ''
    level = 0
    buffer = []

    def flush():
        text = '\n'.join(buffer).strip()
        if text:
            sections.append({
                'heading': heading, 'parent': parent, 'level': level, 'text': text,
            })
        buffer.clear()

    for para in document.paragraphs:
        text = (para.text or '').strip()
        if not text:
            continue
        style = (para.style.name or '') if para.style is not None else ''
        match = re.match(r'Heading\s+(\d+)', style, re.I)
        if match:
            flush()
            level = int(match.group(1))
            if level <= 1:
                # A top-level heading names the subject its children sit under.
                parent = text
            heading = text
        elif style.lower() == 'title':
            flush()
            level = 0
            parent = text
            heading = text
        elif re.match(r'^#{1,6}\s+\S', text):
            flush()
            level = len(text) - len(text.lstrip('#'))
            if level <= 1:
                parent = text
            heading = text.lstrip('# ').strip()
        else:
            buffer.append(text)
    flush()
    return sections


def _read_pdf(path: Path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    try:
        reader = PdfReader(str(path))
    except Exception:
        return []
    sections = []
    for number, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or '').strip()
        except Exception:
            text = ''
        if text:
            sections.append({
                'heading': f'{path.stem} (page {number})',
                'parent': '',
                'level': 1,
                'text': text,
            })
    return sections


def _read_plain(path: Path):
    try:
        text = path.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return []
    if not text:
        return []

    # Markdown and plain text both use '#' headings; split on them so each
    # section is retrievable on its own.
    sections = []
    heading = path.stem
    parent = ''
    level = 0
    buffer = []

    def flush():
        body = '\n'.join(buffer).strip()
        if body:
            sections.append({
                'heading': heading, 'parent': parent, 'level': level, 'text': body,
            })
        buffer.clear()

    for line in text.splitlines():
        match = re.match(r'^\s{0,3}(#{1,6})\s+(\S.*)$', line)
        if match:
            flush()
            level = len(match.group(1))
            if level <= 1:
                parent = match.group(2).strip()
            heading = match.group(2).strip()
        else:
            buffer.append(line)
    flush()
    return sections


def read_file(path: Path):
    """Return a list of ``{heading, text, subject}`` sections.

    ``subject`` is resolved per section rather than per file, because a single
    knowledge base can cover several courses: "Artificial Intelligence" is a
    heading followed by its own numbered sections, and each of those sections
    must be retrievable under AI and not under DBMS.
    """
    suffix = path.suffix.lower()
    if suffix == '.docx':
        sections = _read_docx(path)
    elif suffix == '.pdf':
        sections = _read_pdf(path)
    else:
        sections = _read_plain(path)
    return _assign_subjects(sections, path)


def _assign_subjects(sections, path: Path):
    """Carry the nearest enclosing course heading down onto each section.

    A level-1 heading that names a course ("DBMS") opens a run of level-2
    sections that all belong to it, so "3. Advantages" is filed under DBMS
    rather than under whichever course happened to be mentioned first
    elsewhere in the file.
    """
    stem_courses = document_courses(
        path.stem.replace('_', ' ').replace('-', ' ')
    )
    current = stem_courses[0] if stem_courses else 'General'
    out = []

    for section in sections:
        heading = section.get('heading', '')
        parent = section.get('parent', '')
        guess = document_courses(parent) or document_courses(heading)
        if guess:
            current = guess[0]

        label = heading
        if current != 'General' and parent and parent != heading:
            label = f'{parent} — {heading}'
        elif current != 'General' and not parent:
            label = heading if heading and document_courses(heading) == [current] \
                else f'{current} — {heading}'

        out.append({
            'heading': heading,
            'label': label or current,
            'level': section.get('level', 0),
            'subject': current,
            'text': section.get('text', ''),
        })
    return out


# ---------------------------------------------------------------- chunking

def _sentences(text):
    return [s for s in re.split(r'(?<=[.!?])\s+', text) if s]


def chunk_section(text, chunk_words=CHUNK_WORDS, overlap=OVERLAP_WORDS):
    text = re.sub(r'[ \t]+', ' ', (text or '').strip())
    if not text:
        return []
    chunks = []
    current = []
    size = 0
    for sentence in _sentences(text):
        words = len(sentence.split())
        if size and size + words > chunk_words:
            chunks.append(' '.join(current))
            carry, carried = [], 0
            for prev in reversed(current):
                prev_words = len(prev.split())
                if carried + prev_words > overlap:
                    break
                carry.insert(0, prev)
                carried += prev_words
            current, size = carry, carried
        current.append(sentence)
        size += words
    if current:
        chunks.append(' '.join(current))
    return [c for c in chunks if c.strip()]


def build_index():
    """Read every supported file under the docs folder into retrievable chunks."""
    path = docs_dir()
    chunks = []
    files = []

    for entry in sorted(path.rglob('*')):
        if not entry.is_file():
            continue
        # Word lock files sit alongside open office documents.
        if entry.name.startswith('.~lock') or entry.name.startswith('~$'):
            continue
        if entry.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue

        sections = read_file(entry)
        if not sections:
            files.append({
                'name': entry.name, 'subject': 'General', 'sections': 0,
                'words': 0, 'readable': False,
            })
            continue

        produced = 0
        words = 0
        by_subject = {}
        for section in sections:
            for piece in chunk_section(section['text']):
                words += len(piece.split())
                produced += 1
                by_subject[section['subject']] = by_subject.get(section['subject'], 0) + 1
                chunks.append({
                    'id': f'{entry.name}#{produced}',
                    'title': section['label'][:255],
                    'source': entry.name,
                    'subject': section['subject'],
                    'content': piece,
                    # Past answers are useful context but must never displace
                    # the course material they were written from.
                    'origin': 'history' if entry.name == HISTORY_FILE else 'file',
                })
        # A file covering several courses reports the one with most content.
        top = max(by_subject, key=by_subject.get) if by_subject else 'General'
        files.append({
            'name': entry.name, 'subject': top, 'sections': produced,
            'words': words, 'readable': True, 'by_subject': by_subject,
        })

    return chunks, files


def index(refresh=False):
    """Cached view of the docs folder, rebuilt whenever a file changes."""
    path = docs_dir()
    stamp = _stamp(path)
    with _lock:
        if not refresh and stamp is not None and stamp == _cache['stamp']:
            return _cache['chunks'], _cache['files']
    chunks, files = build_index()
    with _lock:
        _cache['stamp'] = stamp
        _cache['chunks'] = chunks
        _cache['files'] = files
    return chunks, files


def library_status():
    """What the knowledge base currently holds, for the UI and diagnostics."""
    chunks, files = index()
    return {
        'path': str(docs_dir()),
        'files': files,
        'total_chunks': len(chunks),
        'supported': sorted(SUPPORTED_SUFFIXES),
    }


# ---------------------------------------------------------------- writing

def _history_path():
    return docs_dir() / HISTORY_FILE


def record_exchange(question, subject, answer, sources=None, kind='answer'):
    """Append a question and its answer to the knowledge base.

    The exchange is written to ``docs/tutor_history.md``, which the indexer
    picks up on the next question. Reviewed material stays in the original
    files, and what the tutor has actually been asked stays auditable
    alongside it.
    """
    path = _history_path()
    stamp = time.strftime('%Y-%m-%d %H:%M')
    cited = ', '.join(
        f"{s.get('title', 'source')}"
        for s in (sources or [])[:3]
    ) or 'no source matched'

    block = (
        f"\n## {question.strip()}\n\n"
        f"- Asked: {stamp}\n"
        f"- Subject: {subject or 'General'}\n"
        f"- Kind: {kind}\n"
        f"- Sources: {cited}\n\n"
        f"{answer.strip()}\n"
    )

    try:
        with _lock:
            header = ''
            if not path.exists():
                header = (
                    '# Tutor Answer History\n\n'
                    'Questions asked in the AI Tutor, with the answer that was '
                    'given and the document sections it came from. This file is '
                    'indexed like every other document in the docs folder.\n'
                )
            with path.open('a', encoding='utf-8') as handle:
                handle.write(header + block)
    except OSError:
        return None

    # The folder changed, so the cached index is stale.
    with _lock:
        _cache['stamp'] = None
    return str(path)
