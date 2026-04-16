from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class MediaFileResponse(BaseModel):
    uuid: UUID
    original_filename: str
    content_type: str
    file_size: int
    file_category: str
    status: str
    error_msg: str = ''
    processing_description: str = ''
    has_persistent: bool = False
    has_processing: bool = False
    created_at: datetime
    updated_at: datetime
    uploaded_by_email: Optional[str] = None
    uploaded_by_uuid: Optional[UUID] = None

    class Config:
        from_attributes = True


class MediaFileDetailResponse(BaseModel):
    uuid: UUID
    original_filename: str
    content_type: str
    file_size: int
    file_category: str
    status: str
    error_msg: str = ''
    processing_description: str = ''
    processing_metadata: dict = Field(default_factory=dict)
    has_persistent: bool = False
    has_processing: bool = False
    created_at: datetime
    updated_at: datetime
    uploaded_by_email: Optional[str] = None
    uploaded_by_uuid: Optional[UUID] = None
    variants: List['MediaFileVariantResponse'] = Field(default_factory=list)

    class Config:
        from_attributes = True


class MediaFileVariantResponse(BaseModel):
    uuid: UUID
    variant_type: str
    content_type: str
    file_size: int
    width: Optional[int] = None
    height: Optional[int] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime

    class Config:
        from_attributes = True


class MediaFileListResponse(BaseModel):
    files: List[MediaFileResponse]
    total: int
    limit: int
    offset: int


class SignedUrlResponse(BaseModel):
    url: str
    expires_in_seconds: int
    variant_type: str
    file_uuid: UUID


class MediaFileUpdateRequest(BaseModel):
    processing_description: Optional[str] = Field(
        None,
        max_length=10000,
        description='User-editable description of the file or processing results',
    )


class MediaFileBatchRequest(BaseModel):
    file_uuids: List[UUID] = Field(
        ...,
        min_length=1,
        max_length=50,
        description='List of file UUIDs to resolve',
    )


class MediaFileBatchResponse(BaseModel):
    files: List['MediaFileBatchItemResponse']
    not_found: List[UUID] = Field(
        default_factory=list,
        description='UUIDs that were not found or not accessible',
    )


class MediaFileBatchItemResponse(MediaFileResponse):
    signed_url: Optional[str] = Field(
        None,
        description='Pre-signed URL for the file (images only by default)',
    )


class MediaFileListParams(BaseModel):
    file_category: Optional[str] = Field(
        None,
        description='Filter by category: image, document, word, text, spreadsheet',
    )
    status: Optional[str] = Field(
        None,
        description='Filter by status: pending, uploaded, processing, ready, failed',
    )
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)

    @field_validator('file_category')
    @classmethod
    def validate_category(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        valid = {'image', 'document', 'word', 'text', 'spreadsheet'}
        if v.lower() not in valid:
            raise ValueError(
                f'Invalid file_category. Must be one of: {", ".join(sorted(valid))}'
            )
        return v.lower()

    @field_validator('status')
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        valid = {'pending', 'uploaded', 'processing', 'ready', 'failed'}
        if v.lower() not in valid:
            raise ValueError(
                f'Invalid status. Must be one of: {", ".join(sorted(valid))}'
            )
        return v.lower()


class PrototypeMediaUploadResponse(BaseModel):
    file: MediaFileResponse
    signed_url: Optional[str] = None


MediaFileDetailResponse.model_rebuild()
MediaFileBatchResponse.model_rebuild()
