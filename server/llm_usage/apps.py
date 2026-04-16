from django.apps import AppConfig


class LlmUsageConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'llm_usage'
    verbose_name = 'LLM Usage Tracking'
