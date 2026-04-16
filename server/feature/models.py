from django.db import models

from basics.models import TimeStampVisibleDescriptionModel


class Discussion(TimeStampVisibleDescriptionModel):
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='discussions',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
            models.Index(fields=['organization', 'is_active']),
        ]

    def __str__(self) -> str:
        return self.name or str(self.uuid)
