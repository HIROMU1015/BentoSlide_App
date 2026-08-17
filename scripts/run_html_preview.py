"""Serve single-deck or modular HTML authoring sources on loopback."""

from __future__ import annotations

import argparse
import contextlib
import hmac
import html
import json
import mimetypes
import secrets
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from bento_converter.errors import BentoConverterError
from bento_converter.section_approval import HtmlDeckOutline, read_html_deck_outline
from scripts.deck_workflow import (
    WorkflowError,
    command_apply_html_change,
    command_approve_html_change,
    command_approve_html_deck,
    command_check_html_change,
    load_state,
    repository_root,
)


STATUS_FORMAT = "bento/html-preview-status/v1"
VISIBLE_HTML_CHANGE_STATUSES = {"proposed", "approved", "applied"}
HTML_CHANGE_ACTION_PATH = "/api/html-change/action"
MAX_ACTION_BODY_BYTES = 64 * 1024


def _html_change_preview(state: dict[str, Any]) -> dict[str, Any] | None:
    proposal = state.get("authoring", {}).get("htmlChange")
    if not isinstance(proposal, dict) or proposal.get("status") not in VISIBLE_HTML_CHANGE_STATUSES:
        return None
    post_apply = proposal.get("postApplyReview") if isinstance(proposal.get("postApplyReview"), dict) else None
    candidate = str(proposal.get("candidateHtml") or "")
    return {
        "proposalId": proposal.get("proposalId"),
        "proposalDigest": proposal.get("proposalDigest"),
        "status": proposal.get("status"),
        "scope": proposal.get("scope"),
        "summary": proposal.get("summary"),
        "impactSummary": proposal.get("impactSummary"),
        "requestedSlideIds": list(proposal.get("requestedSlideIds") or []),
        "relatedSlideIds": list(proposal.get("relatedSlideIds") or []),
        "changedSlideIds": list(proposal.get("changedSlideIds") or []),
        "affectedSlideIds": list(proposal.get("affectedSlideIds") or []),
        "addedSlideIds": list(proposal.get("addedSlideIds") or []),
        "removedSlideIds": list(proposal.get("removedSlideIds") or []),
        "reordered": bool(proposal.get("reordered")),
        "sectionMembershipChanged": bool(proposal.get("sectionMembershipChanged")),
        "structuralImpact": bool(proposal.get("structuralImpact")),
        "globalStyleChanged": bool(proposal.get("globalStyleChanged")),
        "registryChanged": bool(proposal.get("registryChanged")),
        "baseHtmlRevision": proposal.get("baseHtmlRevision"),
        "baseRegistryRevision": proposal.get("baseRegistryRevision"),
        "candidateHtmlRevision": proposal.get("candidateHtmlRevision"),
        "candidateRegistryRevision": proposal.get("candidateRegistryRevision"),
        "slideTitles": dict(proposal.get("slideTitles") or {}),
        "candidatePath": candidate,
        "candidateUrl": "/" + quote(candidate.replace("\\", "/"), safe="/"),
        "postApplyReviewStatus": post_apply.get("status") if post_apply else None,
    }


def _require_action_contract(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    proposal = state.get("authoring", {}).get("htmlChange")
    if not isinstance(proposal, dict) or proposal.get("status") not in VISIBLE_HTML_CHANGE_STATUSES:
        raise WorkflowError("There is no reviewable HTML change proposal")
    expected = {
        "proposalId": proposal.get("proposalId"),
        "proposalDigest": proposal.get("proposalDigest"),
        "baseHtmlRevision": proposal.get("baseHtmlRevision"),
        "baseRegistryRevision": proposal.get("baseRegistryRevision"),
        "candidateHtmlRevision": proposal.get("candidateHtmlRevision"),
        "candidateRegistryRevision": proposal.get("candidateRegistryRevision"),
    }
    for field, value in expected.items():
        supplied = payload.get(field)
        if not isinstance(supplied, str) or not isinstance(value, str) or not hmac.compare_digest(supplied, value):
            raise WorkflowError(f"HTML change action is stale or mismatched: {field}")
    if payload.get("confirmed") is not True:
        raise WorkflowError("HTML change action requires explicit confirmation")
    return proposal


def _run_html_preview_action(repository: Path, payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action not in {"approve-apply-check", "approve-html-deck"}:
        raise WorkflowError("Unknown HTML preview action")
    state = load_state(repository)
    proposal = _require_action_contract(state, payload)

    if action == "approve-apply-check":
        if state["workflow"]["stage"] != "html_review":
            if state["workflow"]["stage"] == "ready_for_conversion":
                return {"status": "already-complete", "stage": "ready_for_conversion"}
            raise WorkflowError("HTML change actions are available only during HTML review")
        if proposal.get("status") in {"proposed", "approved"}:
            reviewed = payload.get("reviewedSlideIds")
            affected = list(proposal.get("affectedSlideIds") or [])
            if not isinstance(reviewed, list) or reviewed != affected or len(set(reviewed)) != len(reviewed):
                raise WorkflowError("Every affected slide must be explicitly reviewed before applying the proposal")
        if proposal.get("status") == "proposed":
            command_approve_html_change(repository, state)
            state = load_state(repository)
            proposal = _require_action_contract(state, payload)
        if proposal.get("status") == "approved":
            command_apply_html_change(repository, state)
            state = load_state(repository)
            proposal = _require_action_contract(state, payload)
        if proposal.get("status") != "applied":
            raise WorkflowError("HTML change proposal did not reach the applied state")
        command_check_html_change(repository, state, browser_executable=None)
        checked = load_state(repository)
        review = checked["authoring"]["htmlChange"].get("postApplyReview") or {}
        return {
            "status": "checked",
            "stage": checked["workflow"]["stage"],
            "postApplyReviewStatus": review.get("status"),
        }

    if state["workflow"]["stage"] == "ready_for_conversion":
        return {"status": "already-approved", "stage": "ready_for_conversion"}
    if state["workflow"]["stage"] != "html_review":
        raise WorkflowError("Whole-deck HTML approval is available only during HTML review")
    if proposal.get("status") != "applied" or (proposal.get("postApplyReview") or {}).get("status") != "checked":
        raise WorkflowError("The applied proposal needs a successful browser check before whole-deck approval")
    command_approve_html_deck(repository, state)
    approved = load_state(repository)
    return {"status": "approved", "stage": approved["workflow"]["stage"]}


def _preview_snapshot(repository: Path) -> tuple[dict[str, Any], list[str], str | None]:
    state = load_state(repository)
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        relative = state["authoring"]["entryHtml"]
        path = (repository / relative).resolve()
        files = [relative] if path.is_file() else []
        return state, files, relative if files else None
    chapters_root = (repository / "chapters").resolve()
    files = [path.relative_to(repository).as_posix() for path in sorted(chapters_root.glob("*.preview.html")) if path.is_file()]
    current_id = state["workflow"].get("currentChapter")
    current_path = None
    if current_id and current_id in state["chapters"]:
        candidate = state["chapters"][current_id]["html"]
        if candidate in files:
            current_path = candidate
    return state, files, current_path


def _preview_outline(repository: Path, relative: str) -> HtmlDeckOutline:
    source = (repository / relative).resolve()
    try:
        source.relative_to(repository.resolve())
    except ValueError as exc:
        raise WorkflowError(f"HTML preview source escapes the repository: {relative}") from exc
    if not source.is_file():
        raise WorkflowError(f"HTML preview source does not exist: {relative}")
    try:
        return read_html_deck_outline(source)
    except BentoConverterError as exc:
        raise WorkflowError(str(exc)) from exc


def _index_html(repository: Path, *, action_token: str) -> bytes:
    state, files, current_path = _preview_snapshot(repository)
    stage_value = state["workflow"]["stage"]
    stage = html.escape(stage_value)
    single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}
    if single:
        current_section = html.escape(state["workflow"].get("currentSection") or "-")
        change = _html_change_preview(state)
        source_url = "/" + quote((current_path or "").replace("\\", "/"), safe="/")
        canonical_outline = _preview_outline(repository, current_path or "")
        candidate_outline = (
            _preview_outline(repository, change["candidatePath"])
            if change else canonical_outline
        )
        outlines = {"canonical": canonical_outline, "candidate": candidate_outline}

        def version_title(outline: HtmlDeckOutline, slide_id: str, view: str) -> str:
            title = outline.slide_titles.get(slide_id, slide_id)
            if title == slide_id and view == "candidate" and change:
                return str(change["slideTitles"].get(slide_id, slide_id))
            return title

        def version_sections(view: str, outline: HtmlDeckOutline) -> str:
            ordered_sections: list[str] = []
            for slide_id in outline.ordered_slide_ids:
                section_id = outline.slide_section_ids[slide_id]
                if section_id not in ordered_sections:
                    ordered_sections.append(section_id)
            items = []
            for section_id in ordered_sections:
                section = state["sections"].get(section_id, {})
                label = str(section.get("title") or section_id)
                marker = (
                    " <strong>current</strong>"
                    if section_id == state["workflow"].get("currentSection") else ""
                )
                items.append(
                    f'<li><a class="section-nav" data-section-nav="{html.escape(section_id, quote=True)}" '
                    f'href="#section={quote(section_id, safe="")}">{html.escape(label)}</a>{marker}</li>'
                )
            hidden = "" if view == "canonical" else " hidden"
            return (
                f'<ul class="version-nav" data-nav-kind="sections" data-nav-view="{view}"{hidden}>'
                f'{"".join(items) or "<li>No sections</li>"}</ul>'
            )

        def version_slides(view: str, outline: HtmlDeckOutline) -> str:
            total = len(outline.ordered_slide_ids)
            items = []
            for position, slide_id in enumerate(outline.ordered_slide_ids, start=1):
                title = version_title(outline, slide_id, view)
                number = f"{position:02d} / {total:02d}"
                items.append(
                    f'<li><a class="slide-nav" data-slide-nav="{html.escape(slide_id, quote=True)}" '
                    f'data-slide-number="{number}" data-slide-title="{html.escape(title, quote=True)}" '
                    f'href="#slide={quote(slide_id, safe="")}">'
                    f'{position:02d}. {html.escape(title)}</a></li>'
                )
            hidden = "" if view == "canonical" else " hidden"
            return (
                f'<ul class="version-nav" data-nav-kind="slides" data-nav-view="{view}"{hidden}>'
                f'{"".join(items) or "<li>No registered slides</li>"}</ul>'
            )

        section_navigation = "".join(
            version_sections(view, outline) for view, outline in outlines.items()
        )
        slide_navigation = "".join(
            version_slides(view, outline) for view, outline in outlines.items()
        )
        change_panel = ""
        view_indicator = ""
        if change:
            review_active = change["status"] in {"proposed", "approved"}
            requested = set(change["requestedSlideIds"])
            related = set(change["relatedSlideIds"])
            changed = set(change["changedSlideIds"])
            added = set(change["addedSlideIds"])
            removed = set(change["removedSlideIds"])

            def impact(slide_id: str) -> tuple[str, str]:
                if slide_id in added:
                    return "追加", "added"
                if slide_id in removed:
                    return "削除", "removed"
                if slide_id in changed:
                    return "変更あり", "changed"
                if slide_id in related:
                    return "関連確認", "related"
                if slide_id in requested:
                    return "依頼対象", "requested"
                return "再確認", "review"

            affected_set = set(change["affectedSlideIds"])

            def version_review_slides(view: str, outline: HtmlDeckOutline) -> str:
                other_view = "candidate" if view == "canonical" else "canonical"
                other_outline = outlines[other_view]
                present = [slide_id for slide_id in outline.ordered_slide_ids if slide_id in affected_set]
                present_set = set(present)
                missing = [slide_id for slide_id in change["affectedSlideIds"] if slide_id not in present_set]
                ordered = present + missing
                positions = {
                    slide_id: position
                    for position, slide_id in enumerate(outline.ordered_slide_ids, start=1)
                }
                total = len(outline.ordered_slide_ids)
                items = []
                for slide_id in ordered:
                    position = positions.get(slide_id)
                    preferred_view = "" if position is not None else other_view
                    if position is not None:
                        number_label = f"{position:02d} / {total:02d}"
                        title = version_title(outline, slide_id, view)
                    else:
                        number_label = "変更案のみ" if view == "canonical" else "現在案のみ"
                        title = version_title(other_outline, slide_id, other_view)
                    impact_label, impact_class = impact(slide_id)
                    items.append(
                        f'<li><a class="review-slide" data-review-slide="{html.escape(slide_id, quote=True)}" '
                        f'data-slide-title="{html.escape(title, quote=True)}" '
                        f'data-slide-number="{html.escape(number_label, quote=True)}" '
                        f'data-preferred-view="{preferred_view}" href="#slide={quote(slide_id, safe="")}">'
                        f'<span class="review-slide-number">{html.escape(number_label)}</span>'
                        f'<span class="review-slide-title">{html.escape(title)}</span>'
                        f'<span class="impact-badge {impact_class}">{impact_label}</span>'
                        f'<span class="review-state-badge" data-review-state-badge="{html.escape(slide_id, quote=True)}">未確認</span>'
                        f'</a></li>'
                    )
                hidden = "" if view == "canonical" else " hidden"
                label = "現在案の順序" if view == "canonical" else "変更案の順序"
                return (
                    f'<div class="review-version" data-review-view="{view}"{hidden}>'
                    f'<p class="review-order-label">{label}</p>'
                    f'<ol class="affected-slides">{"".join(items)}</ol></div>'
                )

            affected = "".join(version_review_slides(view, outline) for view, outline in outlines.items())
            if review_active:
                change_panel = f"""
<section class="change-review" id="html-change-review">
<h2>変更案の確認</h2>
<p><code>{html.escape(str(change['status']))}</code> / <code>{html.escape(str(change['scope']))}</code>{f" / browser review: <code>{html.escape(str(change['postApplyReviewStatus']))}</code>" if change['postApplyReviewStatus'] else ''}</p>
<div class="sidebar-view-status" id="sidebar-view-status" data-view="canonical">
<span>表示中</span><strong id="sidebar-view-label">現在案</strong><small id="sidebar-view-note">承認前の現在版</small>
</div>
<div class="view-toggle" role="group" aria-label="確認する案">
<button id="show-canonical" class="view-button active" type="button" data-view="canonical" data-url="{html.escape(source_url, quote=True)}" aria-pressed="true">現在案</button>
<button id="show-candidate" class="view-button" type="button" data-view="candidate" data-url="{html.escape(change['candidateUrl'], quote=True)}" aria-pressed="false">変更案</button>
</div>
<p class="change-copy"><span>変更すること</span>{html.escape(str(change['summary']))}</p>
<p class="change-copy"><span>他への影響</span>{html.escape(str(change['impactSummary']))}</p>
<div class="review-progress"><span>確認の進み具合</span><strong id="review-progress-count">0 / {len(change['affectedSlideIds'])}</strong></div>
<details open><summary>確認が必要なスライド ({len(change['affectedSlideIds'])})</summary>{affected}</details>
<div class="slide-review-actions" id="slide-review-actions">
<p><span>選択中</span><strong id="selected-review-title">スライドを選択してください</strong></p>
<div><button id="mark-needs-work" class="needs-work-button" type="button">要修正にする</button><button id="mark-reviewed" class="reviewed-button" type="button">確認済みにする</button></div>
</div>
<div class="proposal-actions"><p id="proposal-action-message">すべての対象スライドを確認すると反映できます。</p><button id="apply-proposal" class="primary-action" type="button" disabled>この変更案全体を反映</button></div>
</section>"""
                view_indicator = """
<div class="view-indicator" id="view-indicator" data-view="canonical" aria-live="polite">
<span>表示中：</span><strong id="view-indicator-label">現在案</strong><small id="view-slide-label">スライドを確認中</small>
</div>"""
            else:
                checked = change["postApplyReviewStatus"] == "checked"
                if stage_value == "html_review" and checked:
                    action_html = '<button id="approve-html-deck" class="primary-action" type="button">このHTML全体でBentoSlideへ進む</button>'
                    result_copy = "自動検証に成功しました。更新後のHTML全体を確定できます。"
                elif stage_value == "html_review":
                    action_html = '<button id="retry-browser-check" class="primary-action" type="button">自動検証を再実行</button>'
                    result_copy = "変更案は現在案へ反映済みです。自動検証を完了してください。"
                else:
                    action_html = ""
                    result_copy = "HTML全体を確定しました。更新後の現在案を表示しています。"
                change_panel = f"""
<section class="change-review applied-review" id="html-change-review">
<h2>✓ 変更案を反映しました</h2>
<p class="applied-copy">{html.escape(result_copy)}</p>
<p class="change-copy"><span>反映した内容</span>{html.escape(str(change['summary']))}</p>
<p class="review-result">自動検証：<strong>{'成功' if checked else '未完了'}</strong></p>
<div class="proposal-actions"><p id="proposal-action-message">変更案の比較表示は終了しました。</p>{action_html}</div>
</section>"""
                view_indicator = """
<div class="view-indicator" id="view-indicator" data-view="canonical" aria-live="polite">
<span>表示中：</span><strong id="view-indicator-label">現在案</strong><small id="view-slide-label">更新後の現在案</small>
</div>"""
        styles = """html,body{height:100%;margin:0}[hidden]{display:none!important}body{font:14px/1.5 system-ui,sans-serif;color:#172033;display:grid;grid-template-columns:300px 1fr}aside{padding:18px;overflow:auto;border-right:1px solid #ccd5e1;background:#f8fafc}main.preview-pane{position:relative;min-width:0;height:100%;overflow:hidden;background:#d9dfdc}iframe{box-sizing:border-box;width:100%;height:100%;border:0;background:white;transition:outline-color .15s ease}.preview-pane[data-view=candidate] iframe{outline:4px solid #f59e0b;outline-offset:-4px}code{background:#e7edf5;padding:2px 5px;border-radius:4px}li{margin:5px 0}button{padding:7px 10px;cursor:pointer}strong{color:#1d4ed8}.change-review{margin:16px -6px;padding:12px;border:1px solid #93c5fd;border-radius:8px;background:#eff6ff}.change-review h2{margin:0 0 8px}.sidebar-view-status{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:2px 8px;margin:10px 0;padding:10px;border-radius:7px;background:#dbeafe;border-left:5px solid #2563eb}.sidebar-view-status>span{font-size:11px;font-weight:700;color:#475569}.sidebar-view-status>strong{font-size:18px;color:#1e40af}.sidebar-view-status>small{grid-column:1/-1;color:#475569}.sidebar-view-status[data-view=candidate]{background:#ffedd5;border-left-color:#ea580c}.sidebar-view-status[data-view=candidate]>strong{color:#9a3412}.view-toggle{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:12px}.view-button{border:1px solid #94a3b8;border-radius:6px;background:#fff;color:#334155}.view-button.active{border-color:#2563eb;background:#2563eb;color:#fff;font-weight:700}.view-button[data-view=candidate].active{border-color:#c2410c;background:#c2410c}.change-copy{margin:10px 0;color:#334155}.change-copy>span{display:block;margin-bottom:2px;font-size:11px;font-weight:800;color:#075985}.review-order-label{margin:8px 0 3px;font-size:11px;font-weight:800;color:#475569}.affected-slides{list-style:none;padding:0;margin:3px 0 8px}.affected-slides li{margin:6px 0}.review-slide{display:grid;grid-template-columns:52px 1fr auto;align-items:center;gap:7px;padding:7px;border:1px solid transparent;border-radius:7px;color:#172033;text-decoration:none;background:rgba(255,255,255,.68)}.review-slide:hover,.review-slide:focus{border-color:#60a5fa;background:#fff}.review-slide.is-active{border-color:#2563eb;background:#dbeafe;box-shadow:0 0 0 2px rgba(37,99,235,.12)}.review-slide-number{font-size:11px;font-weight:800;color:#475569}.review-slide-title{line-height:1.35}.impact-badge{white-space:nowrap;padding:2px 5px;border-radius:999px;font-size:10px;font-weight:800;background:#e2e8f0}.impact-badge.changed,.impact-badge.added{background:#ffedd5;color:#9a3412}.impact-badge.removed{background:#fee2e2;color:#991b1b}.impact-badge.related{background:#ede9fe;color:#5b21b6}.slide-nav.is-active{font-weight:800;color:#1d4ed8}.view-indicator{position:absolute;z-index:5;top:12px;right:20px;display:flex;align-items:baseline;gap:4px;max-width:min(620px,70%);padding:8px 12px;border-radius:999px;background:rgba(30,64,175,.94);color:#fff;box-shadow:0 3px 14px rgba(15,23,42,.25);pointer-events:none}.view-indicator strong{color:#fff}.view-indicator small{margin-left:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#dbeafe}.view-indicator[data-view=candidate]{background:rgba(154,52,18,.95)}.view-indicator[data-view=candidate] small{color:#ffedd5}@media(max-width:900px){body{grid-template-columns:260px 1fr}.review-slide{grid-template-columns:46px 1fr}.impact-badge{grid-column:2}.view-indicator{right:12px;max-width:75%}}"""
        styles += """body{--sidebar-width:300px;grid-template-columns:var(--sidebar-width) 8px minmax(0,1fr)}aside{grid-column:1;min-width:0}.sidebar-resizer{grid-column:2;position:relative;z-index:20;width:8px;height:100%;padding:0;border:0;border-left:1px solid #cbd5e1;border-right:1px solid #cbd5e1;background:#e2e8f0;cursor:col-resize;touch-action:none}.sidebar-resizer::after{content:'';position:absolute;left:2px;top:calc(50% - 24px);width:2px;height:48px;border-radius:999px;background:#94a3b8}.sidebar-resizer:hover,.sidebar-resizer:focus,.sidebar-resizer.is-dragging{outline:0;background:#bfdbfe;border-color:#60a5fa}.sidebar-resizer:hover::after,.sidebar-resizer:focus::after,.sidebar-resizer.is-dragging::after{background:#2563eb}body.sidebar-resizing{cursor:col-resize;user-select:none}body.sidebar-resizing iframe{pointer-events:none}main.preview-pane{grid-column:3}.scale-indicator{position:absolute;z-index:5;right:20px;bottom:14px;display:flex;align-items:center;gap:5px;padding:5px 9px;border-radius:999px;background:rgba(15,23,42,.76);color:#e2e8f0;font-size:11px;box-shadow:0 2px 8px rgba(15,23,42,.2);pointer-events:none}.scale-indicator strong{color:#fff}@media(max-width:900px){body{grid-template-columns:var(--sidebar-width) 8px minmax(0,1fr)}}"""
        styles += """.review-state-badge{grid-column:2/4;justify-self:start;white-space:nowrap;padding:2px 7px;border-radius:999px;font-size:10px;font-weight:800;color:#475569;background:#e2e8f0}.review-state-badge.reviewed{color:#166534;background:#dcfce7}.review-state-badge.needs-work{color:#991b1b;background:#fee2e2}.review-slide[data-review-state=reviewed]{border-color:#86efac}.review-slide[data-review-state=needs-work]{border-color:#fca5a5}.review-progress{display:flex;align-items:center;justify-content:space-between;margin:12px 0 7px;padding:8px 10px;border-radius:7px;background:#fff}.review-progress strong{color:#166534}.slide-review-actions{margin:12px 0;padding:10px;border:1px solid #cbd5e1;border-radius:8px;background:#fff}.slide-review-actions p{margin:0 0 8px}.slide-review-actions p span{display:block;font-size:11px;font-weight:800;color:#475569}.slide-review-actions p strong{display:block;color:#172033}.slide-review-actions>div{display:grid;grid-template-columns:1fr 1fr;gap:6px}.needs-work-button{border:1px solid #dc2626;border-radius:6px;background:#fff;color:#991b1b}.reviewed-button{border:1px solid #16a34a;border-radius:6px;background:#f0fdf4;color:#166534}.proposal-actions{position:sticky;bottom:-12px;margin:12px -12px -12px;padding:12px;border-top:1px solid #bfdbfe;border-radius:0 0 8px 8px;background:#eff6ff;box-shadow:0 -5px 12px rgba(30,64,175,.08)}.proposal-actions p{margin:0 0 8px;font-size:12px;color:#475569}.primary-action{width:100%;border:1px solid #1d4ed8;border-radius:7px;background:#1d4ed8;color:#fff;font-weight:800}.primary-action:disabled{cursor:not-allowed;border-color:#94a3b8;background:#cbd5e1;color:#64748b}.change-review.is-busy{opacity:.72;pointer-events:none}.change-review.has-error{border-color:#f87171;background:#fef2f2}.applied-review{border-color:#86efac;background:#f0fdf4}.applied-review .proposal-actions{border-top-color:#bbf7d0;background:#f0fdf4}.applied-copy{color:#166534;font-weight:700}.review-result{padding:8px 10px;border-radius:7px;background:#fff}.review-result strong{color:#166534}@media(max-width:900px){.review-state-badge{grid-column:2}.slide-review-actions>div{grid-template-columns:1fr}}"""
        action_change = None
        if change:
            action_change = {
                field: change[field]
                for field in (
                    "proposalId", "proposalDigest", "baseHtmlRevision", "baseRegistryRevision",
                    "candidateHtmlRevision", "candidateRegistryRevision", "affectedSlideIds",
                    "structuralImpact", "globalStyleChanged", "reordered", "sectionMembershipChanged",
                )
            }
        action_config_json = json.dumps(
            {"endpoint": HTML_CHANGE_ACTION_PATH, "token": action_token, "change": action_change},
            ensure_ascii=False,
        ).replace("<", "\\u003c")
        script = """const actionConfig=__BENTO_ACTION_CONFIG__;
const frame=document.getElementById('deck');
const pane=document.getElementById('preview-pane');
const resizer=document.getElementById('sidebar-resizer');
const scaleLabel=document.getElementById('preview-scale-label');
const viewConfig={canonical:{label:'現在案',note:'承認前の現在版'},candidate:{label:'変更案',note:'確認中の修正候補'}};
let activeView='canonical';
let activeSlideId=null;
let scaleFrameRequest=0;
let pinnedScaleSlideId=null;
let scalePinTimer=0;
const sidebarStorageKey='bento.htmlPreview.sidebarWidth.v1';
function releaseScalePinSoon(){
  if(scalePinTimer)clearTimeout(scalePinTimer);
  scalePinTimer=setTimeout(()=>{if(resizer.classList.contains('is-dragging')){releaseScalePinSoon();return;}pinnedScaleSlideId=null;scalePinTimer=0;},180);
}
function pinScaleSlide(){
  if(!pinnedScaleSlideId)pinnedScaleSlideId=activeSlideId||activeFrameSlideId();
  releaseScalePinSoon();
}
function sidebarBounds(){const minimum=Math.min(220,Math.max(160,window.innerWidth-360));return{minimum,maximum:Math.max(minimum,Math.min(560,window.innerWidth-360))};}
function setSidebarWidth(value,{persist=true}={}){
  const bounds=sidebarBounds();const width=Math.round(Math.min(bounds.maximum,Math.max(bounds.minimum,Number(value)||300)));
  document.body.style.setProperty('--sidebar-width',width+'px');
  resizer.setAttribute('aria-valuemin',String(bounds.minimum));resizer.setAttribute('aria-valuemax',String(bounds.maximum));resizer.setAttribute('aria-valuenow',String(width));resizer.title='サイドバー幅：'+width+'px（ドラッグ、矢印キー、ダブルクリックで調整）';
  if(persist){try{localStorage.setItem(sidebarStorageKey,String(width));}catch{}}
  pinScaleSlide();scheduleFrameScale();return width;
}
function setupSidebarResize(){
  let startX=0;let startWidth=300;let dragging=false;
  resizer.addEventListener('pointerdown',event=>{if(event.button!==0)return;dragging=true;startX=event.clientX;startWidth=document.querySelector('aside').getBoundingClientRect().width;resizer.setPointerCapture(event.pointerId);resizer.classList.add('is-dragging');document.body.classList.add('sidebar-resizing');pinScaleSlide();event.preventDefault();});
  resizer.addEventListener('pointermove',event=>{if(dragging)setSidebarWidth(startWidth+event.clientX-startX);});
  const finish=event=>{if(!dragging)return;dragging=false;resizer.classList.remove('is-dragging');document.body.classList.remove('sidebar-resizing');if(resizer.hasPointerCapture(event.pointerId))resizer.releasePointerCapture(event.pointerId);};
  resizer.addEventListener('pointerup',finish);resizer.addEventListener('pointercancel',finish);
  resizer.addEventListener('keydown',event=>{const current=document.querySelector('aside').getBoundingClientRect().width;let next=null;if(event.key==='ArrowLeft')next=current-(event.shiftKey?48:16);if(event.key==='ArrowRight')next=current+(event.shiftKey?48:16);if(event.key==='Home')next=sidebarBounds().minimum;if(event.key==='End')next=sidebarBounds().maximum;if(next!==null){setSidebarWidth(next);event.preventDefault();}});
  resizer.addEventListener('dblclick',()=>setSidebarWidth(300));
  let saved=300;try{saved=Number(localStorage.getItem(sidebarStorageKey))||300;}catch{}
  setSidebarWidth(saved,{persist:false});
}
function orderedSlideIds(view){return [...document.querySelectorAll('[data-nav-kind="slides"][data-nav-view="'+view+'"] [data-slide-nav]')].map(link=>link.dataset.slideNav);}
function activeFrameSlideId(){
  const doc=frame.contentDocument;if(!doc)return null;
  const slides=[...doc.querySelectorAll('[data-slide-id]')];
  const viewport=frame.contentWindow?frame.contentWindow.innerHeight:frame.clientHeight;
  let active=null;let distance=Infinity;
  for(const node of slides){const rect=node.getBoundingClientRect();if(rect.bottom<=0||rect.top>=viewport)continue;const fromTop=Math.abs(rect.top);if(fromTop<distance){active=node;distance=fromTop;}}
  return active&&active.getAttribute('data-slide-id');
}
function applyFrameScale({preserveSlide=true}={}){
  scaleFrameRequest=0;const doc=frame.contentDocument;if(!doc)return;
  const slide=doc.querySelector('[data-slide-id]');if(!slide)return;
  const currentId=preserveSlide?(pinnedScaleSlideId||activeFrameSlideId()):null;
  const naturalWidth=slide.offsetWidth||1280;const naturalHeight=slide.offsetHeight||720;
  const horizontalGutter=Math.min(64,Math.max(24,frame.clientWidth*.05));
  const verticalGutter=Math.min(56,Math.max(24,frame.clientHeight*.06));
  const widthScale=(frame.clientWidth-horizontalGutter)/naturalWidth;
  const heightScale=(frame.clientHeight-verticalGutter)/naturalHeight;
  const scale=Math.max(.25,Math.min(1.15,widthScale,heightScale));
  const encoded=scale.toFixed(4);doc.documentElement.style.zoom=encoded;doc.documentElement.dataset.bentoPreviewScale=encoded;
  if(scaleLabel)scaleLabel.textContent=Math.round(scale*100)+'%（自動）';
  if(currentId){requestAnimationFrame(()=>{const node=doc.querySelector('[data-slide-id="'+CSS.escape(currentId)+'"]');if(node)node.scrollIntoView({block:'start'});requestAnimationFrame(syncActiveSlide);});}
}
function scheduleFrameScale(options){if(scaleFrameRequest)cancelAnimationFrame(scaleFrameRequest);scaleFrameRequest=requestAnimationFrame(()=>applyFrameScale(options));}
function nearestAvailableSlide(fromView,toView,currentId){
  const target=orderedSlideIds(toView);if(!target.length)return null;
  if(currentId&&target.includes(currentId))return currentId;
  const source=orderedSlideIds(fromView);const index=source.indexOf(currentId);
  if(index>=0){for(let distance=1;distance<source.length;distance++){for(const candidate of [source[index-distance],source[index+distance]]){if(candidate&&target.includes(candidate))return candidate;}}}
  return target[0];
}
function updateViewUi(){
  const config=viewConfig[activeView]||viewConfig.canonical;
  for(const node of [pane,document.getElementById('view-indicator'),document.getElementById('sidebar-view-status')]){if(node)node.dataset.view=activeView;}
  for(const id of ['view-indicator-label','sidebar-view-label']){const node=document.getElementById(id);if(node)node.textContent=config.label;}
  frame.title='Deck preview（'+config.label+'）';
  const note=document.getElementById('sidebar-view-note');if(note)note.textContent=config.note;
  for(const node of document.querySelectorAll('[data-nav-view],[data-review-view]')){const view=node.dataset.navView||node.dataset.reviewView;node.hidden=view!==activeView;}
  for(const button of document.querySelectorAll('.view-button')){const selected=button.dataset.view===activeView;button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));}
}
function syncActiveSlide(){
  const slideId=activeFrameSlideId();
  if(slideId)activeSlideId=slideId;
  for(const link of document.querySelectorAll('[data-review-slide],[data-slide-nav]')){const selected=(link.dataset.reviewSlide||link.dataset.slideNav)===slideId;link.classList.toggle('is-active',selected);if(selected)link.setAttribute('aria-current','true');else link.removeAttribute('aria-current');}
  const escaped=slideId&&CSS.escape(slideId);
  const reviewLink=escaped&&document.querySelector('[data-review-view="'+activeView+'"] [data-review-slide="'+escaped+'"]');
  const navLink=escaped&&document.querySelector('[data-nav-kind="slides"][data-nav-view="'+activeView+'"] [data-slide-nav="'+escaped+'"]');
  const source=reviewLink||navLink;
  const label=document.getElementById('view-slide-label');
  if(label)label.textContent=source?(source.dataset.slideNumber+' '+source.dataset.slideTitle):(slideId||'スライドを確認中');
  updateReviewUi();
}
function navigate(){
  const match=location.hash.match(/^#(section|slide)=(.+)$/);if(!match)return;
  const attr=match[1]==='section'?'data-section-id':'data-slide-id';
  const apply=()=>{const doc=frame.contentDocument;const node=doc&&doc.querySelector('['+attr+'="'+CSS.escape(decodeURIComponent(match[2]))+'"]');if(node){node.scrollIntoView({block:'start'});requestAnimationFrame(syncActiveSlide);}};
  if(frame.contentDocument)apply();else frame.addEventListener('load',apply,{once:true});
}
function bindFrame(){
  const win=frame.contentWindow;if(win){win.addEventListener('scroll',syncActiveSlide,{passive:true});}
  pinnedScaleSlideId=null;if(scalePinTimer){clearTimeout(scalePinTimer);scalePinTimer=0;}
  applyFrameScale({preserveSlide:false});navigate();syncActiveSlide();
}
function showView(button){
  const next=button.dataset.view;if(!viewConfig[next]||next===activeView)return;
  const previous=activeView;const target=nearestAvailableSlide(previous,next,activeFrameSlideId());
  activeView=next;activeSlideId=target;frame.dataset.view=next;updateViewUi();
  if(target)history.replaceState(null,'','#slide='+encodeURIComponent(target));
  const nextUrl=button.dataset.url;if(frame.getAttribute('src')!==nextUrl)frame.setAttribute('src',nextUrl);else{navigate();syncActiveSlide();}
}
let reviewStates={};
function reviewStorageKey(){return actionConfig.change?'bento.htmlPreview.review.'+actionConfig.change.proposalDigest:null;}
function loadReviewStates(){
  const key=reviewStorageKey();if(!key)return;
  try{const value=JSON.parse(localStorage.getItem(key)||'{}');if(value&&typeof value==='object')reviewStates=value;}catch{reviewStates={};}
  const allowed=new Set(actionConfig.change.affectedSlideIds);for(const id of Object.keys(reviewStates)){if(!allowed.has(id)||!['reviewed','needs-work'].includes(reviewStates[id]))delete reviewStates[id];}
}
function saveReviewStates(){const key=reviewStorageKey();if(!key)return;try{localStorage.setItem(key,JSON.stringify(reviewStates));}catch{}}
function selectedReviewId(){const affected=actionConfig.change&&actionConfig.change.affectedSlideIds||[];return activeSlideId&&affected.includes(activeSlideId)?activeSlideId:null;}
function updateReviewUi(){
  if(!actionConfig.change)return;
  const affected=actionConfig.change.affectedSlideIds;const reviewed=affected.filter(id=>reviewStates[id]==='reviewed');const needs=affected.filter(id=>reviewStates[id]==='needs-work');
  for(const badge of document.querySelectorAll('[data-review-state-badge]')){const id=badge.dataset.reviewStateBadge;const value=reviewStates[id]||'pending';badge.className='review-state-badge'+(value==='pending'?'':' '+value);badge.textContent=value==='reviewed'?'確認済み':value==='needs-work'?'要修正':'未確認';const link=badge.closest('.review-slide');if(link)link.dataset.reviewState=value;}
  const count=document.getElementById('review-progress-count');if(count)count.textContent=reviewed.length+' / '+affected.length;
  const selected=selectedReviewId();const title=document.getElementById('selected-review-title');if(title){const link=selected&&document.querySelector('[data-review-slide="'+CSS.escape(selected)+'"]');title.textContent=link?link.dataset.slideTitle:'確認対象のスライドを選択してください';}
  for(const id of ['mark-reviewed','mark-needs-work']){const button=document.getElementById(id);if(button)button.disabled=!selected;}
  const apply=document.getElementById('apply-proposal');if(apply)apply.disabled=reviewed.length!==affected.length||needs.length>0;
  const message=document.getElementById('proposal-action-message');if(message){message.textContent=needs.length?needs.length+'件が要修正です。チャットで修正内容を伝えてください。':reviewed.length===affected.length?'すべて確認済みです。変更案全体を一括で反映できます。':'未確認 '+(affected.length-reviewed.length)+'件。各スライドを確認してください。';}
}
function setSelectedReviewState(value){const id=selectedReviewId();if(!id)return;reviewStates[id]=value;saveReviewStates();updateReviewUi();}
function actionContract(action){const change=actionConfig.change||{};return{action,confirmed:true,proposalId:change.proposalId,proposalDigest:change.proposalDigest,baseHtmlRevision:change.baseHtmlRevision,baseRegistryRevision:change.baseRegistryRevision,candidateHtmlRevision:change.candidateHtmlRevision,candidateRegistryRevision:change.candidateRegistryRevision,reviewedSlideIds:(change.affectedSlideIds||[]).filter(id=>reviewStates[id]==='reviewed')};}
async function postPreviewAction(action){
  const panel=document.getElementById('html-change-review');const message=document.getElementById('proposal-action-message');
  const affected=actionConfig.change&&actionConfig.change.affectedSlideIds||[];
  const prompt=action==='approve-html-deck'?'更新後のHTML全体を確定して、BentoSlide作成へ進みますか？':affected.length+'件を確認済みとして、変更案全体を現在案へ反映しますか？';
  if(!window.confirm(prompt))return;
  if(panel){panel.classList.add('is-busy');panel.classList.remove('has-error');}if(message)message.textContent='処理中です。この画面を閉じずにお待ちください。';
  try{
    const response=await fetch(actionConfig.endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Bento-Preview-Token':actionConfig.token},body:JSON.stringify(actionContract(action))});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'処理に失敗しました');
    if(message)message.textContent='完了しました。更新後の画面を読み込みます。';window.location.replace('/');
  }catch(error){if(panel){panel.classList.remove('is-busy');panel.classList.add('has-error');}if(message)message.textContent='処理できませんでした：'+error.message;}
}
function setupReviewControls(){loadReviewStates();const reviewed=document.getElementById('mark-reviewed');if(reviewed)reviewed.onclick=()=>setSelectedReviewState('reviewed');const needs=document.getElementById('mark-needs-work');if(needs)needs.onclick=()=>setSelectedReviewState('needs-work');const apply=document.getElementById('apply-proposal');if(apply)apply.onclick=()=>postPreviewAction('approve-apply-check');updateReviewUi();}
function setupWorkflowActions(){const retry=document.getElementById('retry-browser-check');if(retry)retry.onclick=()=>postPreviewAction('approve-apply-check');const approve=document.getElementById('approve-html-deck');if(approve)approve.onclick=()=>postPreviewAction('approve-html-deck');}
window.addEventListener('hashchange',navigate);window.addEventListener('resize',()=>{setSidebarWidth(document.querySelector('aside').getBoundingClientRect().width,{persist:false});scheduleFrameScale();});frame.addEventListener('load',bindFrame);
document.getElementById('reload').onclick=()=>{frame.contentWindow.location.reload();};
for(const button of document.querySelectorAll('.view-button'))button.onclick=()=>showView(button);
for(const link of document.querySelectorAll('.review-slide'))link.addEventListener('click',()=>{const preferred=link.dataset.preferredView;if(preferred&&preferred!==activeView){const button=document.querySelector('.view-button[data-view="'+preferred+'"]');if(button)showView(button);}});
setupSidebarResize();setupReviewControls();setupWorkflowActions();new ResizeObserver(()=>scheduleFrameScale()).observe(frame);updateViewUi();navigate();"""
        script = script.replace("__BENTO_ACTION_CONFIG__", action_config_json)
        payload = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>BentoSlide deck preview</title>
<style>{styles}</style>
</head><body><aside><h1>BentoSlide</h1><p>stage: <code>{stage}</code><br>current section: <code>{current_section}</code></p>
<button id="reload" type="button">Reload</button>{change_panel}<h2>Sections</h2>{section_navigation}
<h2>Slides</h2>{slide_navigation}</aside><div id="sidebar-resizer" class="sidebar-resizer" role="separator" aria-label="サイドバーの幅を調整" aria-orientation="vertical" aria-valuemin="160" aria-valuemax="560" aria-valuenow="300" tabindex="0"></div>
<main class="preview-pane" id="preview-pane" data-view="canonical">{view_indicator}<div class="scale-indicator">表示倍率 <strong id="preview-scale-label">自動</strong></div><iframe id="deck" data-view="canonical" title="Deck preview（現在案）" sandbox="allow-same-origin" src="{source_url}"></iframe></main>
<script>{script}</script>
</body></html>"""
        return payload.encode("utf-8")
    current_id = html.escape(state["workflow"].get("currentChapter") or "-")
    items = []
    for relative in files:
        label = html.escape(Path(relative).name)
        href = "/" + quote(relative.replace("\\", "/"), safe="/")
        marker = " <strong>current</strong>" if relative == current_path else ""
        items.append(f'<li><a href="{href}">{label}</a>{marker}</li>')
    if not items:
        items.append("<li>No chapter preview HTML exists yet.</li>")
    payload = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>BentoSlide chapter preview</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:860px;margin:48px auto;padding:0 24px;color:#172033}}code{{background:#eef2f7;padding:2px 6px;border-radius:5px}}li{{margin:10px 0}}strong{{color:#1d4ed8}}</style>
</head><body><h1>BentoSlide chapter preview</h1><p>stage: <code>{stage}</code><br>current chapter: <code>{current_id}</code></p><ul>{''.join(items)}</ul><p>After an agent updates a chapter, reload its tab.</p></body></html>"""
    return payload.encode("utf-8")


def _safe_preview_path(repository: Path, request_path: str) -> Path | None:
    decoded = unquote(request_path)
    if "\x00" in decoded or "\\" in decoded:
        return None
    pure = PurePosixPath(decoded.lstrip("/"))
    if not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    state = load_state(repository)
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        entry = (repository / state["authoring"]["entryHtml"]).resolve()
        allowed_root = entry.parent
    else:
        if pure.parts[0] != "chapters":
            return None
        allowed_root = (repository / "chapters").resolve()
    candidate = (repository.joinpath(*pure.parts)).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


class HtmlPreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], repository: Path):
        self.repository = repository.resolve()
        self.action_token = secrets.token_urlsafe(32)
        self.action_lock = threading.Lock()
        super().__init__(address, HtmlPreviewHandler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class HtmlPreviewHandler(BaseHTTPRequestHandler):
    server: HtmlPreviewServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stdout.write(f"{self.address_string()} - {format % args}\n")
        sys.stdout.flush()

    def _send(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
            self.wfile.flush()

    def _discard_bounded_request_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return
        if length < 1 or length > MAX_ACTION_BODY_BYTES:
            return
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.25)
            self.rfile.read(length)
        except (OSError, socket.timeout):
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        self._send(status, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"), "application/json; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route != HTML_CHANGE_ACTION_PATH:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        expected_authority = f"127.0.0.1:{self.server.server_port}"
        expected_origin = f"http://{expected_authority}"
        host = self.headers.get("Host") or ""
        origin = self.headers.get("Origin") or ""
        token = self.headers.get("X-Bento-Preview-Token") or ""
        if not hmac.compare_digest(host, expected_authority) or not hmac.compare_digest(origin, expected_origin):
            self._discard_bounded_request_body()
            self._json(HTTPStatus.FORBIDDEN, {"error": "HTML preview action origin is not allowed"})
            return
        if not hmac.compare_digest(token, self.server.action_token):
            self._discard_bounded_request_body()
            self._json(HTTPStatus.FORBIDDEN, {"error": "HTML preview action token is invalid"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Expected application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "A valid Content-Length is required"})
            return
        if length < 2 or length > MAX_ACTION_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "HTML preview action body is invalid"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON request"})
            return
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "HTML preview action must be a JSON object"})
            return
        try:
            with self.server.action_lock:
                result = _run_html_preview_action(self.server.repository, payload)
        except WorkflowError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        except BentoConverterError as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return
        except OSError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, result)

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route == "/":
            self._send(
                HTTPStatus.OK,
                _index_html(self.server.repository, action_token=self.server.action_token),
                "text/html; charset=utf-8",
            )
            return
        if route == "/api/status":
            state, files, current_path = _preview_snapshot(self.server.repository)
            host, port = self.server.server_address[:2]
            single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}
            self._json(HTTPStatus.OK, {
                "format": STATUS_FORMAT,
                "repository": str(self.server.repository),
                "stage": state["workflow"]["stage"],
                "currentChapter": state["workflow"].get("currentChapter"),
                "currentSection": state["workflow"].get("currentSection") if single else None,
                "currentSlide": (
                    next(iter(state["sections"].get(state["workflow"].get("currentSection"), {}).get("slideIds", [])), None)
                    if single else None
                ),
                "currentPath": current_path,
                "chapters": files,
                "mode": state["authoring"]["mode"] if single else "modular",
                "sections": list(state["sections"]) if single else [],
                "slides": [slide for section in state["sections"].values() for slide in section["slideIds"]] if single else [],
                "htmlChange": _html_change_preview(state) if single else None,
                "url": f"http://{host}:{port}/",
            })
            return
        path = _safe_preview_path(self.server.repository, route)
        if path is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = path.read_bytes()
        except OSError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, payload, content_type)


def create_preview_server(repository: str | Path, *, host: str = "127.0.0.1", port: int = 4173) -> HtmlPreviewServer:
    if host != "127.0.0.1":
        raise WorkflowError("HTML preview must bind exactly to 127.0.0.1")
    if port < 1 or port > 65535:
        raise WorkflowError(f"Invalid preview port: {port}")
    root = repository_root(repository)
    load_state(root)
    state = load_state(root)
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        entry = root / state["authoring"]["entryHtml"]
        if not entry.is_file():
            raise WorkflowError(f"Single HTML authoring entry does not exist: {entry}")
    else:
        chapters = root / "chapters"
        if not chapters.is_dir():
            raise WorkflowError(f"chapters/ does not exist: {chapters}")
    return HtmlPreviewServer((host, port), root)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=4173)
    result.add_argument("--stdout-log", type=Path)
    result.add_argument("--stderr-log", type=Path)
    return result


def run(args: argparse.Namespace) -> int:
    server = create_preview_server(args.root, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"BentoSlide HTML preview: http://{host}:{port}/")
    print(f"Repository: {server.repository}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    with contextlib.ExitStack() as stack:
        if args.stdout_log is not None:
            stdout = stack.enter_context(args.stdout_log.open("a", encoding="utf-8", buffering=1))
            stack.enter_context(contextlib.redirect_stdout(stdout))
        if args.stderr_log is not None:
            stderr = stack.enter_context(args.stderr_log.open("a", encoding="utf-8", buffering=1))
            stack.enter_context(contextlib.redirect_stderr(stderr))
        try:
            return run(args)
        except (WorkflowError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
