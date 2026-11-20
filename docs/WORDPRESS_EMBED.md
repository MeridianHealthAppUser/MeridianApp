# The screening on the WordPress site

The practice's website (meridianhealth.co.za) stays on WordPress. The app's screening questionnaire (`https://app.meridianhealth.co.za/screening/`) can be shown inside a WordPress page in a frame, so the address bar keeps the WordPress address, such as `/screening/`, from the first question until the patient has paid. The test page is `/app-questionnaire-dev` (password protected).

## How it works

- **Only the screening can be framed.** The questionnaire, result, "Please call me" and checkout pages allow framing by the sites in `DJANGO_QUESTIONNAIRE_FRAME_ANCESTORS` (`Content-Security-Policy: frame-ancestors`). Every other page, including sign-in, the portal and staff pages, keeps `X-Frame-Options: DENY`.
- **Embed mode.** Inside a frame the app header, the back-to-home arrow and the Appearance switch are dropped. The page is always light, and its background is see-through, so it sits inside the WordPress page. Dropdowns and date fields use the browser's own pickers: ours open as a sheet at the bottom of the screen on phones, and in a frame that has grown to the full height of the form, the bottom of the frame is below the screen. Browsers mark every load inside a frame (`Sec-Fetch-Dest: iframe`), so each step stays embedded. `?embed=1` on the first address does the same. An ordinary visit to `/screening/` is unchanged.
- **Height and scroll.** `static/js/questionnaire_embed.js` tells the WordPress page its height, so the frame grows to fit and the page has one scroll bar. After each step it asks WordPress to scroll back to the top of the form. On the first load it doesn't, so the visitor stays where they are.
- **Leaving the frame.** After booking (and whenever a step would send an existing patient to the portal), the whole tab moves to the patient portal on app.meridianhealth.co.za. "Sign in to continue" and "Book from your patient portal" open in the whole tab too.
- **Cookies.** app.meridianhealth.co.za and www.meridianhealth.co.za are the same site, so the app's normal cookies work inside the frame on every browser, iPhones included. No setting is needed.
- **Refreshing.** Reloading the WordPress page reloads the frame's first address, so the patient sees an empty Step 1 again. What they already submitted is kept.

## The WordPress page

The page is built in the block editor.

1. Open the page and delete whatever is there (on the dev pages, the block with the redirect script).
2. Open **⋮ → Code editor**, paste the block below, switch back with **⋮ → Visual editor**, and click **Update**.

```html
<!-- wp:html -->
<iframe id="meridian-questionnaire"
  src="https://app.meridianhealth.co.za/screening/?embed=1"
  title="Medical assessment" allow="payment"
  style="display:block;width:100%;height:900px;border:0"></iframe>
<script data-no-optimize="1">
(function () {
  var APP = 'https://app.meridianhealth.co.za';
  var OFFSET = 110; // room for the site's sticky header when scrolling back to the form
  var frame = document.getElementById('meridian-questionnaire');
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (e.origin !== APP || !d || d.source !== 'meridian-questionnaire') return;
    if (typeof d.height === 'number') frame.style.height = Math.ceil(d.height) + 'px';
    if (typeof d.scrollTo === 'number') {
      var top = frame.getBoundingClientRect().top + window.pageYOffset + d.scrollTo - OFFSET;
      window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    }
    if (typeof d.navigate === 'string' && d.navigate.indexOf(APP + '/') === 0) window.location.href = d.navigate;
  });
})();
</script>
<!-- /wp:html -->
```

- `data-no-optimize="1"` keeps LiteSpeed Cache from delaying the script. If the frame stays 900px tall with its own scroll bar, check LiteSpeed's JS settings for this page.
- Raise or lower `OFFSET` if the sticky header covers the top of the form after a step.
- The `<script>` only survives if the person saving the page is an Administrator. WordPress strips it for Editors.

## App settings

`DJANGO_QUESTIONNAIRE_FRAME_ANCESTORS` (optional) lists the sites allowed to frame the screening, comma-separated. The default, `https://www.meridianhealth.co.za,https://meridianhealth.co.za`, covers the website, so production needs nothing set. An empty value turns framing off.

## Payment

The questionnaire checkout isn't connected to Peach yet; only the test code completes a booking. When it is, test the card form and the 3-D Secure bank step inside the frame on the Peach sandbox. If a bank's page refuses to load in a frame, the payment step has to open in the whole tab.

## Testing locally

Run the app with `DJANGO_QUESTIONNAIRE_FRAME_ANCESTORS=http://127.0.0.1:8011`. Save the block above as `index.html` with `src` and `APP` changed to `http://127.0.0.1:8000`, and serve it with `python3 -m http.server 8011 --bind 127.0.0.1`. Then open `http://127.0.0.1:8011/`. Both addresses count as the same site, so the default cookies work.
