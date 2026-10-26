"""Cross-project spec export (SPEC-0015) + pluggable export schemes (SPEC-0016): resolve an
outbound spec (<data_dir>/outbox/<project>/<PROJ>-NNNN-slug.md), resolve + validate an
`ExportScheme` (CLI `--scheme` > `outbox_targets[project].scheme` > `wt-native`), render its
`OutputFile`s, and file-drop them into the target repo — non-clobbering per each file's
reconciliation `mode` (see `export_schemes/__init__.py`'s `OutputFile` docstring) — recording
a provenance line (now including which scheme ran).

SPEC-0041: successful export advances `source_idea` to EXPORTED; wt-native re-export preserves
dest portable `status`/`updated` when the body hash matches; `pull_status` mirrors portable
status back into the outbox.

File-drop only (no live sync / MCP back-channel — see SPEC-0015's Non-goals)."""
import datetime as dt
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

from .render import warn
from . import export_schemes as ES
from . import org_write as OW
from .org_write import resolve_selector
from . import specmeta
from .specmeta import split_frontmatter
from .specs import outbox_dir, write_outbox_index
from .org import load_tasks, filter_tasks


def _atomic_write(path, content, encoding=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wt-export-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _find_outbound_spec(cfg, outbound_id):
    """Locate <data_dir>/outbox/<project>/<outbound_id>-*.md by id, regardless of project dir name."""
    root = outbox_dir(cfg)
    matches = sorted(root.glob(f"*/{outbound_id}-*.md"))
    if not matches:
        raise ValueError(f"no outbound spec found for {outbound_id!r} under {root}")
    if len(matches) > 1:
        cand = ", ".join(str(p) for p in matches)
        raise ValueError(f"ambiguous outbound id {outbound_id!r} -> {cand}")
    return matches[0]


def _append_provenance(cfg, project, outbound_id, dest_paths, scheme_name, now, forced):
    path = outbox_dir(cfg) / project / "PROVENANCE.md"
    header = (f"# Provenance — {project}\n\n"
              "Append-only record of `wt spec export` runs for this project's outbound "
              "specs (what/where/when + which scheme rendered it), so re-exports are "
              "auditable.\n\n")
    existing = path.read_text() if path.exists() else header
    if not existing.endswith("\n"):
        existing += "\n"
    forced_note = " (--force)" if forced else ""
    paths_str = ", ".join(f"`{p}`" for p in dest_paths)
    line = f"- {now} — {outbound_id} [{scheme_name}] -> {paths_str}{forced_note}\n"
    _atomic_write(path, existing + line)
    return path


def _rewrite_frontmatter(text, updates):
    """Return markdown with frontmatter keys merged from `updates` (None deletes a key)."""
    fm, body = split_frontmatter(text)
    fm = dict(fm or {})
    for key, value in updates.items():
        if value is None:
            fm.pop(key, None)
        else:
            fm[key] = value
    fm_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{fm_text}\n---\n{body}"


def _write_native_spec(dest_path, content, force):
    """wt-native's own non-clobber guard (SPEC-0015): compares the embedded provenance
    frontmatter (source_outbound_id/source_content_hash) rather than raw bytes, so a
    byte-identical re-render of the same source is always a no-op.

    SPEC-0041: when the hash matches and `--force` is not set, preserve the dest portable
    file's `status` / `updated` so a foreign agent's build progress is not clobbered.
    """
    if dest_path.exists():
        existing_text = dest_path.read_text()
        existing_fm, _ = split_frontmatter(existing_text)
        existing_fm = existing_fm or {}
        new_fm, _ = split_frontmatter(content)
        new_fm = new_fm or {}
        same = (existing_fm.get("source_outbound_id") == new_fm.get("source_outbound_id")
                and existing_fm.get("source_content_hash") == new_fm.get("source_content_hash"))
        if not same and not force:
            raise ValueError(
                f"refusing to overwrite {dest_path} — it already exists with different "
                f"provenance (source_outbound_id={existing_fm.get('source_outbound_id')!r}, "
                f"hash={existing_fm.get('source_content_hash')!r}); use --force to overwrite")
        if same and not force:
            overrides = {}
            if existing_fm.get("status") is not None:
                overrides["status"] = existing_fm["status"]
            if existing_fm.get("updated") is not None:
                overrides["updated"] = existing_fm["updated"]
            if overrides:
                content = _rewrite_frontmatter(content, overrides)
            if content == existing_text:
                return
    _atomic_write(dest_path, content)


def _write_owned(dest_path, content, force):
    """Generic non-clobber guard for a file a scheme fully owns: write if absent, no-op if
    byte-identical, refuse a divergent overwrite without --force."""
    if dest_path.exists():
        existing = dest_path.read_text()
        if existing == content:
            return
        if not force:
            raise ValueError(
                f"refusing to overwrite {dest_path} — it already exists with different "
                f"content; use --force to overwrite")
    _atomic_write(dest_path, content)


def _splice(dest_path, splice_id, block_content, force):
    """Merge `block_content` (a '### Deliverable <splice_id>' fragment) into a shared file:
    replace an existing same-id deliverable block in place, append if absent, and leave
    everything else in the file untouched. Refuses to overwrite a divergent existing block
    without --force; re-splicing identical content is a no-op."""
    header_re = re.compile(
        r"^### Deliverable " + re.escape(splice_id) + r"\b.*?(?=^### Deliverable |\Z)",
        re.M | re.S)
    existing = dest_path.read_text() if dest_path.exists() else "# Project plan\n\n"
    m = header_re.search(existing)
    if m:
        old_block = m.group(0)
        if old_block.rstrip() == block_content.rstrip():
            return
        if not force:
            raise ValueError(
                f"refusing to splice {dest_path} — Deliverable {splice_id} already exists "
                f"with different content; use --force to overwrite")
        new_text = existing[:m.start()] + block_content + existing[m.end():]
    else:
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        new_text = existing + sep + block_content
    _atomic_write(dest_path, new_text)


def resolve_scheme(target_cfg, scheme_name):
    """Resolve the scheme to use: explicit `--scheme` > `outbox_targets[project].scheme` >
    `wt-native`."""
    name = scheme_name or (target_cfg or {}).get("scheme") or "wt-native"
    return ES.get(name)


def _advance_source_idea_exported(cfg, fm):
    """SPECCED → EXPORTED when export succeeds (SPEC-0041). No-op if no source_idea."""
    source_idea = (fm.get("source_idea") or "").strip()
    if not source_idea:
        return
    try:
        idea = resolve_selector(cfg, source_idea)
    except ValueError:
        return
    if (idea.state or "").upper() == "EXPORTED":
        return
    OW.set_state(cfg, idea, "EXPORTED")


def _portable_dest_path(cfg, source_path, fm, to=None):
    """Resolve where the wt-native portable spec should live for this outbound.

    An explicit `to` override always wins (unchanged). Otherwise prefers the frozen
    `target_spec_path` frontmatter (SPEC-0065) — the resolved absolute path computed once at
    scaffold time — over live `outbox_targets` config, so a later config change (repo
    moved/repointed) doesn't silently redirect an already-exported spec. Outbound specs
    scaffolded before `target_spec_path` existed fall back to the live-config derivation,
    unchanged from before."""
    project = fm.get("target_project")
    target_cfg = (cfg.get("outbox_targets") or {}).get(project, {})

    if to:
        dest_root = Path(to).expanduser()
        spec_dir_rel = target_cfg.get("spec_dir", "docs/specs")
        return dest_root / spec_dir_rel / source_path.name

    frozen = fm.get("target_spec_path")
    if frozen:
        return Path(frozen).expanduser()

    repo_path = target_cfg.get("repo_path")
    if not repo_path:
        raise ValueError(f"no destination for project {project!r}: pass --to or configure "
                         f"outbox_targets[{project!r}].repo_path")
    dest_root = Path(repo_path).expanduser()
    spec_dir_rel = target_cfg.get("spec_dir", "docs/specs")
    return dest_root / spec_dir_rel / source_path.name


def _git_plumbing_lookup(repo_path: Path, relpath: str) -> str | None:
    """Return file content at `relpath` from the most recent commit that touched it, anywhere
    in `repo_path`'s repo (any branch/worktree, including ones since deleted) — or None if
    `repo_path` isn't a git repo, git isn't available, or no commit ever touched that path
    (SPEC-0130)."""
    try:
        log = subprocess.run(
            ["git", "-C", str(repo_path), "log", "--all", "--format=%H", "-1", "--", relpath],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = log.stdout.strip()
    if log.returncode != 0 or not commit:
        return None
    try:
        show = subprocess.run(["git", "-C", str(repo_path), "show", f"{commit}:{relpath}"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return show.stdout if show.returncode == 0 else None


def _write_temp_copy(content: str, name: str) -> Path:
    """A short-lived tmp file holding `content`, so a spec reconstructed from git history
    (SPEC-0130) can still be read via the same Path.read_text()/.exists() contract as an
    on-disk file — callers never need to know which source it came from."""
    fd, tmp = tempfile.mkstemp(prefix=".wt-portable-", suffix=f"-{name}")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return Path(tmp)


def _resolve_portable_spec(cfg, source_path, fm, *, to=None) -> Path:
    """Find where an already-exported portable spec actually lives (SPEC-0130): the
    configured/frozen path first (unchanged fast path, `_portable_dest_path`), then
    `outbox_targets[project].extra_search_paths`, then a git-plumbing search of `repo_path`'s
    history — covering a spec that only exists in a sibling worktree on an unmerged branch.
    Returns `_portable_dest_path`'s own result unchanged when nothing else is found, so
    callers' existing "not found" `ValueError` fires exactly as before."""
    primary = _portable_dest_path(cfg, source_path, fm, to=to)
    if primary.exists():
        return primary
    project = fm.get("target_project")
    target_cfg = (cfg.get("outbox_targets") or {}).get(project, {})
    for extra in target_cfg.get("extra_search_paths", []):
        candidate = Path(extra).expanduser() / primary.name
        if candidate.exists():
            return candidate
    repo_path = target_cfg.get("repo_path")
    if repo_path:
        spec_dir = target_cfg.get("spec_dir", "docs/specs")
        relpath = f"{spec_dir}/{primary.name}"
        content = _git_plumbing_lookup(Path(repo_path).expanduser(), relpath)
        if content is not None:
            return _write_temp_copy(content, primary.name)
    return primary


def pull_status(cfg, outbound_id, *, to=None):
    """Mirror portable frontmatter `status`/`updated` from the target repo into the outbox
    copy (SPEC-0041). Returns (outbox_path, status). SPEC-0135: the first time this pulls a
    `done` status for an idea still in state EXPORTED, advances it to SHIPPED (which archives
    via set_state's SPEC-0073 hook)."""
    source_path = _find_outbound_spec(cfg, outbound_id)
    text = source_path.read_text()
    fm, body = split_frontmatter(text)
    fm = fm or {}
    status_before = fm.get("status")
    dest_path = _resolve_portable_spec(cfg, source_path, fm, to=to)
    if not dest_path.exists():
        raise ValueError(f"portable spec not found at {dest_path}")
    dest_fm, _ = split_frontmatter(dest_path.read_text())
    dest_fm = dest_fm or {}
    status = dest_fm.get("status")
    if not status:
        raise ValueError(f"{dest_path}: portable spec has no status frontmatter")
    updates = {"status": status}
    if dest_fm.get("updated") is not None:
        updates["updated"] = dest_fm["updated"]
    out_fm = dict(fm)
    out_fm.update(updates)
    fm_text = yaml.safe_dump(out_fm, sort_keys=False, default_flow_style=False).strip()
    new_text = f"---\n{fm_text}\n---\n{body}"
    _atomic_write(source_path, new_text)
    write_outbox_index(cfg)

    if status == "done" and status_before != "done":
        try:
            selector = _resolve_source_idea(cfg, outbound_id)
            idea = resolve_selector(cfg, selector)
            if idea.is_idea and (idea.state or "").upper() == "EXPORTED":
                if OW.ensure_idea_keyword(cfg, idea.file, "SHIPPED"):
                    idea = resolve_selector(cfg, selector)
                OW.set_state(cfg, idea, "SHIPPED")
        except ValueError as e:
            warn(f"  ! {outbound_id}: could not advance linked idea to SHIPPED ({e})")

    return source_path, status


def pull_clock(cfg, outbound_id, *, to=None):
    """Reconcile a portable spec's markdown `## Clock Log` (SPEC-0062) into org `CLOCK:`
    entries on the linked idea (SPEC-0063). Requires `source_idea` on the outbox copy — raises
    if absent (nothing to reconcile onto). Idempotent (add_clock_entry skips duplicates).
    Returns (idea_id, n_added)."""
    from . import explore as EX

    source_path = _find_outbound_spec(cfg, outbound_id)
    fm, _ = split_frontmatter(source_path.read_text())
    fm = fm or {}
    source_idea = (fm.get("source_idea") or "").strip()
    if not source_idea:
        raise ValueError(f"{outbound_id}: no source_idea — nothing to reconcile onto")

    dest_path = _resolve_portable_spec(cfg, source_path, fm, to=to)
    if not dest_path.exists():
        raise ValueError(f"portable spec not found at {dest_path}")
    _dest_fm, dest_body = split_frontmatter(dest_path.read_text())
    clock_log = specmeta.sections(dest_body).get("Clock Log", "")
    pairs = EX.parse_clock_log(clock_log)

    # Safety net: a Clock Log with CLOCK-IN lines that don't all resolve into parsed pairs
    # used to fail silently (bug found against a real DEMO-0001 log) — warn loudly
    # rather than letting `0 added` read as "nothing to do" when there's actually unparsed
    # content. One legitimate still-open trailing CLOCK-IN is not an error (docstring above).
    n_clock_in = sum(1 for line in clock_log.splitlines() if line.strip().startswith("CLOCK-IN:"))
    if n_clock_in not in (len(pairs), len(pairs) + 1):
        warn(f"  ! {outbound_id}: Clock Log has {n_clock_in} CLOCK-IN line(s) but only "
              f"{len(pairs)} parsed as complete pairs — check timestamp format in {dest_path}")

    added = 0
    for start, end in pairs:
        _path, was_added = OW.add_clock_entry(cfg, source_idea, start, end)
        if was_added:
            added += 1
    return source_idea, added


def _resolve_source_idea(cfg, spec_id):
    """Resolve `spec_id` (internal `SPEC-NNNN` or outbound `PROJ-NNNN`, SPEC-0069) to its
    source idea selector: via `source_idea` frontmatter, or a fallback scan for an idea whose
    `:SPEC:` matches `spec_id`. Shared by `fit_log` and `postmortem`."""
    from .specs import resolve_spec_path
    from .org import filter_tasks, load_tasks

    source_path = resolve_spec_path(cfg, spec_id)
    fm, _ = split_frontmatter(source_path.read_text())
    fm = fm or {}
    selector = (fm.get("source_idea") or "").strip()
    if selector:
        return selector

    matches = [
        t for t in filter_tasks(load_tasks(cfg), is_idea=True)
        if (t.properties.get("SPEC") or "").strip() == spec_id
    ]
    if not matches:
        raise ValueError(
            f"no source idea linked to {spec_id!r} "
            f"(set source_idea on the spec or :SPEC: on an idea)")
    if len(matches) > 1:
        ids = ", ".join((t.properties.get("ID") or t.id) for t in matches)
        raise ValueError(f"ambiguous ideas for {spec_id!r}: {ids}")
    return matches[0].properties.get("ID") or matches[0].id


def fit_log(cfg, outbound_id, note):
    """Append a post-ship fit/miss note to the source idea's Log (SPEC-0049). Works for
    internal or outbound spec ids alike (SPEC-0069) via `_resolve_source_idea`. Returns
    (idea_id, path)."""
    from . import explore as EX

    note = (note or "").strip()
    if not note:
        raise ValueError("--note is required")
    if not note.lower().startswith("fit"):
        note = f"fit-log: {note}"

    selector = _resolve_source_idea(cfg, outbound_id)
    path = EX.append_log(cfg, selector, note)
    idea = resolve_selector(cfg, selector)
    idea_id = idea.properties.get("ID") or idea.id
    return idea_id, path


def postmortem(cfg, spec_id, note):
    """Append a post-mortem/defect note to a spec's linked source idea's Log (SPEC-0069).
    Works for internal (`SPEC-NNNN`) and outbound (`PROJ-NNNN`) specs alike — distinct from
    `fit_log`'s narrower post-ship fit/miss framing. Returns (idea_id, path)."""
    from . import explore as EX

    note = (note or "").strip()
    if not note:
        raise ValueError("--note is required")
    if not note.lower().startswith("postmortem"):
        note = f"postmortem: {note}"

    selector = _resolve_source_idea(cfg, spec_id)
    path = EX.append_log(cfg, selector, note)
    idea = resolve_selector(cfg, selector)
    idea_id = idea.properties.get("ID") or idea.id
    return idea_id, path


def pending_exported_ideas(cfg):
    """Ideas in state EXPORTED whose linked outbound spec's status isn't yet `done` (SPEC-0067).
    Returns a list of (task, outbound_id, target_project) — "pending" mirrors the same status
    check `workflow.next_step_for_idea` already uses for its own outbound hints."""
    tasks = load_tasks(cfg)
    pending = []
    for t in filter_tasks(tasks, state="EXPORTED", is_idea=True):
        outbound_id = (t.properties.get("SPEC") or "").strip()
        if not outbound_id:
            continue
        try:
            source_path = _find_outbound_spec(cfg, outbound_id)
        except ValueError:
            continue
        fm, _ = split_frontmatter(source_path.read_text())
        fm = fm or {}
        if str(fm.get("status") or "").lower() == "done":
            continue
        pending.append((t, outbound_id, fm.get("target_project", "")))
    return pending


def _log_sweep_miss(cfg, project, outbound_id, error):
    """Append a missing-destination record to the project's PROVENANCE.md (SPEC-0067) instead
    of raising — reuses the same append-only file/format `_append_provenance` writes to."""
    path = outbox_dir(cfg) / (project or "unknown") / "PROVENANCE.md"
    header = (f"# Provenance — {project}\n\n"
              "Append-only record of `wt spec export` runs for this project's outbound "
              "specs (what/where/when + which scheme rendered it), so re-exports are "
              "auditable.\n\n")
    existing = path.read_text() if path.exists() else header
    if not existing.endswith("\n"):
        existing += "\n"
    now = dt.datetime.now(cfg.get("_tz")).strftime("%Y-%m-%d %H:%M:%S")
    line = f"- {now} — {outbound_id} sweep: destination missing ({error})\n"
    _atomic_write(path, existing + line)
    return path


def sweep(cfg):
    """Bulk pull-status/pull-clock across every pending EXPORTED idea (SPEC-0067). Reuses
    `pull_status`/`pull_clock` per idea; a missing destination is logged (not raised) and the
    sweep continues. Returns a list of (idea_id, outbound_id, result_text), result_text
    labeled "updated"/"unchanged"/"missing"."""
    results = []
    for task, outbound_id, project in pending_exported_ideas(cfg):
        idea_id = task.properties.get("ID") or task.id
        source_path = _find_outbound_spec(cfg, outbound_id)
        fm_before, _ = split_frontmatter(source_path.read_text())
        status_before = (fm_before or {}).get("status")
        try:
            _path, status_after = pull_status(cfg, outbound_id)
        except ValueError as e:
            _log_sweep_miss(cfg, project, outbound_id, e)
            results.append((idea_id, outbound_id, f"missing: {e}"))
            continue
        try:
            _idea_id, added = pull_clock(cfg, outbound_id)
        except ValueError as e:
            # Only "nothing to reconcile onto" cases are expected here (pull_status just
            # resolved this same destination, so a missing-spec ValueError shouldn't recur) —
            # any other failure (e.g. a Clock Log parse error) must be visible, not silently
            # read as "0 clock entries" (bug found against EXAMPLE-0026/IDEA-212: a timestamp
            # parse crash was masquerading as "+0 clock" instead of surfacing).
            warn(f"  ! {outbound_id}: pull-clock failed — {e}")
            added = 0
        label = "updated" if (status_after != status_before or added > 0) else "unchanged"
        results.append((idea_id, outbound_id, f"{label}: status={status_after}, +{added} clock"))
    return results


def export_spec(cfg, outbound_id, *, scheme=None, emit=None, to=None, force=False,
                task_id=None):
    """Resolve outbound spec `outbound_id`, resolve + validate an `ExportScheme`, render it,
    and write its `OutputFile`s into `to` (or the configured
    `outbox_targets[project]['repo_path']`), appending a provenance record.

    For the default `wt-native` scheme (no `scheme=` given, and no target config override)
    this returns the SPEC-0015 shape: `(dest_spec_path, dest_contract_path)`. Any other
    scheme returns the list of paths written.

    `emit`, if given, is an iterable of artifact names (e.g. `["task-spec"]`) narrowing the
    run to a subset of what the scheme would otherwise emit.

    `task_id` (SPEC-0037) is an optional `X.Y.Z` pin from CLI `--task-id` when a scheme allocates ids.
    """
    source_path = _find_outbound_spec(cfg, outbound_id)
    text = source_path.read_text()
    fm, body = split_frontmatter(text)
    fm = fm or {}
    project = fm.get("target_project")
    if not project:
        raise ValueError(f"{source_path}: missing target_project frontmatter")

    target_cfg = (cfg.get("outbox_targets") or {}).get(project, {})
    scheme_obj = resolve_scheme(target_cfg, scheme)

    canonical = ES.parse_canonical(fm, body, specmeta)
    problems = list(scheme_obj.validate(canonical) or [])
    # SPEC-0048: wt-native warns on missing outcome; strict schemes hard-fail via validate.
    # --force skips product-fit outcome problems for any scheme (legacy escape).
    from .export_schemes.canonical import has_outcome_signal, missing_outcome_problem
    if scheme_obj.name == "wt-native" and not has_outcome_signal(canonical):
        warn(f"  ! {outbound_id}: {missing_outcome_problem(canonical)}")
    if force:
        problems = [p for p in problems if "product-fit outcome" not in p]
    if problems:
        raise ValueError(f"{scheme_obj.name}: spec {outbound_id} fails validation: "
                         + "; ".join(problems))

    dest_root = Path(to).expanduser() if to else None
    if dest_root is None:
        repo_path = target_cfg.get("repo_path")
        if not repo_path:
            raise ValueError(f"no destination for project {project!r}: pass --to or configure "
                             f"outbox_targets[{project!r}].repo_path")
        dest_root = Path(repo_path).expanduser()
    spec_dir_rel = target_cfg.get("spec_dir", "docs/specs")

    now = dt.datetime.now(cfg.get("_tz")).strftime("%Y-%m-%d %H:%M:%S")
    ctx = ES.RenderContext(outbound_id=outbound_id, fm=fm, body=body, canonical=canonical,
                          source_path=source_path, project=project, now=now,
                          dest_root=dest_root, task_id=task_id)

    emit_set = set(emit) if emit else None
    files = scheme_obj.render(ctx, target_cfg, emit=emit_set)
    if not files:
        extra = f" for --emit {sorted(emit_set)}" if emit_set else ""
        raise ValueError(f"{scheme_obj.name}: nothing to emit{extra}")

    written = []
    for f in files:
        if f.mode in ("native-spec", "contract"):
            dest_path = dest_root / spec_dir_rel / f.relpath
        else:
            dest_path = dest_root / f.relpath

        if f.mode == "native-spec":
            _write_native_spec(dest_path, f.content, force)
        elif f.mode == "contract":
            _atomic_write(dest_path, f.content)
        elif f.mode == "splice":
            _splice(dest_path, f.splice_id, f.content, force)
        else:
            _write_owned(dest_path, f.content, force)
        written.append(dest_path)

    _append_provenance(cfg, project, outbound_id, written, scheme_obj.name, now, force)
    _advance_source_idea_exported(cfg, fm)

    if scheme_obj.name == "wt-native":
        spec_path = next((p for p, f in zip(written, files) if f.artifact == "spec"), None)
        contract_path = next((p for p, f in zip(written, files) if f.artifact == "contract"),
                             None)
        return spec_path, contract_path
    return written
