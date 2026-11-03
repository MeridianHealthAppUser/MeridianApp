(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('demo_data', (lifecycle) => {
  const dialog=document.querySelector('[data-demo-data-dialog]');
  if(!dialog || typeof dialog.showModal!=='function')return;
  const content=dialog.querySelector('[data-demo-data-content]');
  const listen=lifecycle?lifecycle.on:(target,...args)=>target.addEventListener(...args);
  let controller,trigger;
  function close(){controller?.abort();if(dialog.open)dialog.close();trigger?.focus();}
  async function open(opener){
    controller?.abort();controller=new AbortController();const pending=controller;trigger=opener;
    const url=new URL(opener.href,location.href);url.searchParams.set('next',location.pathname+location.search);url.searchParams.set('fragment','1');
    const loading=document.createElement('div');loading.className='demo-data-loading';
    const message=document.createElement('span');message.textContent='Loading demo options…';
    const dismiss=document.createElement('button');dismiss.type='button';dismiss.className='demo-data-button demo-data-button--quiet';dismiss.textContent='Close';dismiss.dataset.demoDataClose='';
    loading.append(message,dismiss);content.replaceChildren(loading);if(!dialog.open)dialog.showModal();dialog.setAttribute('aria-busy','true');
    try{
      const response=await fetch(url,{signal:pending.signal,credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'}});
      if(!response.ok || response.headers.get('X-Demo-Data-Fragment')!=='1')throw new Error('Unavailable');
      const html=await response.text();if(pending!==controller || !dialog.open)return;
      const panel=new DOMParser().parseFromString(html,'text/html').querySelector('[data-demo-data-panel]');if(!panel)throw new Error('Unavailable');
      content.replaceChildren(panel);dialog.scrollTop=0;content.querySelector('[data-demo-data-close]')?.focus();
    }catch(error){if(error.name==='AbortError'||pending!==controller)return;message.textContent='Could not load demo options. ';const fallback=document.createElement('a');url.searchParams.delete('fragment');fallback.href=url.href;fallback.dataset.nativeNavigation='';fallback.textContent='Open demo data';message.append(fallback);}
    finally{if(pending===controller)dialog.removeAttribute('aria-busy');}
  }
  listen(document,'click',event=>{
    if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
    const opener=event.target.closest('[data-demo-data-open]');if(opener){event.preventDefault();open(opener);return;}
    if(dialog.contains(event.target)&&event.target.closest('[data-demo-data-close]')){event.preventDefault();close();}
  });
  listen(document,'submit',event=>{
    const form=event.target.closest('[data-demo-data-form]');if(!form)return;
    if(form.dataset.submitted){event.preventDefault();return;}
    if(!form.reportValidity()){event.preventDefault();return;}
    form.dataset.submitted='true';form.setAttribute('aria-busy','true');
    const button=form.querySelector('[data-demo-data-submit]');button.disabled=true;button.textContent='Please wait…';
  });
  listen(dialog,'click',event=>{if(event.target===dialog)close();});
  listen(dialog,'cancel',event=>{event.preventDefault();close();});
  listen(document,'workspace:navigating',close);
  listen(window,'pageshow',()=>{document.querySelectorAll('[data-demo-data-form][data-submitted]').forEach(form=>{delete form.dataset.submitted;form.removeAttribute('aria-busy');const button=form.querySelector('[data-demo-data-submit]');button.disabled=false;button.textContent='Continue';});});
  lifecycle?.cleanup(()=>{controller?.abort();if(dialog.open)dialog.close();});
}, {persistent:true});
