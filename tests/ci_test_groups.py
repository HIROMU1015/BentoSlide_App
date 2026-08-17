"""Single source of truth for GitHub Actions test-group ownership."""

from __future__ import annotations


VALID_GROUPS = ("unit", "browser", "determinism", "windows")

# Every unittest.TestCase class must appear exactly once. A small number of
# mixed classes use TEST_GROUP_OVERRIDES for individual browser-dependent tests.
CLASS_GROUPS = {
    "tests.app.test_app_backend.ApplicationApiTests": "unit",
    "tests.app.test_app_backend.HtmlReviewServiceTests": "unit",
    "tests.app.test_app_backend.WorkflowServiceViewTests": "unit",
    "tests.app.test_ai_proposal_service.AiProposalServiceTests": "unit",
    "tests.app.test_ai_proposal_service.AiProposalApiTests": "unit",
    "tests.app.test_bento_lifecycle_service.BentoLifecycleServiceTests": "unit",
    "tests.app.test_bento_lifecycle_service.BentoLifecycleApiTests": "unit",
    "tests.app.test_conversion_service.ConversionServiceTests": "unit",
    "tests.app.test_conversion_service.ConversionApiTests": "unit",
    "tests.test_bentoslide_app_node_resolver.BentoSlideAppNodeResolverTests": "windows",
    "tests.test_apply_bento_final_edits.FastFinalEditTests": "unit",
    "tests.test_artifact_transaction.ArtifactTransactionTests": "unit",
    "tests.test_authoring_storage.AuthoringArtifactStorageTests": "unit",
    "tests.test_browser.BrowserIntegrationTests": "browser",
    "tests.test_browser_harness.BrowserHarnessUnitTests": "unit",
    "tests.test_ci_finalization_fixture.CiFinalizationFixtureTests": "unit",
    "tests.test_ci_test_groups.CiTestGroupContractTests": "unit",
    "tests.test_converter.ConverterTests": "unit",
    "tests.test_critical_visual.CriticalVisualTests": "unit",
    "tests.test_deck_migration.DeckMigrationTests": "unit",
    "tests.test_deck_workflow.ProjectMetadataCommandTests": "unit",
    "tests.test_deck_workflow.DeckWorkflowTests": "unit",
    "tests.test_demo_contract.DemoContractTests": "unit",
    "tests.test_determinism.DeterminismHelpersTests": "unit",
    "tests.test_determinism.DeterminismBrowserTests": "determinism",
    "tests.test_fallback_capture_scope.FallbackCaptureScopeTests": "browser",
    "tests.test_html_document.HtmlDocumentTests": "unit",
    "tests.test_html_first.HtmlSourceTests": "unit",
    "tests.test_html_first.HtmlConversionTests": "unit",
    "tests.test_html_first.HtmlFirstBrowserIntegrationTests": "browser",
    "tests.test_html_import.HtmlImportNormalizationTests": "unit",
    "tests.test_html_import.HtmlImportCliTests": "unit",
    "tests.test_html_preview.HtmlPreviewTests": "unit",
    "tests.test_html_preview.SingleHtmlPreviewTests": "unit",
    "tests.test_html_preview.HtmlPreviewActionTests": "unit",
    "tests.test_html_preview.HtmlPreviewBrowserTests": "browser",
    "tests.test_media_poster.MediaPosterTests": "unit",
    "tests.test_native_compatibility.NativeCompatibilityTests": "unit",
    "tests.test_overlap_policy.OverlapPolicyTests": "unit",
    "tests.test_registry_document.RegistryDocumentTests": "unit",
    "tests.test_resource_embedding.ResourceEmbeddingTests": "unit",
    "tests.test_roundtrip.IntegrationRoundtripTests": "unit",
    "tests.test_section_approval.SectionDigestTests": "unit",
    "tests.test_section_approval.SingleHtmlWorkflowTests": "unit",
    "tests.test_segment.SegmentMergeTests": "unit",
    "tests.test_validation.DesignValidationTests": "unit",
    "tests.test_visual_comparison.VisualComparisonTests": "unit",
    "tests.test_visual_assets.VisualAssetTests": "unit",
    "tests.test_windows_launcher.WindowsLauncherTests": "windows",
    "tests.test_windows_workspace_launcher.WindowsWorkspaceLauncherTests": "windows",
    "tests.test_work_editor.WorkEditorTests": "unit",
    "tests.test_work_editor.WorkEditorBrowserTests": "browser",
    "tests.test_workflow_ux.WorkflowUxUnitTests": "unit",
    "tests.test_workflow_ux.RollingSectionBrowserTests": "browser",
}

TEST_GROUP_OVERRIDES = {
    "tests.test_deck_workflow.DeckWorkflowTests.test_explicit_full_reset_rebuilds_authoring_but_never_final": "browser",
    "tests.test_deck_workflow.DeckWorkflowTests.test_segment_cli_imports_new_slide_without_changing_generated_or_final": "browser",
}


def classify_test_id(test_id: str) -> str:
    if test_id in TEST_GROUP_OVERRIDES:
        return TEST_GROUP_OVERRIDES[test_id]
    class_id, separator, _ = test_id.rpartition(".")
    if not separator or class_id not in CLASS_GROUPS:
        raise KeyError(f"Unclassified unittest: {test_id}")
    return CLASS_GROUPS[class_id]
