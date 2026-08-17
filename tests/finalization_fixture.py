"""Build a disposable, workflow-valid schema v2 finalization fixture for CI."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from bento_converter.html_document import extract_bento_doc, load_html, serialize_bento_doc
from scripts.deck_workflow import (
    command_approve_content,
    command_approve_plan,
    command_approve_section,
    command_begin_authoring,
    command_begin_content_review,
    command_begin_finalization,
    command_begin_section,
    command_complete_section,
    command_configure_sections,
    command_initialize,
    command_mark_converted,
    command_prepare_conversion,
    command_submit_plan,
    load_state,
    validate_output_bundle,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _copy_support_file(root: Path, relative: str) -> None:
    source = SOURCE_ROOT / relative
    target = root / relative
    if source.resolve() == target.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _write_conversion_fixture(root: Path) -> None:
    output = root / "output"
    diagnostics = output / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)

    source_html = load_html(root / "demo.bento.html")
    source_document = extract_bento_doc(source_html)
    (output / "presentation.generated.bento.html").write_text(source_html, encoding="utf-8")
    (output / "presentation.generated.bento.json").write_text(
        serialize_bento_doc(source_document) + "\n", encoding="utf-8",
    )
    (output / "conversion-report.json").write_text(
        json.dumps({"summary": {"criticalElementFail": 0, "unresolvedLocalResourceReferences": 0}}),
        encoding="utf-8",
    )
    (diagnostics / "computed-layout.json").write_text("{}\n", encoding="utf-8")
    (diagnostics / "merged-registry.json").write_text(
        json.dumps({
            "format": "bento/html-registry/v2",
            "unitId": "deck",
            "sources": {},
            "document": {},
            "assets": {},
            "fonts": {},
            "equations": {
                "hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"},
            },
            "figures": {},
            "tables": {},
            "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }),
        encoding="utf-8",
    )
    (diagnostics / "resource-scan.json").write_text(
        json.dumps({"passed": True, "unresolved": []}), encoding="utf-8",
    )
    (diagnostics / "browser-check.json").write_text(
        json.dumps({"serialize_roundtrip": True}), encoding="utf-8",
    )
    (diagnostics / "browser-environment.json").write_text(
        json.dumps({
            "format": "bento/browser-environment/v1",
            "environmentDigest": "sha256:" + "0" * 64,
            "browserEnvironment": {
                "profiles": {"sourceLayout": {}, "bentoCheck": {}},
            },
        }),
        encoding="utf-8",
    )


def prepare_authoring_fixture(
    root: str | Path, *, bento_port: int | None = None, confirm_disposable: bool = False,
) -> dict:
    """Create a disposable bento-authoring fixture through the real workflow gates."""

    if not confirm_disposable:
        raise ValueError("Refusing to overwrite fixture artifacts without disposable confirmation")
    repository = Path(root).resolve()
    for directory in (
        "workflow", "sources/private", "planning", "deck", "output/diagnostics",
    ):
        (repository / directory).mkdir(parents=True, exist_ok=True)
    for relative in (
        "REQUEST.md",
        "demo.bento.html",
        "workflow/deck.schema.json",
        "workflow/deck.v1.schema.json",
        "planning/work-log.md",
    ):
        _copy_support_file(repository, relative)
    shutil.copy2(SOURCE_ROOT / "tests/fixtures/deck_v2.initialized.yaml", repository / "deck.yaml")

    (repository / "sources/private/fixture.md").write_text(
        "# Fixture source\n\nVerified CI fixture content.\n", encoding="utf-8",
    )
    (repository / "sources/source-manifest.yaml").write_text(
        "schemaVersion: 1\n"
        "authorityMode: single\n"
        "items:\n"
        "  - id: fixture-source\n"
        "    path: sources/private/fixture.md\n"
        "    type: document\n"
        "    role: primary\n",
        encoding="utf-8",
    )
    (repository / "planning/explanation-policy.md").write_text(
        "# Policy\n\nExplain only verified fixture content.\n", encoding="utf-8",
    )
    (repository / "planning/story-outline.md").write_text(
        "# Story\n\nFixture source to validated finalization.\n", encoding="utf-8",
    )
    (repository / "planning/slide-plan.md").write_text(
        "# Plan\n\nOne deterministic fixture chapter.\n", encoding="utf-8",
    )

    command_initialize(repository, load_state(repository))
    command_configure_sections(repository, load_state(repository), ("fixture-section",))
    command_submit_plan(repository, load_state(repository))
    command_approve_plan(repository, load_state(repository))
    command_begin_section(repository, load_state(repository), "fixture-section")

    slide_id = "fixture-slide"
    (repository / "deck/deck.preview.html").write_text(
        "<!doctype html><html><body>"
        f'<section class="slide" data-slide-id="{slide_id}" data-section-id="fixture-section">'
        '<h1 data-bento-id="title">Finalization fixture</h1>'
        '<div data-bento-id="equation" data-equation-id="energy" '
        'data-latex="E=mc^2">E = mc2</div>'
        "</section></body></html>",
        encoding="utf-8",
    )
    (repository / "deck/deck.registry.json").write_text(
        json.dumps({
            "format": "bento/html-registry/v2",
            "unitId": "deck",
            "sources": {},
            "document": {},
            "assets": {},
            "fonts": {},
            "equations": {"energy": {"latex": "E=mc^2", "usedOnSlides": [slide_id]}},
            "figures": {},
            "tables": {},
            "charts": {},
            "protected": {"slideIds": [slide_id], "elementIds": ["title"], "requiredText": []},
        }),
        encoding="utf-8",
    )
    command_complete_section(repository, load_state(repository), "fixture-section")
    command_approve_section(repository, load_state(repository), "fixture-section")
    command_prepare_conversion(repository, load_state(repository))

    _write_conversion_fixture(repository)
    command_mark_converted(repository, load_state(repository))
    command_begin_authoring(repository, load_state(repository))

    state = load_state(repository)
    if bento_port is not None:
        state["preview"]["bentoPort"] = bento_port
        from scripts.deck_workflow import atomic_write_state

        atomic_write_state(repository, state)
        state = load_state(repository)
    validate_output_bundle(repository, state, require_final=False)
    return state


def prepare_finalization_fixture(
    root: str | Path, *, bento_port: int | None = None, confirm_disposable: bool = False,
) -> dict:
    """Create a disposable finalization fixture by traversing the real workflow gates."""

    repository = Path(root).resolve()
    prepare_authoring_fixture(
        repository, bento_port=bento_port, confirm_disposable=confirm_disposable,
    )
    command_begin_content_review(repository, load_state(repository))
    command_approve_content(repository, load_state(repository))
    command_begin_finalization(repository, load_state(repository))
    state = load_state(repository)
    validate_output_bundle(repository, state, require_final=True)
    return state


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--bento-port", type=int)
    result.add_argument("--confirm-disposable-fixture", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.confirm_disposable_fixture:
        raise SystemExit("Refusing to overwrite fixture artifacts without --confirm-disposable-fixture")
    state = prepare_finalization_fixture(
        args.root, bento_port=args.bento_port, confirm_disposable=True,
    )
    print(json.dumps({
        "schemaVersion": state["schemaVersion"],
        "stage": state["workflow"]["stage"],
        "outputs": state["outputs"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
