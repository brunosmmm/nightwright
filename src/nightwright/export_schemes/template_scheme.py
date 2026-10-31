"""TemplateScheme (SPEC-0016 declarative tier): a manifest-driven `ExportScheme` — section
order + label mapping + naming + required fields, no Python. A new simple scheme is just a
`manifests/<name>.toml` file; this module loads every manifest under `manifests/` and
registers a `TemplateScheme` instance for each on import.

SPEC-0058: also loads user manifests from `<config_dir>/schemes/*.toml` — no nightwright code change
needed for a new org-specific declarative scheme. `paths.config_dir()` is env-var/XDG-only
(no `cfg` object needed), so this runs at the same module-import point as the built-in load.
Loaded after the built-ins, so a same-named user manifest overrides a built-in (register()'s
last-write-wins).

SPEC-0059: a section may also be `static` — fixed boilerplate text (org-specific contracts,
review style, verification scripts) with `{{field}}` / `{{field:checkbox}}` placeholders
substituted from `CanonicalSpec`, for templates where nightwright-sourced content is interleaved with
content nightwright can't derive (e.g. a richer org-specific task-spec template)."""
import re
import tomllib
from pathlib import Path

from . import OutputFile, register
from .. import paths
from ..specs import slugify

_MANIFEST_DIR = Path(__file__).parent / "manifests"
_USER_SCHEMES_DIR = Path(paths.config_dir()) / "schemes"
_FILL = "<!-- fill -->"
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)(?::(checkbox))?\}\}")


def _render_value(value, *, checkbox=False):
    """Same list/string rendering rule `source`-based sections use, reused for placeholder
    substitution inside a `static` block."""
    if isinstance(value, list):
        if not value:
            return _FILL
        fmt = (lambda it: f"- [ ] {it}") if checkbox else (lambda it: f"- {it}")
        return "\n".join(fmt(it) for it in value)
    text = (value or "").strip()
    return text or _FILL


def _substitute(static_text, spec):
    """Replace every `{{field}}` / `{{field:checkbox}}` in `static_text` with the named
    `CanonicalSpec` attribute, rendered the same way a `source` section would."""
    def repl(m):
        field, checkbox = m.group(1), m.group(2)
        return _render_value(getattr(spec, field, ""), checkbox=bool(checkbox))
    return _PLACEHOLDER_RE.sub(repl, static_text)


class TemplateScheme:
    """A scheme fully described by a manifest dict (see manifests/*.toml for the shape):

        name, description, required_fields, artifact, naming, sections: [{label, source,
        list?, checkbox?}, ...]

    `source` names a `CanonicalSpec` attribute; `list`/`checkbox` control how a list-typed
    source is rendered (plain bullets vs. binary checkboxes). `origin` (SPEC-0058) is
    "built-in" or "user-config", for `nw spec schemes` display only — it doesn't affect
    rendering."""

    def __init__(self, manifest, *, origin="built-in"):
        self._m = manifest
        self.name = manifest["name"]
        self.description = manifest.get("description", "")
        self.required_fields = tuple(manifest.get("required_fields", ()))
        self.artifacts = (manifest.get("artifact", "spec"),)
        self.origin = origin

    def validate(self, spec):
        problems = []
        for field_name in self.required_fields:
            if not getattr(spec, field_name, None):
                problems.append(f"missing {field_name}")
        return problems

    def render(self, ctx, target_cfg, emit=None):
        artifact = self.artifacts[0]
        if emit and artifact not in emit:
            return []
        spec = ctx.canonical
        slug = slugify(spec.title)
        relpath = self._m.get("naming", "{id}-{slug}.md").format(id=spec.id, slug=slug)

        parts = [f"# {spec.title}\n"]
        for sec in self._m.get("sections", []):
            label = sec["label"]
            if "static" in sec:
                text = _substitute(sec["static"], spec).strip() or _FILL
            else:
                value = getattr(spec, sec["source"], "")
                if sec.get("list"):
                    items = value if isinstance(value, list) else [value] if value else []
                    fmt = ((lambda it: f"- [ ] {it}") if sec.get("checkbox")
                           else (lambda it: f"- {it}"))
                    text = "\n".join(fmt(it) for it in items) or _FILL
                else:
                    text = (value or "").strip() if isinstance(value, str) else "\n".join(value)
                    text = text or _FILL
            parts.append(f"## {label}\n\n{text}\n")

        content = "\n".join(parts)
        return [OutputFile(relpath=relpath, content=content, artifact=artifact, mode="owned")]


def _load_manifest(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_manifests_from(dir_path, *, origin) -> list["TemplateScheme"]:
    """Load every `*.toml` manifest in `dir_path` (which may not exist — silent no-op) into
    `TemplateScheme` instances, stamped with `origin`. Pure: does not touch the registry.

    Skips `kind = "sheet"` manifests (SPEC-0124): those belong to `sheet_schemes.py`'s
    `SheetManifest` loader, which shares this same user-config directory (SPEC-0058) but
    describes spreadsheet columns, not markdown sections — not an `ExportScheme` at all."""
    dir_path = Path(dir_path)
    if not dir_path.exists():
        return []
    out = []
    for p in sorted(dir_path.glob("*.toml")):
        manifest = _load_manifest(p)
        if manifest.get("kind") == "sheet":
            continue
        out.append(TemplateScheme(manifest, origin=origin))
    return out


for _scheme in load_manifests_from(_MANIFEST_DIR, origin="built-in"):
    register(_scheme)
for _scheme in load_manifests_from(_USER_SCHEMES_DIR, origin="user-config"):  # after: wins ties
    register(_scheme)
