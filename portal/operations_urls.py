from django.urls import path
from . import operations_views as views

urlpatterns = [
    path('catalogue/', views.CatalogueView.as_view(), name='ops-catalogue'),
    path('catalogue/new/', views.ProductEditorView.as_view(), name='ops-product-create'),
    path('catalogue/<int:pk>/', views.ProductEditorView.as_view(), name='ops-product-detail'),
    path('stock/', views.StockListView.as_view(), name='ops-stock'),
    path('stock/receive/', views.StockReceiveView.as_view(), name='ops-stock-receive'),
    path('stock/export.csv', views.StockExportView.as_view(), name='ops-stock-export'),
    path('stock/<int:pk>/', views.BatchDetailView.as_view(), name='ops-batch-detail'),
    path('shipping/', views.ShippingListView.as_view(), name='ops-shipping'),
    path('shipping/new/', views.ShipmentCreateView.as_view(), name='ops-shipping-create'),
    path('shipping/lock/', views.ShippingLockView.as_view(), name='ops-shipping-lock'),
    path('shipping/manifest.csv', views.ManifestDownloadView.as_view(), name='ops-manifest'),
    path('shipping/<int:pk>/', views.ShipmentDetailView.as_view(), name='ops-shipment-detail'),
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
