from __future__ import annotations

from app.backend.models.view_models import BentoIntegrationResponse
from app.backend.services.workflow_service import WorkflowService


class BentoService:
    def __init__(self, workflow: WorkflowService):
        self.workflow = workflow

    def integration(self) -> BentoIntegrationResponse:
        state = self.workflow.state_view()
        if state.canEditBento and state.bentoEditorUrl:
            return BentoIntegrationResponse(
                available=True,
                editorUrl=state.bentoEditorUrl,
                message="既存のBento編集画面を開いています。",
            )
        return BentoIntegrationResponse(
            available=False,
            editorUrl=None,
            message="Bento編集は、HTMLの確認と変換が完了すると利用できます。",
        )
