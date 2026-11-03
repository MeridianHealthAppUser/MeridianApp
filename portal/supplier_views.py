"""Super Admin supplier maintenance. Product links do not change stock or purchase orders."""
from types import SimpleNamespace
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from care.models import AuditEvent, Supplier, SupplierProduct
from care.services import record_audit
from care.suppliers import delete_supplier, save_supplier
from care.table_export import table_workbook
from practices.models import CompanyMembership
from practices.services import active_membership_for, get_active_company
from .filter_menus import list_tools
from .operations_views import OperationsView
from .pagination import numbered_pagination
from .supplier_forms import SupplierForm, SupplierSearchForm, supplier_product_forms
from .workflow_context import make_workflow_context, validate_workflow_context


def supplier_url(**query):
    query = {key: value for key, value in query.items() if value not in ('', None)}
    return reverse('portal:supplier-list') + ('?' + urlencode(query) if query else '')


def supplier_records(company, query=''):
    links = SupplierProduct.objects.filter(company=company, supplier__company=company,
        product__company=company).select_related('product').order_by('product__name', 'product__strength', 'pk')
    suppliers = Supplier.objects.filter(company=company)
    for word in query.split():
        own_link = Q(product_links__company=company, product_links__product__company=company)
        linked_text = (Q(product_links__product__name__icontains=word)
                       | Q(product_links__product__strength__icontains=word)
                       | Q(product_links__supplier_code__icontains=word))
        suppliers = suppliers.filter(Q(name__icontains=word) | Q(contact_name__icontains=word)
            | Q(email__icontains=word) | Q(phone__icontains=word) | (own_link & linked_text))
    return suppliers.distinct().order_by('name', 'pk').prefetch_related(
        Prefetch('product_links', queryset=links, to_attr='display_links'))


def selected_supplier(company, pk):
    try:
        pk = int(pk)
    except (TypeError, ValueError, OverflowError):
        raise Http404('Choose a supplier from this practice.') from None
    if pk < 1 or pk > 9223372036854775807:
        raise Http404('Choose a supplier from this practice.')
    return get_object_or_404(Supplier, company=company, pk=pk)


class SuppliersView(OperationsView):
    nav_section, page_title, shipping = 'suppliers', 'Suppliers', False
    http_method_names = ('get', 'head', 'options')

    def dispatch(self, request, *args, **kwargs):
        # Apply the role gate before method dispatch or lightweight navigation can skip a handler.
        if request.user.is_authenticated:
            company = get_active_company(request)
            membership = active_membership_for(request, company)
            if membership is None or membership.role != CompanyMembership.Role.SUPER_ADMIN:
                raise PermissionDenied('Supplier management is for Super Admins.')
        return super().dispatch(request, *args, **kwargs)

    def require_super_admin(self):
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Supplier management is for Super Admins.')

    def display(self, request, supplier=None, form=None, formset=None, status=200, *, delete_supplier=None, delete_error=''):
        self.require_super_admin()
        data = request.POST if request.method == 'POST' else request.GET
        search_form = SupplierSearchForm(data)
        valid = search_form.is_valid()
        search = search_form.cleaned_data['q'] if valid else ''
        queryset = supplier_records(self.company, search) if valid else Supplier.objects.none()
        page = Paginator(queryset, 25).get_page(data.get('page'))
        query = {'q': search} if search else {}
        return_query = {**query, 'page': page.number} if page.number > 1 else query
        return_url = supplier_url(**return_query)
        suppliers = list(page.object_list)
        for row in suppliers:
            row.link_count = len(row.display_links)
            row.edit_url = supplier_url(**return_query, edit=row.pk)
            row.delete_url = supplier_url(**return_query, delete=row.pk)
        editor_open = form is not None or formset is not None or supplier is not None or data.get('create') == '1'
        if request.method == 'GET' and not editor_open and delete_supplier is None:
            if data.get('edit'):
                supplier = selected_supplier(self.company, data['edit'])
                editor_open = True
            elif data.get('delete'):
                delete_supplier = selected_supplier(self.company, data['delete'])
        supplier_form = form if form is not None else SupplierForm(initial={
            name: getattr(supplier, name) for name in ('name', 'contact_name', 'email', 'phone')
        } if supplier else None)
        product_formset = formset if formset is not None else supplier_product_forms(self.company, supplier=supplier)
        tools = list_tools(action=reverse('portal:supplier-list'), data=data, sections=[], form=search_form,
            label='Search suppliers', placeholder='Supplier, contact, product or code')
        tools.update(filter_menu=None, count_label=f'{page.paginator.count} supplier' + ('' if page.paginator.count == 1 else 's'),
            table_export_url=reverse('portal:supplier-export') + ('?' + urlencode(query) if query else ''),
            table_export_disabled=not valid)
        delete_context = ''
        if delete_supplier is not None:
            delete_context = (request.POST.get('workflow_context', '') if request.method == 'POST' else
                make_workflow_context(request, self.company, 'supplier-delete', delete_supplier))
            delete_supplier.link_count = delete_supplier.product_links.filter(company=self.company,
                product__company=self.company).count()
        context = self.context(suppliers=suppliers, page_obj=page, filter_form=search_form,
            supplier_form=supplier_form, product_formset=product_formset, editor_supplier=supplier,
            supplier_editor_open=editor_open, supplier_delete_open=delete_supplier is not None,
            supplier_delete_error=delete_error,
            delete_supplier=delete_supplier, supplier_delete_context=delete_context,
            supplier_context=(request.POST.get('workflow_context', '') if request.method == 'POST' and editor_open else
                make_workflow_context(request, self.company, 'supplier-update' if supplier else 'supplier-create', supplier)),
            supplier_return_url=return_url, supplier_create_url=supplier_url(**return_query, create=1),
            supplier_form_action=reverse('portal:supplier-edit', kwargs={'pk': supplier.pk}) if supplier else
                reverse('portal:supplier-create'),
            **tools, **numbered_pagination(page, base_url=reverse('portal:supplier-list'), query=query))
        return render(request, 'portal/suppliers.html', context, status=status if valid else 400)

    def get(self, request):
        return self.display(request)


class SupplierCreateView(SuppliersView):
    http_method_names = ('get', 'post', 'head', 'options')

    def get(self, request):
        return self.display(request, form=SupplierForm())

    def post(self, request):
        self.require_super_admin()
        form, formset = SupplierForm(request.POST), supplier_product_forms(self.company, request.POST)
        try:
            intent = validate_workflow_context(request, self.company, 'supplier-create')
            valid_details, valid_products = form.is_valid(), formset.is_valid()
            if valid_details and valid_products:
                supplier = save_supplier(company=self.company, actor=request.user, **form.cleaned_data,
                    products=[{key: line.cleaned_data[key] for key in ('product', 'supplier_code')}
                              for line in formset if line.cleaned_data and not line.cleaned_data.get('DELETE')],
                    submission_key=intent['key'], request=request)
                messages.success(request, f'{supplier.name} created.')
                return redirect(supplier_url())
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, form=form, formset=formset, status=400)


class SupplierEditView(SuppliersView):
    http_method_names = ('get', 'post', 'head', 'options')

    def get(self, request, pk):
        self.require_super_admin()
        return self.display(request, supplier=selected_supplier(self.company, pk))

    def post(self, request, pk):
        self.require_super_admin()
        supplier = selected_supplier(self.company, pk)
        form, formset = SupplierForm(request.POST), supplier_product_forms(self.company, request.POST, supplier=supplier)
        try:
            intent = validate_workflow_context(request, self.company, 'supplier-update', supplier, check_version=False)
            valid_details, valid_products = form.is_valid(), formset.is_valid()
            if valid_details and valid_products:
                supplier = save_supplier(company=self.company, actor=request.user, supplier=supplier, **form.cleaned_data,
                    products=[{key: line.cleaned_data[key] for key in ('product', 'supplier_code')}
                              for line in formset if line.cleaned_data and not line.cleaned_data.get('DELETE')],
                    submission_key=intent['key'], expected_updated=intent['updated'], request=request)
                messages.success(request, f'{supplier.name} saved.')
                return redirect(supplier_url())
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, supplier=supplier, form=form, formset=formset, status=400)


class SupplierDeleteView(SuppliersView):
    http_method_names = ('get', 'post', 'head', 'options')

    def get(self, request, pk):
        self.require_super_admin()
        return self.display(request, delete_supplier=selected_supplier(self.company, pk))

    def post(self, request, pk):
        self.require_super_admin()
        supplier = Supplier.objects.filter(company=self.company, pk=pk).first()
        # An already completed, signed delete can be replayed after its row is gone.
        record = supplier or SimpleNamespace(pk=pk)
        try:
            intent = validate_workflow_context(request, self.company, 'supplier-delete', record, check_version=False)
            if supplier is None:
                if not AuditEvent.objects.filter(company=self.company, actor=request.user,
                    action='supplier.deleted', target_type=Supplier._meta.label_lower, target_id=str(pk),
                    metadata__submission_key=intent['key']).exists():
                    raise Http404('Choose a supplier from this practice.')
                supplier = Supplier(pk=pk, company=self.company)
            delete_supplier(company=self.company, actor=request.user, supplier=supplier,
                submission_key=intent['key'], expected_updated=intent['updated'], request=request)
            messages.success(request, 'Supplier deleted. Catalogue products are unchanged.')
            return redirect(supplier_url())
        except ValidationError as error:
            if supplier is None:
                raise Http404('Choose a supplier from this practice.') from None
            return self.display(request, delete_supplier=supplier, delete_error=' '.join(error.messages), status=400)


class SupplierExportView(SuppliersView):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        self.require_super_admin()
        form = SupplierSearchForm(request.GET)
        if not form.is_valid():
            return HttpResponse('Correct the supplier search before exporting.', status=400, content_type='text/plain')

        def rows():
            for supplier in supplier_records(self.company, form.cleaned_data['q']).iterator(chunk_size=500):
                for link in supplier.display_links or [None]:
                    yield (supplier.name, supplier.contact_name, supplier.email, supplier.phone,
                        link.product.name if link else '', link.product.strength if link else '',
                        link.product.get_category_display() if link else '', link.supplier_code if link else '')

        try:
            output, count = table_workbook('Suppliers',
                ('Supplier', 'Contact person', 'Email', 'Phone', 'Product', 'Strength', 'Category', 'Supplier product code'),
                rows(), widths=(35, 28, 38, 22, 38, 23, 25, 28))
        except ValidationError as error:
            return HttpResponse(' '.join(error.messages), status=400, content_type='text/plain')
        try:
            if request.method != 'HEAD':
                record_audit(company=self.company, actor=request.user, action='supplier.exported', request=request,
                             metadata={'rows': count})
            response = FileResponse(output, as_attachment=True, filename='suppliers.xlsx',
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
        except Exception:
            output.close()
            raise
