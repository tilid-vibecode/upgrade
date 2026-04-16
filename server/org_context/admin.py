from django.contrib import admin

from .models import (
    CatalogResolutionReviewItem,
    ContextProfile,
    Employee,
    EmployeeCapabilityAvailability,
    EmployeeCVMatchCandidate,
    EmployeeCVProfile,
    EmployeeOrgAssignment,
    EmployeeProjectAssignment,
    AllocationConstraint,
    EmployeeSkillEvidence,
    EscoConceptScheme,
    EscoDictionaryEntry,
    EscoGreenOccupationShare,
    EscoImportRun,
    EscoIscoGroup,
    EscoOccupation,
    EscoOccupationBroaderRelation,
    EscoOccupationCollectionMembership,
    EscoOccupationLabel,
    EscoOccupationSkillRelation,
    EscoSkill,
    EscoSkillBroaderRelation,
    EscoSkillCollectionMembership,
    EscoSkillGroup,
    EscoSkillHierarchyPath,
    EscoSkillLabel,
    EscoSkillRelation,
    OccupationResolutionOverride,
    OccupationMapping,
    OrgUnit,
    ParsedSource,
    PlanningContext,
    PlanningContextSource,
    ProjectCapabilityDemand,
    Project,
    RoadmapAnalysisRun,
    ReportingLine,
    RoleProfile,
    RoleSkillRequirement,
    Skill,
    SkillAlias,
    SkillResolutionOverride,
    SourceChunk,
)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'current_title', 'email', 'workspace')
    search_fields = ('full_name', 'email', 'external_employee_id', 'workspace__slug')
    raw_id_fields = ('workspace',)


@admin.register(OrgUnit)
class OrgUnitAdmin(admin.ModelAdmin):
    list_display = ('name', 'unit_kind', 'workspace')
    list_filter = ('unit_kind',)
    search_fields = ('name', 'workspace__slug')
    raw_id_fields = ('workspace',)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'workspace')
    search_fields = ('name', 'workspace__slug')
    raw_id_fields = ('workspace',)


@admin.register(ParsedSource)
class ParsedSourceAdmin(admin.ModelAdmin):
    list_display = ('source', 'workspace', 'parser_name', 'updated_at')
    search_fields = ('workspace__slug', 'source__title', 'source__media_file__original_filename')
    raw_id_fields = ('workspace', 'source')


@admin.register(SourceChunk)
class SourceChunkAdmin(admin.ModelAdmin):
    list_display = ('parsed_source', 'chunk_index', 'char_count')
    search_fields = ('parsed_source__workspace__slug', 'text')
    raw_id_fields = ('parsed_source',)


@admin.register(RoadmapAnalysisRun)
class RoadmapAnalysisRunAdmin(admin.ModelAdmin):
    list_display = ('title', 'workspace', 'status', 'analysis_version', 'updated_at')
    list_filter = ('status', 'analysis_version')
    search_fields = ('title', 'workspace__slug')
    raw_id_fields = ('workspace',)


@admin.register(ReportingLine)
class ReportingLineAdmin(admin.ModelAdmin):
    list_display = ('manager', 'report', 'workspace')
    raw_id_fields = ('workspace', 'manager', 'report', 'source')


@admin.register(EmployeeOrgAssignment)
class EmployeeOrgAssignmentAdmin(admin.ModelAdmin):
    list_display = ('employee', 'org_unit', 'assignment_kind', 'is_primary')
    list_filter = ('assignment_kind', 'is_primary')
    raw_id_fields = ('workspace', 'employee', 'org_unit', 'source')


@admin.register(EmployeeProjectAssignment)
class EmployeeProjectAssignmentAdmin(admin.ModelAdmin):
    list_display = ('employee', 'project', 'role_label', 'allocation_percent')
    raw_id_fields = ('workspace', 'employee', 'project', 'source')


@admin.register(RoleProfile)
class RoleProfileAdmin(admin.ModelAdmin):
    list_display = ('name', 'family', 'seniority', 'workspace')
    search_fields = ('name', 'family', 'workspace__slug')
    raw_id_fields = ('workspace',)


@admin.register(OccupationMapping)
class OccupationMappingAdmin(admin.ModelAdmin):
    list_display = ('occupation_name_en', 'role_profile', 'esco_occupation', 'match_score')
    search_fields = ('occupation_name_en', 'occupation_key')
    raw_id_fields = ('workspace', 'role_profile', 'esco_occupation')


@admin.register(SkillResolutionOverride)
class SkillResolutionOverrideAdmin(admin.ModelAdmin):
    list_display = ('raw_term', 'display_name_en', 'workspace', 'esco_skill', 'status', 'source')
    list_filter = ('status', 'source')
    search_fields = ('raw_term', 'normalized_term', 'display_name_en', 'canonical_key')
    raw_id_fields = ('workspace', 'esco_skill')


@admin.register(OccupationResolutionOverride)
class OccupationResolutionOverrideAdmin(admin.ModelAdmin):
    list_display = ('raw_term', 'occupation_name_en', 'workspace', 'esco_occupation', 'status', 'source')
    list_filter = ('status', 'source')
    search_fields = ('raw_term', 'normalized_term', 'occupation_name_en', 'occupation_key')
    raw_id_fields = ('workspace', 'esco_occupation')


@admin.register(CatalogResolutionReviewItem)
class CatalogResolutionReviewItemAdmin(admin.ModelAdmin):
    list_display = ('term_kind', 'raw_term', 'workspace', 'status', 'seen_count', 'last_seen_at')
    list_filter = ('term_kind', 'status')
    search_fields = ('raw_term', 'normalized_term', 'workspace__slug')
    raw_id_fields = ('workspace',)


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ('display_name_en', 'display_name_ru', 'canonical_key', 'esco_skill', 'source')
    search_fields = ('display_name_en', 'display_name_ru', 'canonical_key')
    raw_id_fields = ('workspace', 'esco_skill')


@admin.register(SkillAlias)
class SkillAliasAdmin(admin.ModelAdmin):
    list_display = ('alias', 'skill', 'language_code')
    search_fields = ('alias', 'skill__display_name_en', 'skill__display_name_ru')
    raw_id_fields = ('skill',)


@admin.register(RoleSkillRequirement)
class RoleSkillRequirementAdmin(admin.ModelAdmin):
    list_display = ('role_profile', 'skill', 'target_level', 'priority', 'is_required')
    list_filter = ('is_required',)
    raw_id_fields = ('workspace', 'role_profile', 'skill')


@admin.register(EmployeeSkillEvidence)
class EmployeeSkillEvidenceAdmin(admin.ModelAdmin):
    list_display = ('employee', 'skill', 'source_kind', 'current_level', 'confidence', 'weight')
    search_fields = ('employee__full_name', 'skill__display_name_en', 'skill__display_name_ru')
    raw_id_fields = ('workspace', 'employee', 'skill', 'source')


@admin.register(EmployeeCVProfile)
class EmployeeCVProfileAdmin(admin.ModelAdmin):
    list_display = ('source', 'employee', 'status', 'evidence_quality', 'matched_by')
    list_filter = ('status', 'evidence_quality')
    search_fields = ('workspace__slug', 'source__title', 'employee__full_name')
    raw_id_fields = ('workspace', 'source', 'employee')


@admin.register(EmployeeCVMatchCandidate)
class EmployeeCVMatchCandidateAdmin(admin.ModelAdmin):
    list_display = ('profile', 'employee', 'rank', 'score', 'exact_name_match', 'email_match')
    list_filter = ('exact_name_match', 'email_match')
    search_fields = ('workspace__slug', 'employee__full_name', 'profile__source__title')
    raw_id_fields = ('workspace', 'profile', 'employee')


@admin.register(EscoImportRun)
class EscoImportRunAdmin(admin.ModelAdmin):
    list_display = ('dataset_version', 'language_code', 'status', 'updated_at')
    list_filter = ('dataset_version', 'language_code', 'status')
    search_fields = ('dataset_version', 'dataset_path')


@admin.register(EscoSkill)
class EscoSkillAdmin(admin.ModelAdmin):
    list_display = ('preferred_label', 'skill_type', 'reuse_level', 'status')
    list_filter = ('skill_type', 'reuse_level', 'status')
    search_fields = ('preferred_label', 'concept_uri', 'normalized_preferred_label')


@admin.register(EscoSkillLabel)
class EscoSkillLabelAdmin(admin.ModelAdmin):
    list_display = ('label', 'label_kind', 'esco_skill')
    list_filter = ('label_kind', 'language_code')
    search_fields = ('label', 'normalized_label', 'esco_skill__preferred_label')
    raw_id_fields = ('esco_skill',)


@admin.register(EscoSkillGroup)
class EscoSkillGroupAdmin(admin.ModelAdmin):
    list_display = ('preferred_label', 'code', 'status')
    list_filter = ('status',)
    search_fields = ('preferred_label', 'code', 'concept_uri')


@admin.register(EscoOccupation)
class EscoOccupationAdmin(admin.ModelAdmin):
    list_display = ('preferred_label', 'code', 'isco_group', 'status')
    list_filter = ('status', 'isco_group')
    search_fields = ('preferred_label', 'code', 'concept_uri', 'normalized_preferred_label')


@admin.register(EscoOccupationLabel)
class EscoOccupationLabelAdmin(admin.ModelAdmin):
    list_display = ('label', 'label_kind', 'esco_occupation')
    list_filter = ('label_kind', 'language_code')
    search_fields = ('label', 'normalized_label', 'esco_occupation__preferred_label')
    raw_id_fields = ('esco_occupation',)


@admin.register(EscoSkillRelation)
class EscoSkillRelationAdmin(admin.ModelAdmin):
    list_display = ('original_skill', 'relation_type', 'related_skill')
    list_filter = ('relation_type',)
    raw_id_fields = ('original_skill', 'related_skill')


@admin.register(EscoOccupationSkillRelation)
class EscoOccupationSkillRelationAdmin(admin.ModelAdmin):
    list_display = ('occupation', 'relation_type', 'skill')
    list_filter = ('relation_type', 'skill_type')
    raw_id_fields = ('occupation', 'skill')


@admin.register(EscoSkillBroaderRelation)
class EscoSkillBroaderRelationAdmin(admin.ModelAdmin):
    list_display = ('concept_label', 'broader_label')
    search_fields = ('concept_label', 'broader_label', 'concept_uri', 'broader_uri')
    raw_id_fields = ('esco_skill', 'esco_skill_group', 'broader_skill', 'broader_skill_group')


@admin.register(EscoOccupationBroaderRelation)
class EscoOccupationBroaderRelationAdmin(admin.ModelAdmin):
    list_display = ('concept_label', 'broader_label')
    search_fields = ('concept_label', 'broader_label', 'concept_uri', 'broader_uri')
    raw_id_fields = ('esco_occupation', 'broader_occupation')


@admin.register(EscoSkillCollectionMembership)
class EscoSkillCollectionMembershipAdmin(admin.ModelAdmin):
    list_display = ('esco_skill', 'collection_key')
    list_filter = ('collection_key',)
    search_fields = ('collection_key', 'esco_skill__preferred_label')
    raw_id_fields = ('esco_skill',)


@admin.register(EscoOccupationCollectionMembership)
class EscoOccupationCollectionMembershipAdmin(admin.ModelAdmin):
    list_display = ('esco_occupation', 'collection_key')
    list_filter = ('collection_key',)
    search_fields = ('collection_key', 'esco_occupation__preferred_label')
    raw_id_fields = ('esco_occupation',)


@admin.register(EscoConceptScheme)
class EscoConceptSchemeAdmin(admin.ModelAdmin):
    list_display = ('preferred_label', 'title', 'status')
    list_filter = ('status',)
    search_fields = ('preferred_label', 'title', 'concept_scheme_uri')


@admin.register(EscoDictionaryEntry)
class EscoDictionaryEntryAdmin(admin.ModelAdmin):
    list_display = ('filename', 'data_header', 'property_name')
    search_fields = ('filename', 'data_header', 'property_name')


@admin.register(EscoIscoGroup)
class EscoIscoGroupAdmin(admin.ModelAdmin):
    list_display = ('preferred_label', 'code', 'status')
    list_filter = ('status',)
    search_fields = ('preferred_label', 'code', 'concept_uri')


@admin.register(EscoGreenOccupationShare)
class EscoGreenOccupationShareAdmin(admin.ModelAdmin):
    list_display = ('preferred_label', 'code', 'green_share')
    search_fields = ('preferred_label', 'code', 'concept_uri')
    raw_id_fields = ('esco_occupation', 'isco_group')


@admin.register(EscoSkillHierarchyPath)
class EscoSkillHierarchyPathAdmin(admin.ModelAdmin):
    list_display = ('level_0_preferred_term', 'level_1_preferred_term', 'level_2_preferred_term', 'level_3_preferred_term')
    search_fields = (
        'level_0_preferred_term',
        'level_1_preferred_term',
        'level_2_preferred_term',
        'level_3_preferred_term',
    )


@admin.register(PlanningContext)
class PlanningContextAdmin(admin.ModelAdmin):
    list_display = ('name', 'kind', 'status', 'workspace', 'project', 'parent_context')
    list_filter = ('kind', 'status')
    search_fields = ('name', 'slug', 'workspace__slug', 'project__name')
    raw_id_fields = ('workspace', 'organization', 'project', 'parent_context')


@admin.register(ContextProfile)
class ContextProfileAdmin(admin.ModelAdmin):
    list_display = ('planning_context', 'inherit_from_parent')
    list_filter = ('inherit_from_parent',)
    search_fields = ('planning_context__name', 'planning_context__slug')
    raw_id_fields = ('planning_context',)


@admin.register(PlanningContextSource)
class PlanningContextSourceAdmin(admin.ModelAdmin):
    list_display = ('planning_context', 'workspace_source', 'usage_type', 'is_active')
    list_filter = ('usage_type', 'is_active', 'include_in_blueprint', 'include_in_roadmap_analysis')
    search_fields = ('planning_context__name', 'planning_context__slug', 'workspace_source__title')
    raw_id_fields = ('planning_context', 'workspace_source', 'inherited_from')


@admin.register(ProjectCapabilityDemand)
class ProjectCapabilityDemandAdmin(admin.ModelAdmin):
    list_display = ('planning_context', 'project', 'skill', 'role_family', 'priority', 'fte_demand')
    list_filter = ('priority', 'time_horizon', 'source_kind')
    search_fields = ('planning_context__name', 'project__name', 'skill__display_name_en', 'role_family')
    raw_id_fields = ('planning_context', 'project', 'skill')


@admin.register(EmployeeCapabilityAvailability)
class EmployeeCapabilityAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('planning_context', 'employee', 'skill', 'current_level', 'confidence', 'available_fte')
    list_filter = ('confidence',)
    search_fields = ('planning_context__name', 'employee__full_name', 'skill__display_name_en')
    raw_id_fields = ('planning_context', 'employee', 'skill')


@admin.register(AllocationConstraint)
class AllocationConstraintAdmin(admin.ModelAdmin):
    list_display = ('planning_context', 'constraint_type', 'is_hard')
    list_filter = ('constraint_type', 'is_hard')
    search_fields = ('planning_context__name', 'description')
    raw_id_fields = ('planning_context',)
