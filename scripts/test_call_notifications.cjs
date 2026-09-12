const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.MERIDIAN_PLAYWRIGHT_PATH);

(async () => {
  const browser = await chromium.launch({channel:'chrome',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:320,height:700}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.setContent(fs.readFileSync(path.join(process.cwd(),'templates/includes/incoming_call.html'),'utf8'));
    await page.addStyleTag({path:'static/css/call_notifications.css'});
    await page.evaluate(() => {
      window.testSockets=[];
      window.WebSocket=class {
        constructor(url){this.url=url;this.sent=[];window.testSockets.push(this);queueMicrotask(()=>this.onopen?.({}));}
        send(value){this.sent.push(value);}
        close(code){this.onclose?.({code});}
      };
    });
    await page.addScriptTag({path:'static/js/call_notifications.js'});
    assert.equal(await page.locator('#incoming-call').isVisible(),false);
    await page.evaluate(() => testSockets[0].onmessage({data:JSON.stringify({type:'incoming_call',appointment_id:'javascript:alert(1)',caller_name:'X',practice_name:'Y'})}));
    assert.equal(await page.locator('#incoming-call').isVisible(),false);
    await page.evaluate(() => testSockets[0].onmessage({data:JSON.stringify({type:'incoming_call',appointment_id:42,caller_name:'<img src=x onerror=alert(1)>',practice_name:'Example practice',room_url:'https://attacker.test/'})}));
    assert.equal(await page.locator('#incoming-call').isVisible(),true);
    assert.equal(await page.locator('#incoming-call-join').getAttribute('href'),'/video/appointments/42/');
    assert.equal(await page.locator('#incoming-call img').count(),0);
    assert.match(await page.locator('#incoming-call-title').innerText(),/<img src=x/);
    const shape=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,heights:[...document.querySelectorAll('.incoming-call__actions > *')].map(el=>el.getBoundingClientRect().height)}));
    assert.ok(shape.scroll<=shape.width);
    assert.ok(shape.heights.every(height=>height>=44));
    await page.getByRole('button',{name:'Dismiss',exact:true}).click();
    await page.evaluate(() => testSockets[0].onmessage({data:JSON.stringify({type:'incoming_call',appointment_id:42,caller_name:'Repeat',practice_name:'Example practice'})}));
    assert.equal(await page.locator('#incoming-call').isVisible(),false);
    await page.evaluate(() => testSockets[0].onmessage({data:JSON.stringify({type:'incoming_call',appointment_id:43,caller_name:'Another call',practice_name:'Example practice'})}));
    assert.equal(await page.locator('#incoming-call').isVisible(),true);
    await page.evaluate(() => testSockets[0].onmessage({data:JSON.stringify({type:'call_cancelled',appointment_id:43})}));
    assert.equal(await page.locator('#incoming-call').isVisible(),false);
    await page.evaluate(() => testSockets[0].onmessage({data:JSON.stringify({type:'ping'})}));
    assert.equal(await page.evaluate(()=>testSockets[0].sent[0]),'{"type":"pong"}');
    await page.evaluate(() => testSockets[0].onclose({code:4001}));
    await page.waitForTimeout(1800);
    assert.equal(await page.evaluate(()=>testSockets.length),1);
    assert.deepEqual(errors,[]);
    console.log('Notification banner: safe text/URLs, deduplication, cancellation, terminal auth and 320px layout passed.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
