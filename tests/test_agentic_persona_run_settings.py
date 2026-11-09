"""SPEC-0394: declared personas' run settings, reviewer routing metadata, `is_reviewer`,
`build_request` for a declared persona and the generated Output section (offline, temp dirs)."""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from nightwright.agentic import persona_defs as PD
from nightwright.agentic import persona_registry as R
from nightwright.agentic import personas as PS
from nightwright.agentic.profiles import resolve_profile

pytestmark = pytest.mark.fast

TEMPLATE = "Review {{spec_id}} in {{worktree}} for crypto misuse.\n{{diff}}\n{{test_files}}\n"
ROOT = Path(__file__).resolve().parent.parent
DEFS = ROOT / "src" / "nightwright" / "agentic" / "persona_defs"


@pytest.fixture
def env(tmp_path):
    """cfg, user personas dir, main checkout root, its personas dir, and a campaign state whose
    worktree (a git repo with one branch commit) is a separate directory."""
    cfg = {"config_dir": str(tmp_path / "cfg"), "data_dir": str(tmp_path / "data"),
           "agentic": {"profiles": {}, "default_profile": "balanced"}}
    user = tmp_path / "cfg" / "personas"
    root = tmp_path / "main"
    pdir = root / ".nw" / "personas"
    user.mkdir(parents=True)
    pdir.mkdir(parents=True)
    wt = tmp_path / "worktree"
    wt.mkdir()
    genv = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}

    def git(*args):
        subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, env=genv)
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "base")
    git("checkout", "-q", "-b", "nw/x")
    (wt / "src").mkdir()
    (wt / "src" / "a.py").write_text("print('hi')\n")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    git("add", "-A")
    git("commit", "-q", "-m", "change")
    state = {"id": "SPEC-0001-20261102-ab12", "kind": "implement", "spec_id": "SPEC-0001",
             "project": "Proj", "state": "REVIEWING",
             "repo": {"root": str(root), "base": "main", "branch": "nw/x", "worktree": str(wt)},
             "spec": {"rel_path": "docs/specs/0001-x.md"}, "counters": {"loops": 1}}
    return cfg, user, root, pdir, state


def _put(directory, name, role="reviewer", template=TEMPLATE, extra=""):
    (directory / f"{name}.md").write_text(template, encoding="utf-8")
    (directory / f"{name}.yaml").write_text(
        f"schema: nw.persona.v1\nname: {name}\nrole: {role}\ntemplate: {name}.md\n{extra}",
        encoding="utf-8")


def _problems(user, root):
    return PD.validate_persona_defs(root, user_dir=user)


def _tail(msg):
    """`origin: key: problem` -> `key: problem` (the two validators name the origin apart)."""
    return msg.split(": ", 1)[1]


# ---- AC1: run-default keys -------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    "model: opus\n", "model: claude-opus-4-1\n", "model: 'claude-sonnet-4-5[1m]'\n",
    "effort: high\n", "max_budget_usd: 2.0\n", "max_budget_usd: 3\n",
    "model: haiku\neffort: low\nmax_budget_usd: 0.25\n"])
@pytest.mark.parametrize("role", ["reviewer", "implementer", "triager"])
def test_settings_keys_valid(env, extra, role):
    cfg, user, root, pdir, _ = env
    _put(pdir, "my_persona", role=role, template="{{spec_text}}\n", extra=extra)
    assert _problems(user, root) == []


@pytest.mark.parametrize("extra,key,needle", [
    ("model: gpt4\n", "model", "got 'gpt4'"),
    ("model: claude\n", "model", "claude-* model id"),
    ("model: 3\n", "model", "got 3"),
    ("effort: extreme\n", "effort", "must be one of low, medium, high, xhigh, max"),
    ("effort: [high]\n", "effort", "got ['high']"),
    ("max_budget_usd: 0\n", "max_budget_usd", "must be a finite number > 0"),
    ("max_budget_usd: .inf\n", "max_budget_usd", "got inf"),
    ("max_budget_usd: .nan\n", "max_budget_usd", "got nan"),
    ("max_budget_usd: -1.5\n", "max_budget_usd", "got -1.5"),
    ("max_budget_usd: true\n", "max_budget_usd", "got True"),
    ("max_budget_usd: '2.0'\n", "max_budget_usd", "got '2.0'"),
])
def test_settings_keys_invalid(env, extra, key, needle):
    cfg, user, root, pdir, _ = env
    _put(pdir, "my_reviewer", extra=extra)
    probs = _problems(user, root)
    assert len(probs) == 1, probs
    assert probs[0].startswith(f"{root.name}:.nw/personas/my_reviewer.yaml: {key}: ")
    assert needle in probs[0]


@pytest.mark.parametrize("key,value", [("model", "opus"), ("effort", "high"),
                                       ("max_budget_usd", 1.0), ("required", True),
                                       ("when", {"labels": ["x"]})])
def test_declared_keys_refused_in_a_builtin_definition(key, value):
    doc = {"schema": "nw.persona.v1", "name": "implementer", "role": "implementer",
           "template": "implementer.md", "description": "x", "order": 10,
           "output_schema": "implementer", key: value}
    errs = R.validate_persona_doc(doc, origin="builtin:persona_defs/implementer.yaml",
                                  layer="builtin", stem="implementer", known=PS.KNOWN)
    assert errs == [f"builtin:persona_defs/implementer.yaml: {key}: allowed only in a user "
                    "or project definition"]


def test_builtin_definitions_carry_no_declared_keys():
    import yaml
    for f in DEFS.glob("*.yaml"):
        assert not set(yaml.safe_load(f.read_text())) & set(R.DECLARED_KEYS), f.name


# ---- AC2: routing keys -------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    "required: true\n", "required: false\n", "when: {}\n", "when: {paths: ['src/crypto/**']}\n",
    "when: {labels: [security]}\n", "when:\n  paths: ['**/*.rs']\n  labels: [crypto, security]\n"])
def test_routing_keys_valid_for_reviewer(env, extra):
    cfg, user, root, pdir, _ = env
    _put(pdir, "my_reviewer", extra=extra)
    assert _problems(user, root) == []


@pytest.mark.parametrize("extra,key,needle", [
    ("required: yes_please\n", "required", "must be true or false"),
    ("required: 1\n", "required", "got 1"),
    ("when:\n", "when", "must be a mapping"),               # when: null
    ("when: [src]\n", "when", "must be a mapping"),
    ("when: src/**\n", "when", "must be a mapping"),
    ("when: {files: [x]}\n", "when.files", "unknown key; allowed: labels, paths"),
    ("when: {paths: []}\n", "when.paths", "non-empty list of non-empty strings"),
    ("when: {labels: []}\n", "when.labels", "non-empty list of non-empty strings"),
    ("when: {paths: src}\n", "when.paths", "non-empty list"),
    ("when: {paths: [3]}\n", "when.paths", "non-empty strings"),
    ("when: {labels: ['']}\n", "when.labels", "non-empty strings"),
    ("when: {labels: ['  ']}\n", "when.labels", "non-empty strings"),
    ("when: {paths: ['src/**', ' ']}\n", "when.paths", "non-empty strings"),
    ("when: {paths: null}\n", "when.paths", "non-empty list"),
])
def test_routing_keys_reviewer_only_bad_shapes(env, extra, key, needle):
    cfg, user, root, pdir, _ = env
    _put(pdir, "my_reviewer", extra=extra)
    probs = _problems(user, root)
    assert len(probs) == 1, probs
    assert f": {key}: " in probs[0] and needle in probs[0]


@pytest.mark.parametrize("role", ["implementer", "triager"])
@pytest.mark.parametrize("extra,key", [("required: true\n", "required"),
                                       ("when: {labels: [x]}\n", "when"),
                                       ("when: {bogus: 1}\n", "when")])
def test_routing_keys_reviewer_only(env, role, extra, key):
    cfg, user, root, pdir, _ = env
    _put(pdir, "my_persona", role=role, template="{{spec_text}}\n", extra=extra)
    probs = _problems(user, root)
    assert probs == [f"{root.name}:.nw/personas/my_persona.yaml: {key}: only allowed for role "
                     "reviewer"]


@pytest.mark.parametrize("extra,keys", [
    ("required: maybe\n", ["required"]),
    ("when: {files: [x], paths: []}\n", ["when.files", "when.paths"]),
    ("when: [src]\n", ["when"]),
])
def test_routing_shape_still_checked_under_an_invalid_role(env, extra, keys):
    """An unknown role is reported, and `required`/`when` are still shape-checked (both
    validators)."""
    cfg, user, root, pdir, _ = env
    _put(pdir, "x_reviewer", role="auditor", template="{{spec_text}}\n", extra=extra)
    defs = _problems(user, root)
    assert [_tail(m).split(":", 1)[0] for m in defs] == ["role", *keys]
    assert "role: must be one of implementer, reviewer, triager" in defs[0]
    reg = R.load_registry(cfg, project_root=root, known=PS.KNOWN)
    entry = next(e for e in reg.entries if e.name == "x_reviewer")
    assert [_tail(m) for m in entry.errors] == [_tail(m) for m in defs]


# ---- AC3: one helper, every problem at once -------------------------------------------------

MULTI = ("model: gpt4\neffort: extreme\nmax_budget_usd: 0\nrequired: maybe\n"
         "when: {files: x, paths: []}\n")


def test_multi_fault_file_reports_every_problem_in_both_validators(env):
    cfg, user, root, pdir, _ = env
    _put(pdir, "multi_reviewer", extra=MULTI)
    _put(user, "user_impl", role="implementer", template="{{spec_text}}\n",
         extra="effort: extreme\nwhen: {labels: [x]}\n")
    defs = _problems(user, root)
    keys = [_tail(m).split(":", 1)[0] for m in defs]
    assert keys == ["effort", "when",                                     # user layer first
                    "model", "effort", "max_budget_usd", "required", "when.files", "when.paths"]
    reg = R.load_registry(cfg, project_root=root, known=PS.KNOWN)
    by = {e.name: e for e in reg.entries if e.scope != "builtin"}
    assert by["multi_reviewer"].defn is None and by["user_impl"].defn is None
    assert [_tail(m) for m in by["user_impl"].errors + by["multi_reviewer"].errors] == \
        [_tail(m) for m in defs]


def test_registry_validator_accepts_the_keys_in_user_and_project_layers(env):
    cfg, user, root, pdir, _ = env
    _put(pdir, "security_reviewer", extra="model: opus\nrequired: false\n"
                                          "when: {paths: ['src/**']}\n")
    _put(user, "my_impl", role="implementer", template="{{spec_text}}\n",
         extra="effort: high\nmax_budget_usd: 2.5\n")
    reg = R.load_registry(cfg, project_root=root, known=PS.KNOWN)
    by = {e.name: e for e in reg.entries if e.scope != "builtin"}
    assert by["security_reviewer"].errors == [] and by["my_impl"].errors == []


# ---- Persona.declared -----------------------------------------------------------------------

def test_declared_mapping_defaults(env):
    cfg, user, root, pdir, _ = env
    _put(pdir, "plain_reviewer")
    _put(pdir, "full_reviewer", extra="model: opus\neffort: high\nmax_budget_usd: 2.0\n"
                                      "required: false\nwhen: {paths: ['src/**'], labels: [s]}\n")
    _put(user, "my_triage", role="triager", template="{{triage_input}}\n", extra="effort: low\n")
    reg = PD.load_registry(root, user_dir=user)
    plain, full, tri = reg["plain_reviewer"], reg["full_reviewer"], reg["my_triage"]
    assert dict(plain.declared["settings"]) == {}
    assert plain.declared["required"] is True and dict(plain.declared["when"]) == {}
    assert dict(full.declared["settings"]) == {"model": "opus", "effort": "high",
                                               "max_budget_usd": 2.0}
    assert full.declared["required"] is False
    assert dict(full.declared["when"]) == {"paths": ("src/**",), "labels": ("s",)}
    assert dict(tri.declared["settings"]) == {"effort": "low"}
    assert tri.declared["required"] is None and tri.declared["when"] is None
    assert plain.declared["template_sha256"] == hashlib.sha256(TEMPLATE.encode()).hexdigest()
    with pytest.raises(TypeError):
        full.declared["required"] = True                      # read-only mapping
    with pytest.raises(TypeError):
        full.declared["settings"]["model"] = "haiku"
    assert all(p.declared is None for p in PS.PERSONAS.values())


# ---- AC4: is_reviewer -----------------------------------------------------------------------

def test_is_reviewer(env, tmp_path):
    cfg, user, root, pdir, _ = env
    _put(pdir, "security_reviewer")
    _put(pdir, "my_impl", role="implementer", template="{{spec_text}}\n")
    _put(user, "my_triage", role="triager", template="{{triage_input}}\n")
    assert PS.is_reviewer("security_reviewer", root, user_dir=user)
    assert not PS.is_reviewer("my_impl", root, user_dir=user)
    assert not PS.is_reviewer("my_triage", root, user_dir=user)
    assert not PS.is_reviewer("security_reviewer", user_dir=user)            # no root
    other = tmp_path / "other"
    (other / ".nw" / "personas").mkdir(parents=True)
    assert not PS.is_reviewer("security_reviewer", other, user_dir=user)     # not declared there
    _put(user, "user_reviewer")                                              # user layer
    assert PS.is_reviewer("user_reviewer", root, user_dir=user)
    assert PS.is_reviewer("user_reviewer", other, user_dir=user)
    assert not PS.is_reviewer("user_reviewer", user_dir=user)                # no root
    for name in PS.PERSONAS:
        want = name in PS.REVIEWER_PERSONAS
        assert PS.is_reviewer(name) is want and PS.is_reviewer(name, root, user_dir=user) is want


# ---- AC5 / AC6: build_request ---------------------------------------------------------------

def _req(cfg, profile, root, state, persona, tmp_path):
    return PS.build_request(cfg, profile, {"_root": str(root)}, state, persona,
                            run_dir=tmp_path / "runs" / f"001-{persona}")


@pytest.mark.parametrize("layers,want", [
    # (definition settings, profile conformance entry override, explicit personas.<name>)
    ((None, None, None), ("opus", "high", 1.5)),        # balanced's conformance_reviewer entry
    (("model: haiku\n", None, None), ("haiku", "high", 1.5)),
    (("model: haiku\nmax_budget_usd: 3.0\n", None, {"max_budget_usd": 0.75}),
     ("haiku", "high", 0.75)),
    ((None, "absent", None), ("sonnet", "medium", 1.0)),   # nightwright defaults
    (("effort: low\n", "absent", None), ("sonnet", "low", 1.0)),
    (("effort: low\n", None, {"model": "claude-x-1", "effort": "max"}),
     ("claude-x-1", "max", 1.5)),
])
def test_build_request_declared_reviewer_layering(env, tmp_path, layers, want):
    cfg, user, root, pdir, state = env
    extra, role_default, explicit = layers
    _put(pdir, "security_reviewer", extra=extra or "")
    profile = resolve_profile(cfg)
    if role_default == "absent":
        del profile["personas"]["conformance_reviewer"]
    if explicit is not None:
        profile["personas"]["security_reviewer"] = explicit
    req = _req(cfg, profile, root, state, "security_reviewer", tmp_path)
    assert (req.model, req.effort, req.max_budget_usd) == want


def test_build_request_declared_reviewer(env, tmp_path):
    cfg, user, root, pdir, state = env
    _put(pdir, "security_reviewer", extra="category: [key_handling, timing]\n")
    # a different copy in the campaign worktree: never read
    wdir = Path(state["repo"]["worktree"]) / ".nw" / "personas"
    wdir.mkdir(parents=True)
    _put(wdir, "security_reviewer", template="WORKTREE COPY\n",
         extra="model: haiku\ncategory: [rogue]\n")
    _put(wdir, "rogue_reviewer")
    profile = resolve_profile(cfg)
    profile["personas"]["security_reviewer"] = {"read_only": False,
                                                "permission_mode": "bypassPermissions"}
    req = _req(cfg, profile, root, state, "security_reviewer", tmp_path)
    assert req.read_only is True and req.permission_mode == "dontAsk"
    assert req.allowed_tools == ()
    assert req.json_schema == PS.reviewer_schema(["key_handling", "timing"])
    assert req.model == "opus"                              # not the worktree copy's haiku
    assert "WORKTREE COPY" not in req.prompt and "rogue" not in req.prompt
    assert "Review SPEC-0001" in req.prompt
    # review context: the branch diff and the test files it touches
    assert "src/a.py" in req.prompt and "print('hi')" in req.prompt
    assert "tests/test_a.py" in req.prompt
    assert req.persona == "security_reviewer"
    with pytest.raises(ValueError, match="unknown persona 'rogue_reviewer'"):
        _req(cfg, profile, root, state, "rogue_reviewer", tmp_path)


def test_build_request_resolves_the_declared_registry_once(env, tmp_path, monkeypatch):
    """Persona and review context come from one registry scan, so they cannot disagree."""
    cfg, user, root, pdir, state = env
    _put(pdir, "security_reviewer")
    calls = []
    real = PD.load_registry
    monkeypatch.setattr(PD, "load_registry",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    req = _req(cfg, resolve_profile(cfg), root, state, "security_reviewer", tmp_path)
    assert len(calls) == 1
    assert "print('hi')" in req.prompt and "tests/test_a.py" in req.prompt


def test_build_request_declared_reviewer_needs_the_root(env, tmp_path):
    cfg, user, root, pdir, state = env
    _put(pdir, "security_reviewer")
    with pytest.raises(ValueError, match="unknown persona"):
        PS.build_request(cfg, resolve_profile(cfg), {}, state, "security_reviewer",
                         run_dir=tmp_path / "r")


def test_build_request_declared_triager_forced_read_only(env, tmp_path):
    cfg, user, root, pdir, state = env
    _put(user, "my_triage", role="triager", template="{{triage_input}}\n")
    profile = resolve_profile(cfg)
    profile["personas"]["my_triage"] = {"read_only": False, "permission_mode": "acceptEdits"}
    req = _req(cfg, profile, root, state, "my_triage", tmp_path)
    assert req.read_only and req.permission_mode == "dontAsk" and req.allowed_tools == ()
    assert req.json_schema is PS.GATE_TRIAGER_SCHEMA
    assert (req.model, req.effort, req.max_budget_usd) == ("sonnet", "medium", 0.5)
    assert "## Output" not in req.prompt


def test_build_request_declared_implementer_writable(env, tmp_path):
    cfg, user, root, pdir, state = env
    _put(pdir, "my_impl", role="implementer", template="Implement {{spec_id}}.\n",
         extra="max_budget_usd: 6.0\n")
    pc = {"_root": str(root), "gates": [{"name": "pytest", "cmd": "uv run pytest -q"}],
          "agents": {"permission_mode": "acceptEdits", "allowed_tools": ["Bash(uv run nw *)"]}}
    req = PS.build_request(cfg, resolve_profile(cfg), pc, state, "my_impl",
                           run_dir=tmp_path / "r")
    assert not req.read_only and req.permission_mode == "acceptEdits"
    assert req.allowed_tools == ("Bash(uv run nw *)", "Bash(uv run pytest -q*)")
    assert req.json_schema is PS.IMPLEMENTER_SCHEMA
    assert (req.model, req.effort, req.max_budget_usd) == ("sonnet", "high", 6.0)
    assert "```diff" not in req.prompt and "print('hi')" not in req.prompt   # no review context
    assert "## Output" not in req.prompt


# ---- AC7: Output section --------------------------------------------------------------------

def test_output_section(env):
    cfg, user, root, pdir, state = env
    _put(pdir, "security_reviewer", extra="category: [key_handling, timing, randomness]\n")
    p = PD.load_registry(root, user_dir=user)["security_reviewer"]
    prompt = PS.build_prompt(p, PS.build_context(cfg, state))
    out = prompt.index("## Output")
    assert prompt.index("for crypto misuse") < out < prompt.index("## nw queue invariants")
    assert "`key_handling`, `timing`, `randomness`, `other`" in prompt
    assert "`must_fix` | `major` | `minor` | `nit`" in prompt
    assert "`pass` | `fix`" in prompt and "integer 0..100" in prompt
    assert "`fix_brief`" in prompt and "`detail`" in prompt and "`files`" in prompt


def test_builtin_prompts_unchanged():
    ctx = {k: f"<{k}>" for k in PS.PLACEHOLDERS}
    for name, p in PS.PERSONAS.items():
        want = PS.render(p.template, ctx).rstrip("\n") + "\n\n" + PS.render(p.invariant, ctx) \
            + "\n"
        assert PS.build_prompt(name, ctx) == want, name
        assert "## Output\n\nFinish with ONE JSON object and nothing after it:\n\n- `verdict`" \
            not in PS.build_prompt(name, ctx)


# ---- AC9: inert -----------------------------------------------------------------------------

def test_inert(env):
    cfg, user, root, pdir, _ = env
    _put(pdir, "security_reviewer")
    assert set(PS.PERSONAS) == {f.stem for f in DEFS.glob("*.yaml")}
    assert "security_reviewer" not in PS.PERSONAS
    assert "security_reviewer" not in PS.REVIEWER_PERSONAS
    assert "security_reviewer" not in PS.ADOPT_REGISTRY
    assert set(PS.REVIEWER_PERSONAS) <= set(PS.PERSONAS)
    cfg["agentic"]["profiles"] = {"mine": {"extends": "balanced",
                                           "reviewers": ["conformance_reviewer",
                                                         "security_reviewer"]}}
    with pytest.raises(ValueError, match="security_reviewer"):
        resolve_profile(cfg, "mine", project_root=root)

