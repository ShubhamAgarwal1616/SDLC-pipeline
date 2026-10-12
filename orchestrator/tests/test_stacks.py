"""Build stacks (Gradle, Node/TypeScript): detection, the new-dependency guard, digests, the Node skeleton."""

import json

import pytest

from sdlc import init_repo, stacks
from sdlc.stages.pipeline_b import failure_digest, prep_stack


def test_detect_by_files_or_pin(tmp_path):
    assert stacks.detect(tmp_path) is None
    (tmp_path / "package.json").write_text("{}")
    assert stacks.detect(tmp_path) is None                      # no lockfile: can't be cached offline
    (tmp_path / "package-lock.json").write_text("{}")
    assert stacks.detect(tmp_path) is stacks.NODE
    assert stacks.detect(tmp_path, "gradle") is stacks.GRADLE
    with pytest.raises(ValueError, match="supported"):
        stacks.detect(tmp_path, "cobol")


def test_package_json_new_dependencies():
    before = json.dumps({"dependencies": {"zod": "^3"}, "devDependencies": {"typescript": "~5.9"}})
    after = json.dumps({"dependencies": {"zod": "^3", "express": "^5"}, "devDependencies": {"typescript": "~5.9.3"}})
    assert stacks.added_dependencies("package.json", before, after) == ["dependencies: express"]
    assert stacks.added_dependencies("package.json", after, after) == []


def test_lockfile_new_packages():
    before = json.dumps({"packages": {"": {}, "node_modules/typescript": {}}})
    after = json.dumps({"packages": {"": {}, "node_modules/typescript": {}, "node_modules/left-pad": {}}})
    assert stacks.added_dependencies("package-lock.json", before, after) == ["lockfile: left-pad"]


def test_gradle_dependencies_still_detected():
    before = 'dependencies {\n    implementation("a:b:1")\n}\n'
    after = before.replace("}", '    testImplementation("c:d:2")\n}')
    assert stacks.added_dependencies("build.gradle.kts", before, after) == ['testImplementation("c:d:2")']


def test_typescript_compile_errors_reach_the_digest():
    log = "src/app.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\nnpm ERR! code 2"
    assert "error TS2322" in failure_digest(log, {"failed": []})


def test_old_runs_without_a_stack_field_are_gradle():
    assert prep_stack({"gradle_warm": "/x"}) is stacks.GRADLE
    assert prep_stack({"stack": "node", "deps_warm": "/y"}) is stacks.NODE
    assert prep_stack({}) is None


def test_node_skeleton_scaffold(tmp_path):
    init_repo.scaffold(tmp_path, "acme/Orders-Web", "main", "KAN", skeleton="node-typescript")
    pkg = json.loads((tmp_path / "package.json").read_text())
    lock = json.loads((tmp_path / "package-lock.json").read_text())
    assert pkg["name"] == "orders-web" and lock["name"] == "orders-web" and lock["packages"][""]["name"] == "orders-web"
    assert "node_modules/typescript" in lock["packages"]
    cfg = (tmp_path / ".sdlc/pipeline.yaml").read_text()
    assert "stack: node" in cfg and "npm test" in cfg and "kotlin-service-standards" not in cfg
    assert (tmp_path / "test/health.test.ts").exists() and (tmp_path / ".sdlc/instructions/README.md").exists()
