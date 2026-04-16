# File location: /server/basics/models.py
from django.db import models
from uuid import uuid4
from django.contrib.postgres.indexes import BrinIndex


class UUIDModel(models.Model):

    uuid = models.UUIDField(primary_key=True, default=uuid4, editable=False)

    class Meta:
        abstract = True


class TimestampedModel(UUIDModel):

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

        # By default, any model that inherits from `TimestampedModel` should
        # be ordered in reverse-chronological order. We can override this on a
        # per-model basis as needed, but reverse-chronological is a good
        # default ordering for most models.
        indexes = (
            BrinIndex(fields=['created_at']),
        )
        ordering = ['-created_at', '-updated_at']


class TimeStampVisibleModel(TimestampedModel):

    is_deleted = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)

    class Meta:
        abstract = True


class TimeStampVisibleDescriptionModel(TimeStampVisibleModel):
    name = models.CharField(max_length=255)
    description = models.TextField(default='')

    class Meta:
        abstract = True
