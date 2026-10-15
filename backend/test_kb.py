"""Tests for the fixed-folder knowledge base.

The tutor no longer accepts uploads: it reads whatever is in the project's
``docs`` folder. These tests cover reading that folder, tagging sections with
the right course, and merging those sections into retrieval.
"""

import os
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")

from app import docstore
from app.database import Base, SessionLocal, engine
from app.rag import retrieve

Base.metadata.create_all(engine)


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tmp = Path(tempfile.mkdtemp())
        self._real = docstore.docs_dir
        self._cache = docstore._cache
        docstore.docs_dir = lambda: self.tmp
        docstore._cache = {"stamp": None, "chunks": [], "files": []}

    def tearDown(self):
        docstore.docs_dir = self._real
        docstore._cache = self._cache
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.db.close()

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_markdown_sections(self):
        self.write("notes.md", "# DBMS\n\nA DBMS stores data.\n\n## Joins\n\nJoins combine rows.\n")
        chunks, files = docstore.index()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["readable"])
        self.assertEqual({c["subject"] for c in chunks}, {"DBMS"})
        self.assertTrue(any("Joins" in c["title"] for c in chunks))

    def test_reads_plain_text_and_csv(self):
        self.write("a.txt", "Normalization reduces redundancy.\n")
        self.write("b.csv", "topic,note\njoins,combine rows\n")
        chunks, files = docstore.index()
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f["readable"] for f in files))
        self.assertTrue(chunks)

    def test_unsupported_file_is_skipped(self):
        self.write("image.png", "not text")
        self.write("notes.md", "# AI\n\nMachines learn patterns.\n")
        _, files = docstore.index()
        self.assertEqual([f["name"] for f in files], ["notes.md"])

    def test_lock_files_are_ignored(self):
        self.write(".~lock.notes.docx#", "lock")
        self.write("~$notes.docx", "lock")
        self.write("notes.md", "# AI\n\nMachines learn.\n")
        _, files = docstore.index()
        self.assertEqual([f["name"] for f in files], ["notes.md"])

    def test_course_heading_tags_all_its_sections(self):
        self.write(
            "kb.md",
            "# AI\n\n## Definition\n\nAI is computing that learns.\n\n"
            "## Applications\n\nAI is used in robots.\n\n"
            "# DBMS\n\n## Definition\n\nA DBMS manages databases.\n",
        )
        chunks, _ = docstore.index()
        by_subject = Counter(c["subject"] for c in chunks)
        self.assertEqual(by_subject["AI"], 2)
        self.assertEqual(by_subject["DBMS"], 1)

    def test_nested_heading_keeps_its_parent_course(self):
        self.write(
            "kb.md",
            "# Data Structures\n\n## Linked lists\n\nA list links nodes.\n\n"
            "## Stacks\n\nA stack is last in first out.\n",
        )
        chunks, _ = docstore.index()
        self.assertEqual({c["subject"] for c in chunks}, {"Programming"})
        self.assertTrue(all("Data Structures" in c["title"] for c in chunks))

    def test_unlabelled_file_is_general(self):
        self.write("scope.md", "Some project notes without a course name.\n")
        chunks, _ = docstore.index()
        self.assertEqual({c["subject"] for c in chunks}, {"General"})

    def test_index_is_cached_until_a_file_changes(self):
        self.write("kb.md", "# AI\n\nMachines learn.\n")
        first = docstore.index()[0]
        again = docstore.index()[0]
        self.assertEqual(len(first), len(again))
        (self.tmp / "kb.md").write_text("# DBMS\n\nDatabases store rows.\n", encoding="utf-8")
        after = docstore.index(refresh=True)[0]
        self.assertEqual({c["subject"] for c in after}, {"DBMS"})

    def test_shipped_document_is_tagged_per_course(self):
        # Read the project's real docs folder, not the temporary one.
        docstore.docs_dir = self._real
        docstore._cache = {"stamp": None, "chunks": [], "files": []}
        real = docstore.docs_dir()
        if not real.exists():
            self.skipTest("no docs folder in this checkout")
        chunks, files = docstore.index()
        by_subject = Counter(c["subject"] for c in chunks)
        self.assertGreater(len(chunks), 0)
        self.assertIn("DBMS", by_subject)
        self.assertTrue(any(f["name"].endswith(".docx") for f in files))

    def test_record_exchange_appends_and_is_reindexed(self):
        self.write("kb.md", "# AI\n\nMachines learn from data.\n")
        self.assertEqual(docstore.index()[0][0]["subject"], "AI")
        docstore.record_exchange("What is machine learning?", "AI", "A field of study.", [])
        text = (self.tmp / "tutor_history.md").read_text(encoding="utf-8")
        self.assertIn("What is machine learning?", text)
        self.assertIn("A field of study.", text)
        # The new file must be visible to the next read without a restart.
        names = [f["name"] for f in docstore.index()[1]]
        self.assertIn("tutor_history.md", names)

    def test_retrieve_prefers_the_folder_over_seeded_notes(self):
        self.write("kb.md", "# DBMS\n\n## Normalization\n\nNormalization removes redundancy in tables.\n")
        results = retrieve(self.db, "How does normalization remove redundancy?", "DBMS")
        self.assertTrue(results, "expected at least one match")
        top = results[0]
        self.assertEqual(top["subject"], "DBMS")
        self.assertEqual(top["source"], "kb.md")
        self.assertEqual(top["origin"], "file")

    def test_answer_history_never_outranks_course_material(self):
        self.write("kb.md", "# DBMS\n\n## Normalization\n\nNormalization removes redundancy in tables.\n")
        docstore.record_exchange(
            "What is normalization?",
            "DBMS",
            "Normalization removes redundancy in tables.",
            [],
        )
        results = retrieve(self.db, "What is normalization in tables?", "DBMS")
        origins = [r["origin"] for r in results]
        self.assertIn("file", origins)
        # Any history hit must sit below the notes it was written from.
        if "history" in origins:
            self.assertLess(
                origins.index("file"), origins.index("history"),
                f"answer history ranked above the course material: {origins}",
            )

    def test_retrieve_falls_back_when_nothing_matches_the_subject(self):
        self.write("kb.md", "# AI\n\nMachine learning finds patterns in data.\n")
        # Every term hits the AI note, but the course asked for is DBMS, so the
        # soft filter has nothing to return and must fall back to it.
        results = retrieve(self.db, "What is machine learning in AI?", "DBMS")
        self.assertTrue(results, "soft subject filter should still return the AI note")
        self.assertEqual(results[0]["subject"], "AI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
