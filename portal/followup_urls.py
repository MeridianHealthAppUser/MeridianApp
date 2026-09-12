from django.urls import path
from .followup_views import DropoutListView, FollowUpDetailView

urlpatterns = [
    path('leads/<int:pk>/follow-up/', FollowUpDetailView.as_view(), name='lead-follow-up'),
    path('dropouts/', DropoutListView.as_view(), name='dropouts'),
    path('dropouts/<int:pk>/', FollowUpDetailView.as_view(is_lead=False), name='dropout-detail'),
]
