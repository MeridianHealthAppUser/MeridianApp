(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('suppliers', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  const form = document.querySelector('[data-supplier-editor]');
  if (form) {
    const container = form.querySelector('[data-supplier-product-rows]');
    const template = form.querySelector('[data-supplier-product-template]');
    const total = form.querySelector('[name="products-TOTAL_FORMS"]');
    const initial = form.querySelector('[name="products-INITIAL_FORMS"]');
    const add = form.querySelector('[data-add-supplier-product]');
    const count = form.querySelector('[data-supplier-product-count]');
    const rows = () => [...container.querySelectorAll('[data-supplier-product-row]')];
    // This is a complete replacement list, rather than a ModelFormSet with persisted row IDs.
    rows().filter(row => row.querySelector('[name$="-DELETE"]')?.checked).forEach(row => row.remove());
    function update() {
      rows().forEach((row,index) => row.querySelectorAll('*').forEach(element => {
        for (const attribute of ['name','id','for','aria-describedby']) {
          const value = element.getAttribute(attribute);
          if (value) element.setAttribute(attribute,value.replace(/products-(?:\d+|__prefix__)-/g,`products-${index}-`));
        }
      }));
      total.value=rows().length; initial.value='0';
      add.disabled=rows().length>=100;
      const selected=rows().filter(row=>row.querySelector('select')?.value).length;
      count.textContent=`${selected} product${selected===1?'':'s'}`;
    }
    listen(add,'click',()=>{if(rows().length>=100)return;container.append(template.content.cloneNode(true));update();rows().at(-1).querySelector('select')?.focus();});
    listen(container,'click',event=>{const remove=event.target.closest('[data-remove-supplier-product]');if(!remove)return;remove.closest('[data-supplier-product-row]').remove();update();});
    listen(container,'change',event=>{if(event.target.matches('select'))update();});
    update();
  }
  document.querySelectorAll('#supplier-editor,#supplier-delete').forEach(dialog=>{
    listen(dialog,'close',()=>{
      const url=new URL(location.href); ['create','edit','delete'].forEach(key=>url.searchParams.delete(key));
      const destination=dialog.querySelector('a[data-dialog-close]')?.getAttribute('href') || url.pathname+url.search;
      if(window.MeridianNavigation?.replaceUrl) window.MeridianNavigation.replaceUrl(destination);
      else history.replaceState(history.state,'',destination);
    });
  });
});
