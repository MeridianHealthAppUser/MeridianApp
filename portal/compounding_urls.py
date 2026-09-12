from django.urls import path

from . import compounding_views as views


urlpatterns = [
    path('compounding/', views.CompoundingListView.as_view(), name='compounding-list'),
    path('authorisations/<int:authorization_pk>/compounding/new/', views.CompoundingCreateView.as_view(), name='compounding-create'),
    path('compounding/<int:pk>/', views.CompoundingDetailView.as_view(), name='compounding-detail'),
    path('compounding/<int:pk>/print/', views.CompoundingPrintView.as_view(), name='compounding-print'),
    path('review-rules/run/', views.ReviewRunView.as_view(), name='treatment-review-run'),
]
