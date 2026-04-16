from django.conf import settings
from django.db import models

from basics.models import TimeStampVisibleDescriptionModel, TimeStampVisibleModel


class OrgRole(models.TextChoices):
    OWNER = 'owner', 'Owner'
    ADMIN = 'admin', 'Admin'
    MEMBER = 'member', 'Member'
    VIEWER = 'viewer', 'Viewer'


ROLE_HIERARCHY = [OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MEMBER, OrgRole.VIEWER]


def has_role_permission(user_role: str, required_role: str) -> bool:
    try:
        user_index = ROLE_HIERARCHY.index(user_role)
        required_index = ROLE_HIERARCHY.index(required_role)
        return user_index <= required_index
    except ValueError:
        return False


class Organization(TimeStampVisibleDescriptionModel):
    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return self.name or str(self.uuid)


class OrganizationMembership(TimeStampVisibleModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='organization_memberships',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    role = models.CharField(
        max_length=32,
        choices=OrgRole.choices,
        default=OrgRole.MEMBER,
    )

    class Meta:
        unique_together = [('user', 'organization')]
        indexes = [
            models.Index(fields=['organization', 'role']),
        ]

    def __str__(self) -> str:
        return f'{self.user_id} in {self.organization_id} ({self.role})'
