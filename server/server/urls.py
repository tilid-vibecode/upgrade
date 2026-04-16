# File location: /server/server/urls.py
from django.contrib import admin
from django.urls import path
from django.http import JsonResponse
from django.db import connection

def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1;')
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=500)

urlpatterns = [
    path('healthz/', healthz),
    path('admin/', admin.site.urls),
]
