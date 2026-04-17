"""Phase 0 smoke test — imports resolve and sqlglot classifier works."""
from node.llm_engine import LLMClient


def test_classify_select():
    client = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    assert client.classify_sql("SELECT * FROM users") == "read"


def test_classify_insert():
    client = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    assert client.classify_sql("INSERT INTO users (name) VALUES ('Alice')") == "write"


def test_classify_create():
    client = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    assert client.classify_sql("CREATE TABLE users (id INTEGER, name TEXT)") == "ddl"


def test_classify_unknown():
    client = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    assert client.classify_sql("NOT SQL AT ALL !!!") == "unknown"
