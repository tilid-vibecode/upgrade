from typing import List

from asgiref.sync import sync_to_async
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from .entities import (
    CompanyDocumentUploadResponse,
    IntakeWorkspaceResponse,
    WorkspaceDocumentListResponse,
)
from .models import IntakeWorkspace, SourceDocument
from .services import (
    build_document_response,
    build_workspace_response,
    store_company_document,
)

company_intake_router = APIRouter(prefix='/company-intake', tags=['company-intake'])


@company_intake_router.post(
    '/documents/upload',
    response_model=CompanyDocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_company_document(
    company_name: str = Form(...),
    notes: str = Form(''),
    file: UploadFile = File(...),
):
    workspace, document = await store_company_document(
        company_name=company_name,
        notes=notes,
        file=file,
    )

    document = await sync_to_async(
        SourceDocument.objects.select_related('workspace').get
    )(uuid=document.uuid)

    return CompanyDocumentUploadResponse(
        workspace=build_workspace_response(workspace),
        document=await build_document_response(document),
    )


@company_intake_router.get(
    '/workspaces',
    response_model=List[IntakeWorkspaceResponse],
)
async def list_intake_workspaces():
    workspaces = await sync_to_async(list)(
        IntakeWorkspace.objects.order_by('-updated_at')
    )
    return [build_workspace_response(workspace) for workspace in workspaces]


@company_intake_router.get(
    '/workspaces/{workspace_slug}/documents',
    response_model=WorkspaceDocumentListResponse,
)
async def list_workspace_documents(
    workspace_slug: str,
    include_signed_urls: bool = Query(True),
):
    workspace = await sync_to_async(
        IntakeWorkspace.objects.filter(slug=workspace_slug).first
    )()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Workspace not found.',
        )

    documents = await sync_to_async(list)(
        SourceDocument.objects.select_related('workspace')
        .filter(workspace=workspace)
        .order_by('-created_at')
    )

    return WorkspaceDocumentListResponse(
        workspace=build_workspace_response(workspace),
        documents=[
            await build_document_response(doc, include_signed_url=include_signed_urls)
            for doc in documents
        ],
    )
