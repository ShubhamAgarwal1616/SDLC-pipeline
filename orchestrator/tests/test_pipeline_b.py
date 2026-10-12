from pathlib import Path

from sdlc.stages.pipeline_b import failure_digest, junit_report, rank_files, skill_hints

RULES = [{"paths": ["**/*.kt"], "skill": "kotlin-service-standards"},
         {"terms": ["firestore"], "skill": "firestore-adapter"}]


def test_skill_hints_by_path_and_term():
    files = ["src/main/kotlin/App.kt", "README.md"]
    assert skill_hints(RULES, files, "rate limiting") == ["kotlin-service-standards"]
    assert skill_hints(RULES, ["README.md"], "store notes in Firestore") == ["firestore-adapter"]
    assert skill_hints([], files, "x") == []


def test_rank_files_prefers_overlap(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ApiKeys.kt").write_text("api key filter; client key")
    (tmp_path / "src" / "Other.kt").write_text("nothing relevant")
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec" / "x.md").write_text("client client client")
    ranked = rank_files(tmp_path, ["src/ApiKeys.kt", "src/Other.kt", "openspec/x.md"], {"client", "filter"})
    assert ranked[0][0] == "src/ApiKeys.kt" and all(not r.startswith("openspec/") for r, _ in ranked)


XML = """<testsuite tests="3" failures="1" errors="0" skipped="0">
<testcase classname="NotesApiTest" name="rate limits key"><failure message="expected 429 but was 201"/></testcase>
<testcase classname="NotesApiTest" name="ok"/><testcase classname="X" name="y"/></testsuite>"""


def test_test_report_and_digest(tmp_path):
    d = tmp_path / "build" / "test-results" / "test"
    d.mkdir(parents=True)
    (d / "TEST-x.xml").write_text(XML)
    report = junit_report(tmp_path)
    assert report["tests"] == 3 and report["failures"] == 1
    assert report["failed"] == ["NotesApiTest > rate limits key: expected 429 but was 201"]
    digest = failure_digest("e: Foo.kt:3:1 Unresolved reference 'x'\nBUILD FAILED", report)
    assert "Compilation errors" in digest and "expected 429" in digest and "BUILD FAILED" in digest


def test_added_dependencies_detects_only_new_lines():
    from sdlc.stages.pipeline_b_review import added_dependencies
    before = 'dependencies {\n    implementation("org.http4k:http4k-core")\n}\n'
    after = ('dependencies {\n    implementation("org.http4k:http4k-core")\n'
             '    implementation("io.github.bucket4j:bucket4j-core:8.10.1")\n    // implementation("x") comment\n}\n')
    assert added_dependencies(before, after) == ['implementation("io.github.bucket4j:bucket4j-core:8.10.1")']
    assert added_dependencies(after, after) == []


def test_watch_fix_prefix():
    from sdlc.watch import feedback_text
    assert feedback_text([], {"body": "/sdlc fix rename limiter"}, "/sdlc fix") == "- rename limiter"
