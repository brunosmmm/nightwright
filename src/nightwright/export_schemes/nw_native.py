"""nw-native export scheme (SPEC-0015, refactored behind the `ExportScheme` seam by
SPEC-0016): identity/portable copy of the outbound spec + a short consumption contract.
This is the DEFAULT scheme, and its rendered output MUST remain byte-for-byte what
SPEC-0015 shipped — tests/test_export.py is the regression guard; do not change the two
render functions below without re-checking it."""
import hashlib

import yaml

from . import OutputFile, register

# Frontmatter fields carried from the outbound spec into the portable export (a foreign agent
# has no use for nightwright-internal routing fields like target_project/target_repo/source_idea).
_PORTABLE_FM_KEYS = ["id", "title", "status", "owner", "created", "updated", "kind", "tags",
                     "depends_on"]


def _content_hash(outbound_id, body):
    """Stable hash over the outbound id + section body (frontmatter excluded, so the hash
    doesn't depend on itself once embedded in the rendered frontmatter)."""
    h = hashlib.sha256()
    h.update(outbound_id.encode())
    h.update(b"\n")
    h.update(body.encode())
    return h.hexdigest()[:16]


def _render_portable(fm, body, outbound_id, content_hash):
    portable = {k: fm[k] for k in _PORTABLE_FM_KEYS if fm.get(k) not in (None, "", [])}
    portable["source_outbound_id"] = outbound_id
    portable["source_content_hash"] = content_hash
    fm_text = yaml.safe_dump(portable, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{fm_text}\n---\n{body}"


def _render_contract(outbound_id, project, source_path, dest_spec_name, content_hash, now):
    return f"""# Consuming this spec — {outbound_id}

This spec was **exported** by `nw` (work-tracking), a separate tool's spec pipeline — it was
not authored in this repo. Treat it as an implementation brief / governing spec (if this repo
has its own spec-first workflow, fold it in per that workflow).

## How to treat it

- Prefer the global agent skill `/nw-implement-spec` (install via `nw skills install` on a
  machine that has `nw`) for the implement → test → done loop on this file.
- Read the whole spec before writing code — it is self-contained (Context / Decision / Design
  / Acceptance criteria / Test plan).
- **This portable file is your working copy** for build progress: update frontmatter `status`
  (`accepted` → `in-progress` → `done`) and check off Acceptance criteria as you go. There is
  no nightwright ledger in this repo.
- Close the loop: implement to the Acceptance criteria and execute the Test plan; don't mark
  anything done without doing so.
- If you deviate from the Design, record the deviation in the spec body (or your own PR) —
  don't diverge silently.
- The nightwright author can later run `nw spec pull-status {outbound_id}` to mirror this file's
  `status` back into their outbox — optional, not automatic.
- Do **not** run `nw spec generate` on this outbound id.

## Provenance

- Source: outbound spec `{outbound_id}` (project `{project}`), `nw`'s canonical copy at
  `{source_path}`.
- Exported: {now}
- Portable spec: `{dest_spec_name}`
- Content hash: `{content_hash}`

This contract + the paired spec were file-dropped by `nw spec export` (SPEC-0015 / SPEC-0041).
There is no live sync back to the source `nw` — this is a point-in-time snapshot, not a
subscription.
"""


class NwNativeScheme:
    name = "nw-native"
    description = ("Identity/portable copy of the outbound spec + a consumption contract "
                  "(SPEC-0015's original shape). Default scheme.")
    required_fields = ("id", "title", "context", "decision", "acceptance", "test_plan")
    artifacts = ("spec", "contract")

    def validate(self, spec):
        problems = []
        if not spec.id:
            problems.append("missing id")
        if not spec.title:
            problems.append("missing title")
        if not spec.context:
            problems.append("missing Context")
        if not spec.decision:
            problems.append("missing Decision")
        if not spec.acceptance:
            problems.append("missing Acceptance criteria")
        if not spec.test_plan:
            problems.append("missing Test plan")
        return problems

    def render(self, ctx, target_cfg, emit=None):
        content_hash = _content_hash(ctx.outbound_id, ctx.body)
        rendered = _render_portable(ctx.fm, ctx.body, ctx.outbound_id, content_hash)
        contract = _render_contract(ctx.outbound_id, ctx.project, ctx.source_path,
                                    ctx.source_path.name, content_hash, ctx.now)
        files = [
            OutputFile(relpath=ctx.source_path.name, content=rendered,
                      artifact="spec", mode="native-spec"),
            OutputFile(relpath=f"{ctx.source_path.stem}.contract.md", content=contract,
                      artifact="contract", mode="contract"),
        ]
        if emit:
            files = [f for f in files if f.artifact in emit]
        return files


register(NwNativeScheme())
