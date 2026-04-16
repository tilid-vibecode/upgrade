from asgiref.sync import async_to_sync
from django.test import TestCase

from company_intake.models import IntakeWorkspace

from .models import MediaFile


class PrototypeMediaScopingTests(TestCase):
    def test_create_pending_can_persist_prototype_workspace_owner(self):
        workspace = IntakeWorkspace.objects.create(name='Acme Cloud', slug='acme-cloud')

        media_file = async_to_sync(MediaFile.objects.create_pending)(
            organization=None,
            uploaded_by=None,
            original_filename='roadmap.pdf',
            content_type='application/pdf',
            file_size=1024,
            file_category='document',
            persistent_key='prototype/acme-cloud/roadmap.pdf',
            processing_key='processing/acme-cloud/roadmap.pdf',
            prototype_workspace=workspace,
        )

        self.assertEqual(media_file.prototype_workspace, workspace)

    def test_prototype_workspace_list_only_returns_owned_files(self):
        workspace_a = IntakeWorkspace.objects.create(name='Acme Cloud', slug='acme-cloud')
        workspace_b = IntakeWorkspace.objects.create(name='Beta Cloud', slug='beta-cloud')

        public_file = MediaFile.objects.create(
            original_filename='public.txt',
            content_type='text/plain',
            file_size=10,
            file_category='text',
            persistent_key='prototype/public.txt',
            status=MediaFile.Status.UPLOADED,
        )
        owned_a = MediaFile.objects.create(
            original_filename='a.txt',
            content_type='text/plain',
            file_size=10,
            file_category='text',
            persistent_key='prototype/a.txt',
            status=MediaFile.Status.UPLOADED,
            prototype_workspace=workspace_a,
        )
        MediaFile.objects.create(
            original_filename='b.txt',
            content_type='text/plain',
            file_size=10,
            file_category='text',
            persistent_key='prototype/b.txt',
            status=MediaFile.Status.UPLOADED,
            prototype_workspace=workspace_b,
        )

        public_files = async_to_sync(MediaFile.objects.list_public_prototype)()
        workspace_a_files = async_to_sync(MediaFile.objects.list_for_prototype_workspace)(workspace_a.slug)

        self.assertEqual([item.uuid for item in public_files], [public_file.uuid])
        self.assertEqual([item.uuid for item in workspace_a_files], [owned_a.uuid])
        self.assertEqual(async_to_sync(MediaFile.objects.count_public_prototype)(), 1)
        self.assertEqual(async_to_sync(MediaFile.objects.count_for_prototype_workspace)(workspace_a.slug), 1)
