from django.contrib import admin

from .models import SkillBlueprintRun


@admin.register(SkillBlueprintRun)
class SkillBlueprintRunAdmin(admin.ModelAdmin):
    list_display = ('title', 'workspace', 'status', 'updated_at')
    list_filter = ('status',)
    search_fields = ('title', 'workspace__name', 'workspace__slug')
    raw_id_fields = ('workspace',)
