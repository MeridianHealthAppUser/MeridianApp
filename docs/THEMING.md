# Light and dark mode

Every colour in the app's stylesheets is a theme token: `#087e75` is written `var(--c-087e75)`. `static/css/theme.css`
gives each token two values:

- **Light**: the colour itself, so light mode looks exactly as designed.
- **Dark**: worked out from it by `scripts/theme_tokens.py`. Near-white surfaces become dark, pale greys and tints
  become lines and highlights a step lighter, dark text becomes light, and accents keep their hue, a little brighter.

The theme follows the device's setting. Anyone signed in can choose **Device**, **Light** or **Dark** under
Appearance in their account menu; the choice is saved in that browser (`static/js/theme.js`) and applied before the
page is drawn (`templates/base.html`).

## Adding or changing styles

Write colours as usual, then run:

```bash
.venv/bin/python scripts/theme_tokens.py
```

It turns new colours into tokens in place and rewrites `theme.css`. Running it again changes nothing.
`portal/test_theme.py` fails while a stylesheet still has a colour that isn't a token, so nothing slips through.

To change how a colour looks in dark mode, add it to `DARK_OVERRIDES` in the script (for example
`'ffffff': '#162427'`, the cards) and run it again. Don't edit the token lists in `theme.css` by hand.

## Not themed

Emails always stay light. The public landing page, the video room and Django admin have their own stylesheets
(`landing.css`, `video_room.css`, `meridian_admin.css`) and aren't themed. The Insights charts' series colours are set
in `portal/reporting_presentation.py`, and read well on both.
