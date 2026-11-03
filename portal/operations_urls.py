from django.urls import path
from . import operations_views as views
from .starting_plan_views import StartingPlanEditorView

from .inventory_views import (InventoryLeadTimeView, InventoryMovementsExportView, InventoryMovementsView,
                              InventorySafetyStockView, InventoryStartingAWCView, InventoryView)
from .purchasing_views import PurchaseOrderCreateView, PurchaseOrderReceiveView, PurchasingView
from .purchase_document_views import PurchaseOrderEmailView, PurchaseOrderExcelView, PurchaseOrderPDFView
from .table_export_views import TableExportView
from .consumption_views import ConsumptionView, ConsumptionDetailView, ConsumptionExportView
from .supplier_views import SuppliersView, SupplierCreateView, SupplierEditView, SupplierDeleteView, SupplierExportView
from .demo_views import DemoDataView, DemoDataActionView

urlpatterns = [
    path('demo-data/', DemoDataView.as_view(), name='demo-data'),
    path('demo-data/create/', DemoDataActionView.as_view(action='create'), name='demo-data-create'),
    path('demo-data/clear/', DemoDataActionView.as_view(action='clear'), name='demo-data-clear'),
    path('demo-data/rebuild/', DemoDataActionView.as_view(action='rebuild'), name='demo-data-rebuild'),
    path('table-exports/<slug:table>.xlsx', TableExportView.as_view(), name='ops-table-export'),
    path('purchasing/', PurchasingView.as_view(), name='purchasing'),
    path('suppliers/', SuppliersView.as_view(), name='supplier-list'),
    path('suppliers/export.xlsx', SupplierExportView.as_view(), name='supplier-export'),
    path('suppliers/create/', SupplierCreateView.as_view(), name='supplier-create'),
    path('suppliers/<int:pk>/edit/', SupplierEditView.as_view(), name='supplier-edit'),
    path('suppliers/<int:pk>/delete/', SupplierDeleteView.as_view(), name='supplier-delete'),
    path('purchasing/create/', PurchaseOrderCreateView.as_view(), name='purchase-order-create'),
    path('purchasing/<int:pk>/receive/', PurchaseOrderReceiveView.as_view(), name='purchase-order-receive'),
    path('purchasing/<int:pk>/order.xlsx', PurchaseOrderExcelView.as_view(), name='purchase-order-excel'),
    path('purchasing/<int:pk>/order.pdf', PurchaseOrderPDFView.as_view(), name='purchase-order-pdf'),
    path('purchasing/<int:pk>/email/', PurchaseOrderEmailView.as_view(), name='purchase-order-email'),
    path('inventory/', InventoryView.as_view(), name='inventory'),
    path('inventory/consumption/', ConsumptionView.as_view(), name='inventory-consumption'),
    path('inventory/consumption.xlsx', ConsumptionExportView.as_view(), name='inventory-consumption-export'),
    path('inventory/consumption/<int:pk>/<str:week>.xlsx', ConsumptionExportView.as_view(), name='inventory-consumption-detail-export'),
    path('inventory/consumption/<int:pk>/<str:week>/', ConsumptionDetailView.as_view(), name='inventory-consumption-detail'),
    path('inventory/<int:pk>/safety-stock/', InventorySafetyStockView.as_view(), name='inventory-safety-stock'),
    path('inventory/<int:pk>/lead-time/', InventoryLeadTimeView.as_view(), name='inventory-lead-time'),
    path('inventory/<int:pk>/starting-awc/', InventoryStartingAWCView.as_view(), name='inventory-starting-awc'),
    path('inventory/movements/', InventoryMovementsView.as_view(), name='inventory-movements'),
    path('inventory/movements.xlsx', InventoryMovementsExportView.as_view(), name='inventory-movements-export'),
    path('catalogue/', views.CatalogueView.as_view(), name='ops-catalogue'),
    path('catalogue/new/', views.ProductEditorView.as_view(), name='ops-product-create'),
    path('catalogue/<int:pk>/', views.ProductEditorView.as_view(), name='ops-product-detail'),
    path('catalogue/doses/new/', views.DoseEditorView.as_view(), name='ops-dose-create'),
    path('catalogue/doses/<int:pk>/', views.DoseEditorView.as_view(), name='ops-dose-detail'),
    path('catalogue/starting-plans/new/', StartingPlanEditorView.as_view(), name='ops-starting-plan-create'),
    path('catalogue/starting-plans/<int:pk>/', StartingPlanEditorView.as_view(), name='ops-starting-plan-detail'),
    path('shipping/', views.ShippingListView.as_view(), name='ops-shipping'),
    path('shipping/new/', views.ShipmentCreateView.as_view(), name='ops-shipping-create'),
    path('shipping/confirm/', views.ShippingConfirmView.as_view(), name='ops-shipping-confirm'),
    path('shipping/lock/', views.ShippingLockView.as_view(), name='ops-shipping-lock'),
    path('shipping/mark-ready/', views.ShippingPrepareView.as_view(), name='ops-shipping-prepare'),
    path('shipping/manifest.csv', views.ManifestDownloadView.as_view(), name='ops-manifest'),
    path('shipping/order.csv', views.WeekOrderDownloadView.as_view(), name='ops-week-order'),
    path('shipping/<int:pk>/', views.ShipmentDetailView.as_view(), name='ops-shipment-detail'),
    path('shipping/<int:pk>/change/', views.ShipmentChangeView.as_view(), name='ops-shipment-change'),
    path('shipping/<int:pk>/ready/', views.ShipmentReadyView.as_view(), name='ops-shipment-ready'),
    path('shipping/<int:pk>/note/', views.ShipmentNoteView.as_view(), name='ops-shipment-note'),
    path('shipping/<int:pk>/label/', views.ShipmentLabelView.as_view(), name='ops-shipment-label'),
    path('courier/webhooks/<str:token>/', views.CourierWebhookView.as_view(), name='courier-webhook'),
    path('shipping/courier/', views.ShippingCourierView.as_view(), name='ops-shipping-courier'),
    path('shipping/collect/', views.ShippingCollectView.as_view(), name='ops-shipping-collect'),
    path('shipping/address/<int:pk>/', views.DeliveryAddressView.as_view(), name='ops-delivery-address'),
    path('dispatch-stock/', views.DispatchStockView.as_view(), name='ops-stock'),
    path('dispatch-history/', views.DispatchHistoryView.as_view(), name='ops-history'),
    path('dispatch-history/export.xlsx', views.DispatchHistoryExportView.as_view(), name='ops-history-export'),
    path('supply-requests/', views.StaffOrderListView.as_view(), name='ops-orders'),
    path('supply-requests/<int:pk>/', views.StaffOrderDetailView.as_view(), name='ops-order-detail'),
    path('patient/pharmacy/', views.PatientPharmacyView.as_view(), name='patient-pharmacy'),
    path('patient/pharmacy/basket/', views.PatientPharmacyView.as_view(), name='patient-basket'),
    path('patient/pharmacy/items/<int:pk>/', views.PatientPharmacyView.as_view(), name='patient-basket-item'),
    path('patient/orders/', views.PatientOrderListView.as_view(), name='patient-orders'),
    path('patient/orders/<int:pk>/', views.PatientOrderDetailView.as_view(), name='patient-order-detail'),
]
