"""SPEC-0185 guard: human output goes through Rich. A bare `print`, `click.echo`,
`sys.stdout.write` or `sys.stderr.write` in `src/nightwright` must be a listed machine-output path."""
import ast
from pathlib import Path
import pytest

pytestmark = [pytest.mark.fast, pytest.mark.tree_guard]

SRC = Path(__file__).resolve().parents[1] / "src" / "nightwright"

# (module relative to src/nightwright, enclosing function) -> why it may write raw
ALLOWED = {
    ("config.py", "<module>"): "pyyaml missing: fails before rich can be relied on",
    ("report.py", "_dump_json"): "JSON emitter",
    ("explore.py", "dump_idea_show_json"): "JSON emitter",
    ("explore.py", "print_idea_show"): "idea show --plain / piped: exact org text",
    ("cli.py", "projects_list_cmd"): "JSON emitter (--json)",
    ("cli.py", "migrate_from_wt_cmd"): "JSON emitter (--json)",
    ("cli.py", "projects_add_cmd"): "JSON emitter (--json)",
    ("cli.py", "projects_rename_cmd"): "JSON emitter (--json)",
    ("cli.py", "spec_status_cmd"): "JSON emitter (--json)",
    ("cli.py", "spec_verify_cmd"): "JSON emitter (--json)",
    ("cli.py", "spec_review_cmd"): "JSON emitter (--json)",
    ("cli.py", "_show_review"): "JSON emitter (--json)",
    ("cli.py", "spec_reviews_cmd"): "JSON emitter (--json)",
    ("cli.py", "spec_reserve_cmd"): "prints the bare SPEC-NNNN / JSON for scripts (SPEC-0208)",
    ("cli.py", "spec_reservations_cmd"): "JSON emitter (--json)",
    ("cli.py", "ideas_group_followups_cmd"): "JSON emitter (--json)",
    ("cli.py", "backups_prune_cmd"): "JSON emitter (--json)",
    ("cli.py", "idea_spec_link_cmd"): "JSON emitter (--json)",
    ("cli.py", "sheet_plan_cmd"): "JSON-only by contract (SPEC-0125)",
    ("cli.py", "sheet_record_cmd"): "JSON-only by contract (SPEC-0125)",
    ("cli.py", "_print_completion"): "shell script for eval",
    ("agentic/cli_projects.py", "projects_config_cmd"): "JSON emitter (--json)",
    ("agentic/cli_queue.py", "on_tick"): "queue run --json-lines stream",
    ("agentic/cli_campaign.py", "campaign_prompt"): "raw prompt meant to be piped",
    ("agentic/cli_campaign.py", "campaign_export"): "--stdout table for piping (SPEC-0236)",
    ("agentic/cli_worker.py", "worker_info"): "worker info: plain/JSON for ssh callers (SPEC-0336)",
    ("agentic/cli_worker.py", "worker_have"): "worker have: one sha per line for ssh callers (SPEC-0336)",
    ("agentic/cli_worker.py", "worker_gc"): "worker gc: JSON summary for ssh callers (SPEC-0336)",
    ("agentic/worker.py", "gate_job"): "stderr diagnostic; stdout is the job frame protocol (SPEC-0336)",
    ("agentic/worker.py", "_fail"): "stderr diagnostic outside the frame protocol (SPEC-0336)",
    ("agentic/worker.py", "_write_fd"): "stderr diagnostic: a stalled frame stdout (SPEC-0336)",
    ("agentic/worker.py", "_start_gc"): "stderr diagnostic: the finish gc did not start (SPEC-0336)",
    ("agentic/worker.py", "ssh_command"): "forced-command refusal on stderr (SPEC-0336)",
    ("agentic/cli_worker.py", "worker_jobs"): "worker jobs: plain/JSON listing for ssh callers (SPEC-0340)",
    ("agentic/cli_worker.py", "worker_claude_check"): "worker claude-check: one JSON line for ssh callers (SPEC-0344)",
    ("agentic/cli_worker.py", "worker_cursor_check"): "worker cursor-check: one JSON line for ssh callers (SPEC-0436)",
    ("agentic/cli_worker.py", "worker_pi_check"): "worker pi-check: one JSON line for ssh callers (SPEC-0465)",
    ("agentic/worker_job.py", "reap"): "stderr diagnostic outside the frame protocol (SPEC-0340)",
    ("agentic/worker_job.py", "_unclaim"): "stderr diagnostic outside the frame protocol (SPEC-0340)",
    ("agentic/worker_job.py", "submit"): "stderr diagnostic outside the frame protocol (SPEC-0340)",
    ("agentic/worker_job.py", "cancel"): "stderr diagnostic: a gate-job is not cancelled here (SPEC-0340)",
    ("agentic/worker_job.py", "_watch"): "stderr diagnostic from the job process (SPEC-0340)",
    ("agentic/worker_job.py", "run_job"): "stderr diagnostic from the job process (SPEC-0340)",
    ("agentic/worker_job.py", "main"): "stderr diagnostic from the job process entry point (SPEC-0340)",
    ("agentic/cli_herdr.py", "herdr_watch"): "one-line no-op notice for the herdr pane/hook, outside the TTY render path (SPEC-0361)",
    ("agentic/cli_herdr.py", "parent_gone"): "one line into herdr_watch.log: the auto-started watcher's stdout is its log file (SPEC-0365)",
    ("agentic/herdr_watch.py", "_guard"): "stderr diagnostic from the herdr watch loop, outside the TTY render path (SPEC-0361/0363)",
    ("agentic/campaign_export.py", "export_tables"): "stderr diagnostic: conductor columns skipped; keeps the exported table on stdout clean",
    ("agentic/cli_conductor.py", "conductor_guard"): "PreToolUse hook JSON decision for the harness, not human output",
    ("agentic/cli_gate.py", "gate_logs"): "gate logs: verbatim log text for piping; Rich markup would mangle it",
    ("agentic/worker.py", "_run"): "stderr diagnostic: the disk watch failed; stdout is the frame protocol (SPEC-0336)",
    ("agentic/worker_update.py", "_stderr"): "stderr diagnostic to the ssh caller, outside the render path",
    ("agentic/cli_worker.py", "serve_cmd"): "daemon startup line on stderr (the server's log), not a TTY render (SPEC-0334)",
    ("agentic/cli_worker.py", "_serve_dashboard"): "daemon startup line on stderr (the server's log), not a TTY render (SPEC-0414)",
    ("agentic/cli_worker.py", "on_change"): "daemon code_changed line on stderr (the server's journal), not a TTY render (SPEC-0404)",
    ("agentic/cli_worker.py", "log"): "daemon tailnet listener state lines on stderr (the server's journal), not a TTY render (SPEC-0432)",
    ("agentic/cli_dashboard.py", "snapshot_cmd"): "dashboard snapshot: plain text/JSON for scripts and ssh callers (SPEC-0401)",
    ("agentic/dashboard/stream.py", "_log_error"): "server daemon log line on stderr (the snapshotter error, rate-limited), not a TTY render (SPEC-0402)",
    ("agentic/cli_queue.py", "queue_install"): "unit text and shell commands printed verbatim (--dry-run), no rich markup (SPEC-0483)",
    ("agentic/cli_queue.py", "queue_uninstall"): "shell commands printed verbatim (SPEC-0483)",
    ("agentic/cli_queue.py", "queue_unit_failed"): "one plain line for the systemd journal (SPEC-0483)",
    ("agentic/cli_dashboard.py", "install_cmd"): "unit text and shell commands printed verbatim (--dry-run), no rich markup (SPEC-0427)",
    ("agentic/cli_dashboard.py", "uninstall_cmd"): "shell commands printed verbatim (SPEC-0427)",
    ("agentic/cli_dashboard.py", "_sh"): "echoes the systemctl command and its output verbatim (SPEC-0427)",
    ("agentic/cli_dashboard.py", "_print_status"): "dashboard status: plain text for scripts and ssh callers (SPEC-0427)",
    ("agentic/cli_dashboard.py", "status_cmd"): "JSON emitter (--json) (SPEC-0427)",
    ("agentic/cli_workers.py", "_add_pull"): "prints the bare enrol blob on stdout for scripts to capture; Rich would mangle it (SPEC-0334)",
    ("agentic/cli_workers.py", "workers_token"): "prints the bare enrol blob on stdout for scripts to capture; Rich would mangle it (SPEC-0334)",
    ("agentic/pull_worker.py", "_log"): "pull-worker daemon log line on stderr, outside the render path (SPEC-0334)",
}


def _is_raw_write(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Name) and f.id == "print":
        return True
    if isinstance(f, ast.Attribute):
        if f.attr == "echo" and isinstance(f.value, ast.Name) and f.value.id == "click":
            return True
        if (f.attr == "write" and isinstance(f.value, ast.Attribute)
                and f.value.attr in ("stdout", "stderr")
                and isinstance(f.value.value, ast.Name) and f.value.value.id == "sys"):
            return True
    return False


def raw_writes(path: Path, rel: str):
    """Yield (rel, innermost function, lineno) for each raw write in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child.name)
            else:
                if isinstance(child, ast.Call) and _is_raw_write(child):
                    yield rel, func, child.lineno
                yield from walk(child, func)

    yield from walk(tree, "<module>")


def test_no_unlisted_raw_writes():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for rel_, func, line in raw_writes(path, rel):
            if (rel_, func) not in ALLOWED:
                bad.append(f"src/nightwright/{rel_}:{line} in {func}()")
    assert not bad, ("raw output outside the allowlist — print through nightwright.console / "
                     "nightwright.render (warn, print_code, …) or, for a machine path, add it to "
                     "ALLOWED with a reason:\n  " + "\n  ".join(bad))


def test_allowlist_has_no_stale_entries():
    seen = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        seen.update((r, f) for r, f, _ in raw_writes(path, rel))
    assert not set(ALLOWED) - seen, set(ALLOWED) - seen


def test_guard_catches_a_bare_print(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import sys\ndef f():\n    print('hi')\n    sys.stderr.write('x')\n")
    assert [(f, line) for _, f, line in raw_writes(p, "m.py")] == [("f", 3), ("f", 4)]

