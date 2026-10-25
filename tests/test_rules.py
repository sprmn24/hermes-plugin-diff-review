"""Detection rules on single added lines."""

import pytest

from helpers import added_lines_diff, diff_text_review

PRINT = "debug print statement"
SECRET = "possible hardcoded secret"
BARE = "bare except clause"
BROAD = "broad Exception catch"
TODO = "unresolved TODO/FIXME"


def found(line: str, path: str = "f"):
    return [w["issue"] for w in diff_text_review(added_lines_diff(path, [line]))["warnings"]]


@pytest.mark.parametrize("line, expected", [
    # debug prints
    ("print('x')", [PRINT]),
    ("    print (x)", [PRINT]),
    ("pprint(obj)", [PRINT]),
    ("x = fingerprint(data)", []),
    ("sprint(3)", []),
    ("blueprint(app)", []),
    ("self.print(doc)", []),
    # console
    ("console.log('x')", ["console.log statement"]),
    ("console.info('x')", ["console.info statement"]),
    ("console.warn('x')", ["console.warn statement"]),
    ("console.error('x')", ["console.error statement"]),
    ("console.debug('x')", ["console.debug statement"]),
    ("console.trace()", ["console.trace statement"]),
    ("myconsole.log('x')", []),
    # except clauses
    ("except:", [BARE]),
    ("    except :  # noqa", [BARE]),
    ("except Exception:", [BROAD]),
    ("except Exception as e:", [BROAD]),
    ("except (Exception,):", [BROAD]),
    ("except (ValueError, Exception) as exc:", [BROAD]),
    ("except BaseException:", [BROAD]),
    ("except* Exception:", [BROAD]),
    ("except ExceptionGroup:", []),
    ("except MyException:", []),
    ("except ValueError:", []),
    # markers
    ("# TODO: later", [TODO]),
    ("// FIXME", [TODO]),
    ("# HACK around bug", [TODO]),
    ("# XXX", [TODO]),
    ("# HACKING guide", []),
    ("XXXylophone = 1", []),
    ("TODO_LIST = []", []),
])
def test_rules(line, expected):
    assert found(line) == expected


@pytest.mark.parametrize("line", [
    "password = 'hunter2'",
    "self.password = \"hunter2\"",
    "password: hunter2",
    "API_KEY=abc123",
    "export API_TOKEN=abc123",
    '"password": "hunter2"',
    'token: str = "abc123"',
    'if (token := "abc123"):',
    "SECRET_KEY = \"django-insecure-abc\"",
    "AWS_SECRET_ACCESS_KEY = \"abcd1234\"",
    "passwd = \"hunter2\"",
    "apikey = \"abcd1234\"",
    "private_key = \"abcd1234\"",
    'auth = {"api-key": "abcd1234"}',
    "client_secret => 'abcd1234'",
    "db_password: correcthorsebatterystaple",
])
def test_secret_forms(line):
    assert found(line) == [SECRET]


@pytest.mark.parametrize("line", [
    "password = ''",
    "password = get_password()",
    "password = os.environ['DB_PASSWORD']",
    "password_field = 'Password'",
    "max_tokens = '100'",
    "tokenizer = 'bert'",
    "if password == 'x':",
    "api_key: SecretStr",
    "password: Optional[str] = None",
    "password: ${DB_PASSWORD}",
    "token: null",
    "token = self.token",
    "secret: <redacted>",
])
def test_secret_non_matches(line):
    assert found(line) == []


def test_findings_only_on_added_lines():
    text = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +1 @@\n-print(1)\n # TODO context\n"
    assert diff_text_review(text)["warnings"] == []


def test_several_rules_on_one_line():
    assert found("print(token := 'abc123')  # TODO") == [PRINT, SECRET, TODO]
