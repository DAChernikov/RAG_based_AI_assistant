import pytest

from app.connectors.parsing import parse_code


@pytest.mark.parametrize(
    ("path", "text", "language", "symbol"),
    [
        ("README.md", "# Install\nRun it", "markdown", "Install"),
        ("main.py", "class Service:\n    pass\n", "python", "Service"),
        ("schema.sql", "CREATE TABLE users(id int);", "sql", "users"),
        ("Main.scala", "object Main { }", "scala", "Main"),
        ("Service.java", "public class Service { }", "java", "Service"),
        ("config.yaml", "server:\n  port: 8000", "yaml", "server"),
        ("config.json", '{"server": {"port": 8000}}', "json", "server"),
    ],
)
def test_structure_aware_parsers(path, text, language, symbol):
    actual_language, chunks = parse_code(path, text)
    assert actual_language == language
    assert chunks
    assert chunks[0].metadata.get("symbol") == symbol
