"""Idea -> spec promotion (SPEC-0013): scaffold a docs/specs/NNNN-slug.md from a template,
prefilled from an org idea's subtree, with a durable bidirectional link (`:SPEC:` property on
the idea <-> `source_idea:` frontmatter on the spec). The idea advances to SPECCED.

Also spec -> org task/epic generation (SPEC-0014): `spec_tasks` extracts a task list from an
accepted spec's Breakdown (epic) / Acceptance criteria (feature); `generate_from_spec` ties that
to `org_write.generate_tasks` and advances a linked idea to PROMOTED.

Also outbound spec authxxing (SPEC-0015): `scaffold_outbound` drops a portable spec into
`<data_dir>/outbox/<project>/<PROJ>-NNNN-slug.md` — a parallel namespace/id-space that NEVER
touches `docs/specs/` or `docs/LEDGER.md`. `validate_outbound` + `write_outbox_index` are light,
outbox-only counterparts of the repo linter's validate/write_ledger (the outbound schema
differs: id is `PROJ-NNNN`, not `SPEC-NNNN`).

Frontmatter/section parsing uses `wt.specmeta` (in-package, stdlib-only), NOT the dev-only repo
linter — so the installed CLI has no runtime dependency on the repo's tooling or its dev deps.

The specs directory is injectable via cfg["specs_dir"] (tests MUST use a tmp dir — writing to
the real docs/specs/ would create a bogus numbered spec and dirty the ledger); it defaults to
`Path.cwd() / "docs/specs"` for real CLI use. The outbox root is injectable via
cfg["outbox_dir"] (default `<data_dir>/outbox` — USER DATA, not the wt repo; SPEC-0021)."""
import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

from .render import warn
from . import org_write as W
from .org_write import resolve_selector
from .specmeta import sections, split_frontmatter

_HEADLINE_RE = re.compile(r"^(\*+)\s")
_NUM_RE = re.compile(r"^(\d{4})-")
_CONTEXT_RE = re.compile(r"(## Context\n\n).*?(\n\n## Goals)", re.S)
_BULLET_RE = re.compile(r"^\s*-\s*(?:\[[ xX]\]\s*)?(.+?)\s*$")
_BULLET_START_RE = re.compile(r"^\s*-\s")


def _yaml_fm_value(value: str) -> str:
    """YAML-safe single-line scalar for frontmatter `key: <value>` (SPEC-0038).

    Always JSON-double-quoted so titles with `:` (prose or leftover tag cookies) never break
    yaml.safe_load.
    """
    return json.dumps(str(value), ensure_ascii=False)

def _package_specs_dir():
    """wt's own `docs/specs`, located from the installed package via the shared checkout walk
    (SPEC-0166), or None when the package does not run from a checkout."""
    from .rules import discover_wt_checkout
    root = discover_wt_checkout()
    return Path(root) / "docs" / "specs" if root else None


def _specs_dir(cfg):
    """Where internal `SPEC-NNNN` files live (SPEC-0079).

    `cfg['specs_dir']` wins. Otherwise wt's own `docs/specs`, located from *this module* rather
    than from the current working directory. The old cwd-relative fallback meant a test whose
    config fixture omitted `specs_dir` silently resolved — and wrote — against the live repo
    while believing it was sandboxed in `tmp_path`; `promote_idea` would have minted a real
    `docs/specs/NNNN-*.md` that way. Anchoring to the package keeps the no-config CLI ergonomics
    without letting the answer depend on where the process happens to be running."""
    configured = cfg.get("specs_dir")
    if configured:
        return Path(configured)
    from .rules import CHECKOUT_HINT, configured_wt_checkout
    checkout = configured_wt_checkout(cfg)            # SPEC-0166 override
    default = checkout / "docs" / "specs" if checkout else _package_specs_dir()
    if default is None or not default.is_dir():
        raise ValueError(
            "cannot locate the internal specs directory; set `specs_dir` in your config, or "
            f"{CHECKOUT_HINT} (looked for {default})")
    return default


def next_spec_number(specs_dir):
    """Max existing docs/specs/NNNN-*.md + 1, zero-padded to 4 digits (as a string)."""
    specs_dir = Path(specs_dir)
    nums = [int(m.group(1)) for p in specs_dir.glob("[0-9]*.md")
            if (m := _NUM_RE.match(p.name))]
    return f"{(max(nums) + 1) if nums else 1:04d}"


def slugify(title, maxlen=50):
    """Kebab-case a title for a spec filename, capped so filenames stay sane (an idea's
    heading can be a whole paragraph — an uncapped slug overruns the OS filename limit)."""
    s = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] or s[:maxlen]   # trim at a word boundary
    return s or "untitled"


def _short_title(heading, maxlen=70):
    """A concise spec title from an idea heading; long headings are truncated at a word
    boundary (the full text is preserved in the scaffolded Context)."""
    h = " ".join(heading.split())
    return h if len(h) <= maxlen else (h[:maxlen].rsplit(" ", 1)[0] or h[:maxlen])


def _idea_context(task):
    """Prefilled Context prose: the idea's heading + its subtree body/notes (everything after
    the headline up to the next headline of level <= the idea's, or EOF).

    Org enrichment markup is lightly converted to Markdown (SPEC-0030) so spec Context stays
    Markdown-native.
    """
    from .org_markup import from_org_body, strip_drawers

    lines = open(task.file).read().splitlines()
    body = []
    for line in lines[task.line:]:
        m = _HEADLINE_RE.match(line)
        if m and len(m.group(1)) <= task.level:
            break
        body.append(line)
    notes = from_org_body(strip_drawers("\n".join(body)).strip("\n"))
    header = f"Promoted from idea `{task.id}` ({task.heading})."
    return f"{header}\n\n{notes}" if notes else header


def _refuse_closed_idea(cfg, task, force):
    """SPEC-0167: never silently re-promote a finished idea. Ideas files are shared across
    concurrent sessions and ids are global, so a terminal (`is_done`) or archived idea reaching
    a promote path is most likely a wrong id — refuse unless `force`."""
    if force:
        return
    archived = os.path.realpath(task.file) == os.path.realpath(W.ideas_archive_path(cfg))
    if not (task.is_done or archived):
        return
    where = f" and archived in {task.file}" if archived else ""
    raise ValueError(f"idea {task.id} {task.heading!r} is {task.state}{where}; refusing to "
                     f"re-promote a closed idea (pass --force if you really mean it)")


def scaffold_from_idea(cfg, selector, *, epic=False, title=None, force=False):
    """Resolve `selector` to an idea, scaffold a new docs/specs/NNNN-slug.md (draft) from the
    template (TEMPLATE-epic.md if `epic` else TEMPLATE.md), prefill Context from the idea's org
    subtree, set `source_idea`, and write the reciprocal `:SPEC:` property + SPECCED state back
    onto the idea. Returns (spec_path, spec_id). Errors if `selector` isn't an idea, or if it's
    already promoted (has a `:SPEC:` property) unless `force`."""
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} resolved to {task.heading!r}, which is not an idea "
                         f"(state {task.state!r})")
    existing = task.properties.get("SPEC")
    if existing and not force:
        raise ValueError(f"idea {task.id} already promoted to {existing} "
                         f"(use --force to re-promote)")
    _refuse_closed_idea(cfg, task, force)

    from .explore import idea_summary_text
    if not idea_summary_text(cfg, task).strip() and not force:
        warn(f"  ! idea {task.id} has empty Summary — explore first "
              f"(`wt idea log {task.id}` / /wt-explore); pass --force to promote anyway")

    if title is None:
        title = _short_title(task.heading)
        if title != task.heading.strip():
            warn(f"  ! long idea heading; used a truncated title (pass --title to override). "
                 f"Full text is in the spec's Context.")
    from . import spec_numbers
    specs_dir = _specs_dir(cfg)
    rec = spec_numbers.allocate(cfg, by="spec new", note=task.id, specs_dir=specs_dir)
    try:
        return _scaffold_allocated(cfg, task, rec, specs_dir, epic, title)
    except BaseException:
        try:
            spec_numbers.release(cfg, rec["number"], specs_dir=specs_dir)
        except Exception:
            pass                      # the original error matters; a stale reservation self-prunes
        raise


def _scaffold_allocated(cfg, task, rec, specs_dir, epic, title):
    """Everything after the number is allocated (SPEC-0208): the caller releases it on error."""
    num = f"{rec['number']:04d}"
    spec_id = rec["id"]
    slug = slugify(title)
    spec_path = specs_dir / f"{num}-{slug}.md"

    template_name = "TEMPLATE-epic.md" if epic else "TEMPLATE.md"
    text = (specs_dir / template_name).read_text()

    created = dt.datetime.now(cfg.get("_tz")).strftime("%Y-%m-%d")
    text = re.sub(r"^id: SPEC-NNNN$", f"id: {spec_id}", text, count=1, flags=re.M)
    text = re.sub(r"^title: .*$", f"title: {_yaml_fm_value(title)}", text, count=1, flags=re.M)
    extra = [f"created: {created}", f"source_idea: {task.id}"]
    idea_epic = task.properties.get("EPIC")
    if idea_epic:
        if idea_epic in known_epics(cfg):
            extra.append(f"parent: {idea_epic}")
        else:
            warn(f"  ! idea {task.id} has :EPIC: {idea_epic!r}, which doesn't resolve to a "
                 f"known kind:epic spec; leaving parent unset")
    text = re.sub(r"^created: YYYY-MM-DD$", "\n".join(extra), text, count=1, flags=re.M)
    context = _idea_context(task)
    text = _CONTEXT_RE.sub(lambda m: m.group(1) + context + m.group(2), text, count=1)

    specs_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(text)

    W.set_property(cfg, task, "SPEC", spec_id)
    W.set_state(cfg, task, "SPECCED")

    return spec_path, spec_id


def promote_idea(cfg, selector, *, internal=False, target=None, epic=False, title=None,
                  force=False):
    """Routing dispatcher in front of scaffold_from_idea/scaffold_outbound (SPEC-0020).

    Resolves `selector` to an idea (must be an idea; honors the already-promoted `:SPEC:`
    guard, same as `scaffold_from_idea`/`scaffold_outbound`), then decides where it lands:
      - `internal`, no project (from `target` or the idea's `:PROJECT:`) or the internal
        project Meta-Tools (SPEC-0189) -> an internal
        `SPEC-NNNN` via `scaffold_from_idea` (kind "internal").
      - the project is a configured `outbox_targets` key -> an outbound `<PROJ>-NNNN` via
        `scaffold_outbound` (kind "outbound").
      - otherwise -> ValueError naming the project + how to configure/override it. Nothing is
        written and the idea is not mutated on this path (raised before either scaffolder runs).

    Returns (spec_path, spec_id, kind) where kind is "internal" or "outbound"."""
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} resolved to {task.heading!r}, which is not an idea "
                         f"(state {task.state!r})")
    existing = task.properties.get("SPEC")
    if existing and not force:
        raise ValueError(f"idea {task.id} already promoted to {existing} "
                         f"(use --force to re-promote)")
    _refuse_closed_idea(cfg, task, force)

    project = target or task.properties.get("PROJECT")

    from .rules import INTERNAL_PROJECT
    if internal or project is None or project == INTERNAL_PROJECT:
        path, spec_id = scaffold_from_idea(cfg, selector, epic=epic, title=title, force=force)
        return path, spec_id, "internal"

    if project in (cfg.get("outbox_targets") or {}):
        path, spec_id = scaffold_outbound(cfg, project, from_idea=selector, title=title,
                                          force=force, epic=epic)
        return path, spec_id, "outbound"

    raise ValueError(
        f"idea {task.id} is associated with project {project!r}, which has no outbound target. "
        f"Run `wt projects add {project} --repo <path> --yes` (dry-run without --yes), "
        f"or pass --target <proj> / --internal.")


def _merge_wrapped_lines(text):
    """Collapse a hand-wrapped bullet list into one logical line per bullet: a non-blank line
    that doesn't itself start a new bullet (`- …`) is a wrapped continuation of the previous
    line, joined with a space rather than silently dropped (bug found in real specs — e.g.
    EXAMPLE-0030's Goals section wraps `- Every batch … and the\n  consumer PR fan-in row …`
    across two physical lines; same shape as export_schemes.canonical._merge_wrapped_lines,
    duplicated here to keep export_schemes decoupled from the internal spec-generation
    module). Blank lines reset continuation (they mark a paragraph break)."""
    out = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue
        if _BULLET_START_RE.match(raw) or not out or not out[-1]:
            out.append(stripped)
        else:
            out[-1] = f"{out[-1]} {stripped}"
    return out


def _bullet_items(section_text):
    """Bullet lines of a '## Section' body, checkbox/markup stripped, in order."""
    items = []
    for line in _merge_wrapped_lines(section_text):
        m = _BULLET_RE.match(line)
        if m:
            items.append(m.group(1))
    return items


def spec_tasks(spec_path):
    """Extract (title, [item, ...]) from a spec file at `spec_path`: for `kind: epic`, items are
    the breakdown lines under '## Breakdown / sub-specs'; for a feature, items are the
    '## Acceptance criteria' bullets (or a single "implement <title>" task if none). Checkbox/
    markdown markup is stripped from each item."""
    text = Path(spec_path).read_text()
    fm, body = split_frontmatter(text)
    fm = fm or {}
    secs = sections(body)
    title = fm.get("title", "")
    kind = fm.get("kind", "feature")
    section_name = "Breakdown / sub-specs" if kind == "epic" else "Acceptance criteria"
    items = _bullet_items(secs.get(section_name, ""))
    if kind != "epic" and not items:
        items = [f"implement {title}"]
    return title, items


def known_epics(cfg):
    """Sorted ids of every `kind: epic` spec — internal docs/specs and outbound outbox
    (SPEC-0018 + SPEC-0043). Used by `--epic` capture validation and parent wiring."""
    ids = []
    specs_dir = _specs_dir(cfg)
    if specs_dir.exists():
        for path in specs_dir.glob("[0-9]*.md"):
            fm, _ = split_frontmatter(path.read_text())
            fm = fm or {}
            if fm.get("kind") == "epic" and fm.get("id"):
                ids.append(fm["id"])
    root = outbox_dir(cfg)
    if root.exists():
        for path in root.glob("*/*.md"):
            if path.name in ("INDEX.md", "PROVENANCE.md", "README.md"):
                continue
            try:
                fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            fm = fm or {}
            if fm.get("kind") == "epic" and fm.get("id"):
                ids.append(fm["id"])
    return sorted(set(ids))


def _find_spec(specs_dir, spec_id):
    """Resolve a SPEC-NNNN id to its docs/specs/NNNN-slug.md path under `specs_dir`."""
    specs_dir = Path(specs_dir)
    num = spec_id.split("-", 1)[1] if "-" in spec_id else spec_id
    matches = sorted(specs_dir.glob(f"{num}-*.md"))
    if not matches:
        raise ValueError(f"no spec file found for {spec_id!r} in {specs_dir}")
    return matches[0]


def _is_outbound_spec_id(spec_id: str) -> bool:
    """Outbound ids are PROJ-NNNN (not SPEC-NNNN). SPEC-0041: generate refuses these."""
    return bool(spec_id) and not str(spec_id).upper().startswith("SPEC-")


def generate_from_spec(cfg, spec_id, *, file=None, key=None):
    """Resolve `spec_id` to its spec file, extract its tasks (`spec_tasks`), generate org tasks
    (`org_write.generate_tasks`) into `file` (or cfg["org_capture_file"]), and — if the spec has
    a `source_idea` — advance that idea to PROMOTED. Returns (path, added_count).

    SPEC-0041: outbound ids are refused — use `wt spec export` for the external handoff.
    """
    if _is_outbound_spec_id(spec_id):
        raise ValueError(
            f"{spec_id} is an outbound spec — use `wt spec export {spec_id}` "
            f"(generate is for internal SPEC-NNNN only; see SPEC-0041)"
        )
    spec_path = _find_spec(_specs_dir(cfg), spec_id)
    text = spec_path.read_text()
    fm, _ = split_frontmatter(text)
    fm = fm or {}
    title, items = spec_tasks(spec_path)

    path, added = W.generate_tasks(cfg, {"id": spec_id, "title": title}, items,
                                   file=file, key=key)

    source_idea = fm.get("source_idea")
    if source_idea:
        idea = resolve_selector(cfg, source_idea)
        W.set_state(cfg, idea, "PROMOTED")

    return path, added


# ---- Outbound spec store (SPEC-0015) -------------------------------------------------

_PROJ_NUM_RE = re.compile(r"^[A-Z0-9]+-(\d{4})-")
OUTBOUND_REQUIRED_SECTIONS = ["Context", "Decision", "Acceptance criteria", "Test plan"]
OUTBOUND_REQUIRED_FRONTMATTER = ["id", "title", "status", "owner", "created",
                                 "target_project", "target_repo"]

OUTBOX_BEGIN = "<!-- outbox:begin (generated by wt — do not edit by hand) -->"
OUTBOX_END = "<!-- outbox:end -->"
_OUTBOX_REGION_RE = re.compile(re.escape(OUTBOX_BEGIN) + r".*?" + re.escape(OUTBOX_END), re.S)


def outbox_dir(cfg):
    """The outbound spec store root (SPEC-0021): cfg['outbox_dir'] if set, else
    <data_dir>/outbox — USER DATA (like mappings/history), never the wt source tree. Outbound
    specs are content for other projects; they don't belong in wt's own repo. Injectable so
    tests write to a tmp dir."""
    base = cfg.get("outbox_dir") or (Path(cfg["data_dir"]) / "outbox")
    return Path(base)


def outbound_project(path, fm) -> str:
    """The project an outbox spec targets (SPEC-0243): its `target_project`, else the name of the
    outbox directory it sits in."""
    return (fm or {}).get("target_project") or Path(path).parent.name


def _proj_code(project):
    """Uppercased short code for a project name, e.g. 'acme' -> 'ACME'."""
    code = re.sub(r"[^A-Za-z0-9]+", "", project).upper()
    return code or "PROJ"


def next_outbound_number(project_dir, proj_code):
    """Max existing <PROJ>-NNNN-*.md under project_dir + 1, zero-padded to 4 digits."""
    project_dir = Path(project_dir)
    nums = []
    for p in project_dir.glob(f"{proj_code}-[0-9]*.md"):
        m = _PROJ_NUM_RE.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return f"{(max(nums) + 1) if nums else 1:04d}"


def _target_repo(cfg, project):
    target = (cfg.get("outbox_targets") or {}).get(project, {})
    return target.get("repo_path", project)


def scaffold_outbound(cfg, project, *, from_idea=None, title=None, force=False, epic=False):
    """Scaffold a new outbound spec at <data_dir>/outbox/<project>/<PROJ>-NNNN-slug.md (draft)
    from TEMPLATE.md (or TEMPLATE-epic.md when `epic=True`, SPEC-0043), extending the internal
    shape with `target_project`/`target_repo` (+ `source_idea` when `from_idea` is given). If
    `from_idea` resolves to an org idea, prefills Context from its subtree and writes the
    reciprocal `:SPEC:` property + SPECCED state back onto the idea (same convention as
    `scaffold_from_idea`). Regenerates outbox INDEX.md.
    Returns (spec_path, outbound_id)."""
    task = None
    if from_idea:
        task = resolve_selector(cfg, from_idea)
        if not task.is_idea:
            raise ValueError(f"{from_idea!r} resolved to {task.heading!r}, which is not an idea "
                             f"(state {task.state!r})")
        _refuse_closed_idea(cfg, task, force)
        from .explore import idea_summary_text
        if not idea_summary_text(cfg, task).strip() and not force:
            warn(f"  ! idea {task.id} has empty Summary — explore first "
                  f"(`wt idea log {task.id}` / /wt-explore); pass --force to promote anyway")
        title = title or task.heading
    if not title:
        raise ValueError("title is required unless --from-idea is given")

    proj_code = _proj_code(project)
    project_dir = outbox_dir(cfg) / project
    project_dir.mkdir(parents=True, exist_ok=True)
    num = next_outbound_number(project_dir, proj_code)
    outbound_id = f"{proj_code}-{num}"
    slug = slugify(title)
    spec_path = project_dir / f"{proj_code}-{num}-{slug}.md"

    specs_dir = _specs_dir(cfg)
    template_name = "TEMPLATE-epic.md" if epic else "TEMPLATE.md"
    text = (specs_dir / template_name).read_text()

    created = dt.datetime.now(cfg.get("_tz")).strftime("%Y-%m-%d")
    target_repo = _target_repo(cfg, project)
    target_cfg = (cfg.get("outbox_targets") or {}).get(project, {})
    spec_dir_rel = target_cfg.get("spec_dir", "docs/specs")
    target_spec_path = str(Path(target_repo).expanduser() / spec_dir_rel / spec_path.name)
    text = re.sub(r"^id: SPEC-NNNN$", f"id: {outbound_id}", text, count=1, flags=re.M)
    text = re.sub(r"^title: .*$", f"title: {_yaml_fm_value(title)}", text, count=1, flags=re.M)
    extra = [f"created: {created}", f"target_project: {project}", f"target_repo: {target_repo}",
             f"target_spec_path: {target_spec_path}"]
    if task is not None:
        extra.append(f"source_idea: {task.id}")
    text = re.sub(r"^created: YYYY-MM-DD$", "\n".join(extra), text, count=1, flags=re.M)
    # Feature TEMPLATE has no kind:; epic template already has kind: epic.
    if epic and not re.search(r"^kind:\s*epic\s*$", text, flags=re.M):
        text = re.sub(r"^status: draft$", "status: draft\nkind: epic", text, count=1, flags=re.M)

    if task is not None:
        context = _idea_context(task)
        text = _CONTEXT_RE.sub(lambda m: m.group(1) + context + m.group(2), text, count=1)

    spec_path.write_text(text)

    if task is not None:
        W.set_property(cfg, task, "SPEC", outbound_id)
        W.set_property(cfg, task, "TARGET_REPO", target_repo)
        W.set_property(cfg, task, "TARGET_SPEC_PATH", target_spec_path)
        W.set_state(cfg, task, "SPECCED")

    write_outbox_index(cfg)

    return spec_path, outbound_id


def resolve_spec_path(cfg, spec_id):
    """Internal or outbound spec path for `spec_id`, or raise ValueError."""
    if not spec_id:
        raise ValueError("spec id is required")
    if _is_outbound_spec_id(spec_id):
        root = outbox_dir(cfg)
        matches = sorted(root.glob(f"*/{spec_id}-*.md")) if root.exists() else []
        if not matches:
            raise ValueError(f"no outbound spec found for {spec_id!r} under {root}")
        if len(matches) > 1:
            raise ValueError(f"ambiguous outbound id {spec_id!r} -> {matches}")
        return matches[0]
    return _find_spec(_specs_dir(cfg), spec_id)


def _git_out(cwd, *args):
    """Stdout of `git -C cwd <args>` (stripped), or None when git fails, times out or is absent."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _git_checkout(cwd):
    """`(toplevel, common_dir)` of the git checkout containing `cwd` (both resolved), or None.
    Uses `--path-format=absolute` (git >= 2.31) and resolves a relative answer against `cwd`
    when the flag is unsupported."""
    top = _git_out(cwd, "rev-parse", "--show-toplevel")
    common = (_git_out(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
              or _git_out(cwd, "rev-parse", "--git-common-dir"))
    if not top or not common:
        return None
    return Path(top).resolve(), (Path(cwd) / common).resolve()


def resolve_spec_path_for_cwd(cfg, spec_id, cwd=None):
    """`(path, origin)` for `wt spec review` (SPEC-0227): like `resolve_spec_path`, but inside a
    checkout (main or linked worktree) of the registered specs directory's repository the spec
    comes from *that* checkout. `origin` is `outbox`, `main checkout`, `worktree <branch>` or
    `registered checkout`. A same-repository checkout without the file is an error, never a
    silent fallback to another copy."""
    if not spec_id:
        raise ValueError("spec id is required")
    if _is_outbound_spec_id(spec_id):
        return resolve_spec_path(cfg, spec_id), "outbox"
    reg_dir = _specs_dir(cfg)
    reg = _git_checkout(reg_dir) if Path(reg_dir).is_dir() else None
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    cur = _git_checkout(cwd) if reg and cwd.is_dir() else None
    if not (reg and cur and cur[1] == reg[1]):
        return resolve_spec_path(cfg, spec_id), "registered checkout"
    reg_top, cur_top = reg[0], cur[0]
    if cur_top == reg_top:
        origin = "main checkout"
    else:
        branch = _git_out(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "detached"
        origin = f"worktree {'detached' if branch == 'HEAD' else branch}"
    try:
        return _find_spec(cur_top / Path(reg_dir).resolve().relative_to(reg_top), spec_id), origin
    except ValueError:
        pass
    msg = f"no spec file for {spec_id} in this checkout ({cur_top}, {origin})"
    try:
        msg += f"; the registered checkout has {resolve_spec_path(cfg, spec_id)}"
    except ValueError:
        pass
    raise ValueError(msg + " — run from there, or use wt spec review --path FILE")


SPEC_STATUSES = ("draft", "proposed", "accepted", "in-progress", "done", "superseded",
                 "rejected")

_FM_BLOCK_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_STATUS_LINE_RE = re.compile(r"^status:\s*(\S+)\s*$", re.M)


def set_spec_status(path, status, *, today=None):
    """Set a spec's `status:` (SPEC-0179), refreshing `updated:`; returns `(old, new)`.

    Edits only the `status:` and `updated:` frontmatter lines (`updated:` is inserted after
    `created:` when absent); the rest of the file is byte-identical. No lifecycle validation —
    `wt spec verify` and the linter own that."""
    if status not in SPEC_STATUSES:
        raise ValueError(f"invalid status {status!r} (expected one of {', '.join(SPEC_STATUSES)})")
    path = Path(path)
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n") if crlf else raw
    fm, _ = split_frontmatter(text)
    block = _FM_BLOCK_RE.match(text)
    if fm is None or not block:
        raise ValueError("no frontmatter")
    head = block.group(1)
    m = _STATUS_LINE_RE.search(head)
    if not m:
        raise ValueError("no status: line in frontmatter")
    old = m.group(1).strip("\"'")
    today = today or dt.date.today().isoformat()
    head = head[:m.start()] + f"status: {status}" + head[m.end():]
    if re.search(r"^updated:", head, re.M):
        head = re.sub(r"^updated:.*$", lambda _m: f"updated: {today}", head, count=1, flags=re.M)
    else:
        created = re.search(r"^created:.*$", head, re.M)
        if not created:
            raise ValueError("no created: line to insert updated: after")
        head = head[:created.end()] + f"\nupdated: {today}" + head[created.end():]
    out = text[:block.start(1)] + head + text[block.end(1):]
    path.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
    return old, status


def spec_status(path):
    """`{"status", "updated"}` from a spec's frontmatter (values as strings, or None)."""
    fm, _ = split_frontmatter(Path(path).read_text(encoding="utf-8"))
    if fm is None:
        raise ValueError("no frontmatter")
    status, updated = fm.get("status"), fm.get("updated")
    return {"status": None if status is None else str(status),
            "updated": None if updated is None else str(updated)}


def _breakdown_idea_title(item: str) -> str:
    """Short idea heading from a Breakdown bullet (SPEC-0043)."""
    text = " ".join((item or "").split())
    for sep in (" — ", " -- ", " – "):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    if len(text) > 100:
        text = text[:97].rstrip() + "..."
    return text or "untitled breakdown item"


# SPEC-0097: optional `(project: Name)` on a Breakdown bullet (anywhere in the raw line).
_PROJECT_ANNOT_RE = re.compile(r"\(\s*project:\s*([^)]+?)\s*\)", re.IGNORECASE)


def parse_breakdown_project(item: str) -> tuple[str, str | None]:
    """Split optional `(project: Name)` from a Breakdown bullet (SPEC-0097).

    Returns `(item_without_annotation, project_or_None)`. Annotation may appear anywhere in
    the raw bullet text; removed before `_breakdown_idea_title`.
    """
    text = item or ""
    m = _PROJECT_ANNOT_RE.search(text)
    if not m:
        return text, None
    project = m.group(1).strip()
    cleaned = f"{text[:m.start()]} {text[m.end():]}"
    cleaned = " ".join(cleaned.split())
    return cleaned, project or None


def _canonical_seed_project(cfg, name: str) -> str:
    """Resolve annotation to a known project name or raise (SPEC-0097). No soft-warn."""
    from .rules import registered_projects

    known = registered_projects(cfg)
    by_cf = {n.casefold(): n for n in known}
    hit = by_cf.get(str(name).strip().casefold())
    if not hit:
        listing = ", ".join(known) if known else "(none)"
        raise ValueError(
            f"unknown project {name!r} in Breakdown (project: …) annotation; "
            f"known: {listing}"
        )
    return hit


def seed_ideas_from_epic(cfg, epic_id, *, state="INCUBATE", tags=()):
    """Create INCUBATE ideas from an epic's Breakdown bullets (SPEC-0043 + SPEC-0097).

    Each new idea gets `:EPIC: <epic_id>`. `:PROJECT:` comes from an optional
    `(project: Name)` annotation on the bullet, else outbound `target_project` when present.
    Unknown annotated names hard-fail. Idempotent: skips headings that already exist under
    the same epic. Returns list of newly created idea ids.
    """
    from .explore import set_summary
    from .org import load_tasks

    path = resolve_spec_path(cfg, epic_id)
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    fm = fm or {}
    if str(fm.get("kind") or "").lower() != "epic":
        raise ValueError(f"{epic_id} is not kind: epic (got {fm.get('kind')!r})")

    _, items = spec_tasks(path)
    if not items:
        return []

    default_project = (fm.get("target_project") or "").strip() or None
    existing_headings = {
        (t.heading or "").casefold().strip()
        for t in load_tasks(cfg)
        if t.is_idea and (t.properties.get("EPIC") or "").strip() == epic_id
    }

    added = []
    for item in items:
        cleaned, annotated = parse_breakdown_project(item)
        if annotated is not None:
            project = _canonical_seed_project(cfg, annotated)
        else:
            project = default_project
        title = _breakdown_idea_title(cleaned)
        key = title.casefold().strip()
        if key in existing_headings:
            continue
        _path, _hl, idea_id = W.add_idea(
            cfg, title, state=state, tags=tuple(tags) if tags else (),
            project=project, epic=epic_id,
        )
        set_summary(cfg, idea_id, f"Child of epic `{epic_id}`.\n\n{item.strip()}")
        existing_headings.add(key)
        added.append(idea_id)
    return added


def validate_outbound(paths):
    """Light validator for outbound specs (schema/section presence only — no jsonschema, no
    ledger; the id namespace is PROJ-NNNN, not SPEC-NNNN, so it deliberately does not reuse
    tools/spec_lint.py's schema). Returns a list of 'file: problem' strings (empty = OK)."""
    problems = []
    for path in paths:
        path = Path(path)
        text = path.read_text()
        fm, body = split_frontmatter(text)
        if fm is None:
            problems.append(f"{path.name}: no YAML frontmatter block")
            continue
        for key in OUTBOUND_REQUIRED_FRONTMATTER:
            if not fm.get(key):
                problems.append(f"{path.name}: missing required frontmatter '{key}'")
        secs = sections(body)
        for sec in OUTBOUND_REQUIRED_SECTIONS:
            if sec not in secs:
                problems.append(f"{path.name}: missing required section '## {sec}'")
    return problems


def _outbound_specs(cfg):
    """All outbound spec files under the outbox store, parsed lightly, sorted by
    (project, id)."""
    root = outbox_dir(cfg)
    out = []
    if not root.exists():
        return out
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in sorted(project_dir.glob("*.md")):
            fm, _ = split_frontmatter(path.read_text())
            fm = fm or {}
            out.append({"project": project_dir.name, "path": path, "fm": fm})
    return out


def write_outbox_index(cfg):
    """Regenerate <data_dir>/outbox/INDEX.md: a light, project-grouped table of outbound specs
    (its own begin/end markers — never docs/LEDGER.md, never scanned by tools/spec_lint.py)."""
    root = outbox_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / "INDEX.md"
    specs = _outbound_specs(cfg)

    lines = []
    by_project = {}
    for s in specs:
        by_project.setdefault(s["project"], []).append(s)
    if not by_project:
        lines.append("_No outbound specs yet._")
    for project in sorted(by_project):
        lines.append(f"### {project}")
        lines.append("")
        lines.append("| ID | Title | Kind | Status | Target repo | Updated |")
        lines.append("|----|-------|------|--------|--------------|---------|")
        for s in by_project[project]:
            fm = s["fm"]
            lines.append(f"| {fm.get('id', '')} | {fm.get('title', '')} | "
                         f"{fm.get('kind', 'feature')} | "
                         f"{fm.get('status', '')} | {fm.get('target_repo', '')} | "
                         f"{fm.get('updated', fm.get('created', ''))} |")
        lines.append("")

    region = f"{OUTBOX_BEGIN}\n\n" + "\n".join(lines).strip() + f"\n\n{OUTBOX_END}"
    header = ("# Outbound spec index\n\n"
              "Generated by `wt spec new --target` / `write_outbox_index` — do not hand-edit "
              "the marked block. Not the internal ledger (docs/LEDGER.md); these specs are a "
              "parallel namespace under the wt data-dir outbox and are not scanned by "
              "tools/spec_lint.py.\n\n")
    if index_path.exists():
        existing = index_path.read_text()
        if _OUTBOX_REGION_RE.search(existing):
            index_path.write_text(_OUTBOX_REGION_RE.sub(lambda m: region, existing))
            return index_path
    index_path.write_text(header + region + "\n")
    return index_path


# ---- state/spec reconciliation (SPEC-0078) ----------------------------------------------

# What a spec's `status` implies for its idea. Pre-terminal idea states only — an idea already
# past these is reported, never rewritten (rewinding would erase a human decision, and
# `next_step_for_idea` short-circuits on PROMOTED so a wrong write never self-corrects).
_PRE_TERMINAL = frozenset({"IDEA", "INCUBATE", "SPECCED"})
_UNSTARTED = frozenset({"IDEA", "INCUBATE"})
# Statuses that justify an idea already being in a terminal state. `accepted`/`in-progress`
# belong here: export advances an idea to EXPORTED while the outbox spec stays `accepted` until
# pulled (SPEC-0041), and `wt spec generate` advances to PROMOTED off an `accepted` spec. Only a
# still-being-written spec (draft/proposed) under a terminal idea is genuine drift.
_TERMINAL_OK = frozenset({"accepted", "in-progress", "done", "superseded", "rejected"})
_DONE_STATUSES = frozenset({"done"})


def _implied_state(status, state, *, outbound=False):
    """The idea state `status` implies, or None for "leave alone" (SPEC-0078 / SPEC-0135).

    Reuses workflow's status vocabularies, plus the `superseded`/`rejected` cases those sets
    deliberately omit. SPEC-0135: outbound `done` → SHIPPED (not PROMOTED); `EXPORTED` +
    `done` may advance to SHIPPED (the one terminal→terminal advance reconcile allows).
    """
    from .workflow import _AUthxx, _DONE, _READY

    state = (state or "").upper()
    if state == "EXPORTED" and status in _DONE:
        return "SHIPPED"
    if state not in _PRE_TERMINAL:
        return None                      # other terminals: never rewound
    if status in _DONE:
        return "SHIPPED" if outbound else "PROMOTED"
    if status in ("superseded", "rejected"):
        return "DROPPED"
    if status in _READY:
        # An accepted/building spec whose idea was left behind gets pulled up to SPECCED.
        return "SPECCED" if state in _UNSTARTED else None
    if status in _AUthxx:
        return None                      # still being written; the idea is not behind
    return None                          # unknown status: report nothing, invent nothing


def _sole_claimant(ideas, spec_id):
    """The single idea whose `:SPEC:` is `spec_id`, or None when 0 or >1 claim it."""
    claims = [t for t in ideas if (t.properties.get("SPEC") or "").strip() == spec_id]
    return claims[0] if len(claims) == 1 else None


def reconcile_ideas(cfg, *, apply=False):
    """Compare every `:SPEC:`-linked idea's state to its spec's `status` (SPEC-0078).

    Returns `[(idea_id, spec_id, action, detail)]`, action in `advance` / `flag-ahead` /
    `flag-no-backlink` / `unchanged` / `missing`. Writes only when `apply` — the default is a
    dry run, because applying `PROMOTED`/`DROPPED` archives the subtree (SPEC-0073).

    Resolution goes through `workflow._load_spec_fm`, which already handles internal
    (`docs/specs/NNNN-*.md`) and outbound (`<outbox>/<project>/PROJ-NNNN-*.md`) ids alike, so
    both provenance kinds reconcile in one pass with no new stored data."""
    from .org import filter_tasks, load_tasks
    from .workflow import _is_outbound_id, _load_spec_fm

    ideas = [t for t in filter_tasks(load_tasks(cfg), is_idea=True)
             if (t.properties.get("SPEC") or "").strip()]
    plan = []                       # uniform (idea_id, spec_id, action, detail) rows
    advances = []                   # (idea_id, target_state) for the write pass
    backlinks = []                  # (spec_id, idea_id) for the write pass
    for task in sorted(ideas, key=lambda t: (t.properties.get("ID") or "", t.file, t.line)):
        idea_id = task.properties.get("ID") or task.id
        spec_id = (task.properties.get("SPEC") or "").strip()
        state = (task.state or "").upper()
        fm = _load_spec_fm(cfg, spec_id)
        if fm is None:
            plan.append((idea_id, spec_id, "missing", "spec not found on disk"))
            continue
        status = str(fm.get("status") or "").lower()

        want = _implied_state(status, state, outbound=_is_outbound_id(spec_id))
        # EXPORTED→SHIPPED is allowed; other terminals ahead of a non-ready spec are flags.
        terminal_ok = state in _PRE_TERMINAL or (state == "EXPORTED" and want == "SHIPPED") \
            or state == "SHIPPED"
        if not terminal_ok and status and status not in _TERMINAL_OK:
            plan.append((idea_id, spec_id, "flag-ahead",
                         f"idea is {state} but spec is {status}"))
        elif want and want != state:
            plan.append((idea_id, spec_id, "advance", f"{state} → {want} (spec {status})"))
            advances.append((idea_id, want))
        else:
            plan.append((idea_id, spec_id, "unchanged", f"{state} / spec {status or '?'}"))

        # SPEC-0080: a landed spec whose idea still carries open questions. Report-only —
        # whether a spec answered a question is not derivable, so nothing here is ever written.
        if status in _DONE_STATUSES:
            from .explore import open_question_count
            n_open = open_question_count(cfg, task)
            if n_open:
                plan.append((idea_id, spec_id, "questions-open",
                             f"spec is done but {n_open} question(s) still open"))

        if not str(fm.get("source_idea") or "").strip():
            sole = _sole_claimant(ideas, spec_id)
            plan.append((idea_id, spec_id, "flag-no-backlink",
                         "spec has no source_idea" if sole
                         else "spec has no source_idea (ambiguous: >1 idea claims it)"))
            if sole is not None:
                backlinks.append((spec_id, idea_id))

    if not apply:
        return plan

    # Write pass. Re-resolve per write (`set_state_by_selector` / `set_idea_state` does): each
    # write can shift line numbers, and PROMOTED/DROPPED/SHIPPED archive the subtree.
    from . import explore as EX
    for idea_id, target in advances:
        EX.set_idea_state(cfg, idea_id, target)
    for spec_id, idea_id in backlinks:
        _write_source_idea(cfg, spec_id, idea_id)
    return plan


def _write_source_idea(cfg, spec_id, idea_id):
    """Add `source_idea: <idea_id>` to a spec that has no back-link (SPEC-0078). A spec with no
    back-link is invisible to `check_source_idea_promotion` and cannot be reconciled.

    Inserts a single line immediately before the frontmatter's closing `---` rather than
    re-dumping the parsed YAML: a round-trip through `yaml.safe_dump` rewrites the whole block
    (flow-style `tags: [a, b]` becomes a bullet list, double quotes become single), producing a
    huge diff on a file this command was only meant to annotate."""
    from .workflow import _resolve_spec_path

    path = _resolve_spec_path(cfg, spec_id)
    if path is None:
        return None
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fm, _body = split_frontmatter(text)
    if str((fm or {}).get("source_idea") or "").strip():
        return None

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None                       # no frontmatter block to extend
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return None
    lines.insert(close, f"source_idea: {idea_id}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path


def open_question_idea_count(cfg) -> int:
    """Ideas whose spec is `done` but which still carry open questions (SPEC-0080)."""
    try:
        return sum(1 for _id, _spec, action, _detail in reconcile_ideas(cfg, apply=False)
                   if action == "questions-open")
    except Exception:
        return 0


def stale_idea_count(cfg) -> int:
    """How many ideas a reconcile would advance (SPEC-0078) — the one definition of "out of
    sync", shared by `wt next` and `wt hub`. Never writes."""
    try:
        return sum(1 for _id, _spec, action, _detail in reconcile_ideas(cfg, apply=False)
                   if action == "advance")
    except Exception:
        return 0
