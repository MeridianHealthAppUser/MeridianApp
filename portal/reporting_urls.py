from django.urls import path
from .reporting_views import MetricsView, StatementCreateView, StatementDetailView, StatementListView

urlpatterns = [
    path('metrics/', MetricsView.as_view(), name='metrics'),
    path('metrics/export.csv', MetricsView.as_view(export=True), name='metrics-export'),
    path('activity-statements/', StatementListView.as_view(), name='activity-statements'),
    path('activity-statements/new/', StatementCreateView.as_view(), name='activity-statement-create'),
    path('activity-statements/<int:pk>/', StatementDetailView.as_view(), name='activity-statement-detail'),
    path('activity-statements/<int:pk>/export.csv', StatementDetailView.as_view(export=True), name='activity-statement-export'),
]
