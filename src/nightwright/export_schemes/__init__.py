"""Pluggable export scheme registry (SPEC-0016): an `ExportScheme` interface + `OutputFile`
result type + a small name -> scheme registry, so `nw spec export` can render an outbound
spec into different downstream shapes (default `nw-native`, plus `custom-scheme` and any
manifest-defined `TemplateScheme`). Built-ins register themselves on import of this package
(see the bottom of this file). See docs/specs/0016-pluggable-export-schemes.md."""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass
class OutputFile:
    """One file a scheme wants written into the target repo.

    - `artifact`: the --emit bucket this file belongs to (e.g. "spec", "contract",
      "task-spec", "project-plan") — lets a run be narrowed to a subset of what a scheme
      would otherwise emit.
    - `mode`: how `export.export_spec` should reconcile this against anything already on
      disk at `relpath` (all non-clobbering; see SPEC-0016's Division of labor section):
        "native-spec" — nw-native's own provenance-hash guard (SPEC-0015): a re-render of
                        identical source content is always a no-op; divergent content
                        needs --force.
        "contract"    — an informational snapshot (nw-native's consumption contract);
                        always (re)written, no clobber guard (it isn't a shared/owned
                        artifact another tool depends on byte-for-byte).
        "owned"       — a file this scheme fully owns outright (e.g. a custom-scheme task
                        spec seed): written if absent, left alone if byte-identical,
                        refuses to overwrite divergent content without --force.
        "splice"      — a shared file we do NOT own outright (e.g. project-plan.md): our
                        block (keyed by `splice_id`, e.g. an "X.Y" deliverable id) is
                        merged in — updated in place if present, appended if absent — and
                        everything else in the file is preserved untouched; refuses to
                        overwrite a divergent existing block without --force.
    """
    relpath: str
    content: str
    artifact: str = "spec"
    mode: str = "owned"
    splice_id: Optional[str] = None


@dataclass
class RenderContext:
    """Everything a scheme needs to render an outbound spec: the raw frontmatter/body (for
    schemes like nw-native that must reproduce the source verbatim) plus the parsed,
    scheme-agnostic `CanonicalSpec`.

    `dest_root` (SPEC-0034) is the resolved export destination so schemes that allocate
    on-disk ids (custom-scheme allocated ids) can scan for clashes; None keeps legacy no-scan
    numbering for dry/unit renders.

    `task_id` (SPEC-0037) is an optional CLI pin (`--task-id`); frontmatter `task_id` is
    read from `fm` inside the scheme. CLI pin also means "do not reuse prior Source stamp."
    """
    outbound_id: str
    fm: dict
    body: str
    canonical: "CanonicalSpec"
    source_path: Path
    project: str
    now: str
    dest_root: Optional[Path] = None
    task_id: Optional[str] = None


class ExportScheme(Protocol):
    """The scheme contract. `required_fields` names `CanonicalSpec` attributes the scheme
    needs populated; `validate` returns a list of human-readable problems (empty = ok) —
    export.export_spec calls this BEFORE writing anything. `artifacts` names the --emit
    buckets this scheme can produce."""
    name: str
    description: str
    required_fields: tuple
    artifacts: tuple

    def validate(self, spec) -> list:
        ...

    def render(self, ctx: "RenderContext", target_cfg: dict, emit=None) -> list:
        ...


_REGISTRY: dict = {}


def register(scheme):
    """Register (or replace) a scheme by its `.name`. Returns the scheme (usable as a
    decorator-less call at module import time)."""
    _REGISTRY[scheme.name] = scheme
    return scheme


def get(name):
    """Look up a registered scheme by name; raises ValueError (with the available names)
    if unknown."""
    if name not in _REGISTRY:
        raise ValueError(f"unknown export scheme {name!r}; available: "
                         f"{', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[name]


def available():
    """All registered schemes, sorted by name."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


# Import order matters: CanonicalSpec must exist before the built-in schemes (which import
# it), and the built-ins must be imported (for their module-level `register(...)` calls)
# after `OutputFile`/`register` above are defined.
from .canonical import CanonicalSpec, parse_canonical  # noqa: E402,F401
from . import nw_native as _nw_native  # noqa: E402,F401
from . import template_scheme as _template_scheme  # noqa: E402,F401
