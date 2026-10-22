from django.urls import path
from .followup_views import CallbackCalledView, DropoutListView, FollowUpDetailView, HelpRequestResolveView

urlpatterns = [
    path('leads/<int:pk>/follow-up/', FollowUpDetailView.as_view(), name='lead-follow-up'),
    path('dropouts/', DropoutListView.as_view(), name='dropouts'),
    path('dropouts/<int:pk>/', FollowUpDetailView.as_view(is_lead=False), name='dropout-detail'),
    path('dropouts/help/<int:pk>/resolve/', HelpRequestResolveView.as_view(), name='help-request-resolve'),
    path('dropouts/calls/<int:pk>/called/', CallbackCalledView.as_view(), name='callback-called'),
]
