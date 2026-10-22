from django.urls import path
from . import operations_views as views
from .starting_plan_views import StartingPlanEditorView

urlpatterns = [
    path('catalogue/', views.CatalogueView.as_view(), name='ops-catalogue'),
    path('catalogue/new/', views.ProductEditorView.as_view(), name='ops-product-create'),
    path('catalogue/<int:pk>/', views.ProductEditorView.as_view(), name='ops-product-detail'),
    path('catalogue/doses/new/', views.DoseEditorView.as_view(), name='ops-dose-create'),
    path('catalogue/doses/<int:pk>/', views.DoseEditorView.as_view(), name='ops-dose-detail'),
    path('catalogue/starting-plans/new/', StartingPlanEditorView.as_view(), name='ops-starting-plan-create'),
    path('catalogue/starting-plans/<int:pk>/', StartingPlanEditorView.as_view(), name='ops-starting-plan-detail'),
    path('shipping/', views.ShippingListView.as_view(), name='ops-shipping'),
    path('shipping/new/', views.ShipmentCreateView.as_view(), name='ops-shipping-create'),
    path('shipping/lock/', views.ShippingLockView.as_view(), name='ops-shipping-lock'),
    path('shipping/mark-ready/', views.ShippingPrepareView.as_view(), name='ops-shipping-prepare'),
    path('shipping/manifest.csv', views.ManifestDownloadView.as_view(), name='ops-manifest'),
    path('shipping/order.csv', views.WeekOrderDownloadView.as_view(), name='ops-week-order'),
    path('shipping/<int:pk>/', views.ShipmentDetailView.as_view(), name='ops-shipment-detail'),
    path('shipping/<int:pk>/change/', views.ShipmentChangeView.as_view(), name='ops-shipment-change'),
    path('shipping/<int:pk>/ready/', views.ShipmentReadyView.as_view(), name='ops-shipment-ready'),
    path('shipping/<int:pk>/note/', views.ShipmentNoteView.as_view(), name='ops-shipment-note'),
    path('dispatch-stock/', views.DispatchStockView.as_view(), name='ops-stock'),
    path('dispatch-history/', views.DispatchHistoryView.as_view(), name='ops-history'),
    path('dispatch-history/export.csv', views.ManifestDownloadView.as_view(history=True), name='ops-history-export'),
    path('supply-requests/', views.StaffOrderListView.as_view(), name='ops-orders'),
    path('supply-requests/<int:pk>/', views.StaffOrderDetailView.as_view(), name='ops-order-detail'),
    path('patient/pharmacy/', views.PatientPharmacyView.as_view(), name='patient-pharmacy'),
    path('patient/pharmacy/basket/', views.PatientBasketView.as_view(), name='patient-basket'),
    path('patient/pharmacy/items/<int:pk>/', views.PatientBasketItemView.as_view(), name='patient-basket-item'),
    path('patient/orders/', views.PatientOrderListView.as_view(), name='patient-orders'),
    path('patient/orders/<int:pk>/', views.PatientOrderDetailView.as_view(), name='patient-order-detail'),
]
