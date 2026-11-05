"""Light and dark mode: turn every colour in the app's stylesheets into a theme token, and write static/css/theme.css.

Each colour becomes a CSS custom property named after it (#087e75 is var(--c-087e75); rgba(7, 135, 127, .12) is
var(--c-07877f-12)). theme.css gives each token its light value, which is the colour as it was, so light mode looks
exactly as before, and a dark value worked out from it (dark_value): light surfaces and borders turn dark, dark text
turns light, and accents stay their hue, a little brighter. DARK_OVERRIDES sets a token's dark value by hand where the
worked-out one isn't right.

Run it after adding colours to a stylesheet:  .venv/bin/python scripts/theme_tokens.py
It changes the stylesheets in place and rewrites theme.css; running it again changes nothing. portal/test_theme.py
fails while any stylesheet still has a colour that isn't a token, so new styles follow the same rules.

Not themed: the public landing page and the video room (pages of their own, not on base.html), Django admin, and
emails, which always stay light.
"""

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / 'static' / 'css'
THEME = CSS / 'theme.css'
SKIP = {'theme.css', 'fonts.css', 'landing.css', 'video_room.css', 'meridian_admin.css'}

# Colours as they appear in a value: #rgb, #rrggbb, #rrggbbaa, rgb()/rgba(), and the white and black keywords.
COLOUR = re.compile(r'#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*[\d.]+%?\s*)?\)'
                    r'|(?<![-\w])(?:white|black)(?![-\w])')
COMMENT = re.compile(r'/\*.*?\*/', re.S)

# Dark values chosen by hand, by token name.
DARK_OVERRIDES = {
    'ffffff': '#172b36',   # cards, panels and inputs: the patient pages' dark surface
    'f4f8f9': '#101e27',   # the page behind them
    'f5f8f9': '#101e27',
    '1f2933': '#1f2933',   # text on a provider's own light form (Peach's card form), the same in both
    # The treatment timeline's four chips (treatment.css), fill then border: each keeps a hue of its own in dark mode.
    'f4eefb': '#30294f', 'e2d6f3': '#6153a3',   # Prescription: violet
    'ecf3fb': '#1b3358', 'd3e3f5': '#3f68ad',   # Shipment: blue
    'e9f5fa': '#11414d', 'cfe7f1': '#2c8398',   # Dosage: cyan
    'fbf4e6': '#3d2f13', 'f0e1bf': '#8c6c28',   # Payment: amber
    # A table row on hover: a step lighter than the dark card it sits on (workspace_polish, patient pages, leads).
    'f8fcfa': '#1f3541', 'f8fafb': '#1f3541', 'f6fafb': '#1f3541',
    # Light catching the edge of glass and buttons (fluid.css): still white in the dark, only fainter.
    'ffffff-600': 'rgba(255, 255, 255, 0.08)', 'ffffff-220': 'rgba(255, 255, 255, 0.18)',
}

# Patient pages choose their own light/dark semantic roles in patient_theme.css, and the
# console's main colours (console.css) use the same ones. Keep these palette values stable
# so the global conversion does not invert the explicit dark palette a second time.
PATIENT_PALETTE = (
    '101e27', '14384a', '172b36', '1d4a5e', '234255', '2b4655', '345363', '405f6e', '46728a', '4e899c',
    '506e7c', '557381', '8ccbdc', 'a5e3f0', 'aec7d3', 'b8ced8', 'c0d5df', 'c4eff8', 'd2e2e8', 'e3f1f6',
    'e5eef2', 'f3f8fa',
)
DARK_OVERRIDES.update({colour: f'#{colour}' for colour in PATIENT_PALETTE})

# The dark blues that buttons, links, icons and focus rings use on staff pages (they were the console's
# teals until Nov 2026). In dark mode they turn pale cyan, as the patient pages' #1d4a5e turns #a5e3f0.
BLUE_ACCENTS = (
    '0d2631', '0f2834', '10303e', '122933', '153a4a', '153a4b', '153b4b', '163341', '163746', '173c4c',
    '173d4d', '174051', '18333f', '183f50', '184153', '193d4e', '194051', '194254', '1a3f4f', '1a4051',
    '1a4356', '1a4457', '1b4355', '1b4457', '1b4557', '1c4354', '1c4557', '1d4a5f', '1e495d', '204252',
    '215065', '225368', '225369', '22556c', '23556b', '24556c', '25566c', '25586f', '2b424d', '446a7d',
)


def parse(text):
    """(r, g, b, alpha) from a colour as written."""
    text = text.strip().lower()
    if text == 'white':
        return 255, 255, 255, 1.0
    if text == 'black':
        return 0, 0, 0, 1.0
    if text.startswith('#'):
        digits = text[1:]
        if len(digits) == 3:
            digits = ''.join(ch * 2 for ch in digits)
        r, g, b = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
        alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
        return r, g, b, alpha
    parts = [part.strip() for part in text[text.index('(') + 1:-1].split(',')]
    r, g, b = (round(float(part)) for part in parts[:3])
    alpha = 1.0
    if len(parts) == 4:
        alpha = float(parts[3][:-1]) / 100 if parts[3].endswith('%') else float(parts[3])
    return r, g, b, alpha


def token_name(r, g, b, alpha):
    """'087e75', or with its opacity in thousandths for a see-through colour: rgba(7, 135, 127, .12) is '07877f-120'."""
    name = f'{r:02x}{g:02x}{b:02x}'
    return name if alpha >= 0.999 else f'{name}-{round(alpha * 1000):03d}'


def css_value(r, g, b, alpha):
    return f'#{r:02x}{g:02x}{b:02x}' if alpha >= 0.999 else f'rgba({r}, {g}, {b}, {round(alpha, 3):g})'


# sRGB <-> OKLab (Björn Ottosson's), for changing lightness without shifting hue.

def _linear(channel):
    channel /= 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _gamma(channel):
    channel = channel * 12.92 if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055
    return max(0, min(255, round(channel * 255)))


def to_oklch(r, g, b):
    r, g, b = _linear(r), _linear(g), _linear(b)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    lab_l = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    lab_a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    lab_b = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return lab_l, math.hypot(lab_a, lab_b), math.atan2(lab_b, lab_a)


def from_oklch(lab_l, chroma, hue):
    lab_a, lab_b = chroma * math.cos(hue), chroma * math.sin(hue)
    l = (lab_l + 0.3963377774 * lab_a + 0.2158037573 * lab_b) ** 3
    m = (lab_l - 0.1055613458 * lab_a - 0.0638541728 * lab_b) ** 3
    s = (lab_l - 0.0894841775 * lab_a - 1.2914855480 * lab_b) ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return _gamma(r), _gamma(g), _gamma(b)


# Light colours' lightness in dark mode, as (light, dark) points: white and near-white become the surfaces, and the pale
# greys and tints used for lines and highlights stay a step lighter than them, so borders still show.
LIGHT_TO_DARK = ((0.62, 0.52), (0.8, 0.41), (0.9, 0.35), (0.95, 0.31), (0.985, 0.24), (1.0, 0.21))

# The hue of the patient pages' dark blues (#172b36, #345363). Pale greys and pale blues take it in dark mode, so
# panels, lines, table headers and hovers match the blue surfaces instead of turning grey. Pale tints of other
# colours (amber, red, green, violet) keep their own hue.
SURFACE_HUE = math.radians(-126)


def _hue_gap(first, second):
    return abs((first - second + math.pi) % (2 * math.pi) - math.pi)


def _between(value, points):
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 <= value <= x2:
            return y1 + (y2 - y1) * (value - x1) / (x2 - x1)
    return points[-1][1] if value > points[-1][0] else points[0][1]


def dark_value(r, g, b, alpha):
    """The colour for dark mode. Light colours (surfaces, borders, tints) become dark ones with the same hue; dark
    colours (text) become light; mid colours (teal buttons, accents) keep their place, a little brighter. A dark
    see-through colour is a shadow, and stays one."""
    lightness, chroma, hue = to_oklch(r, g, b)
    if alpha < 0.5 and lightness < 0.45:
        return f'rgba(0, 0, 0, {round(min(alpha * 2, 0.6), 3):g})'
    if lightness > 0.62:
        new_l, new_c = _between(lightness, LIGHT_TO_DARK), chroma * 0.75
        if chroma < 0.009 or (chroma < 0.05 and _hue_gap(hue, SURFACE_HUE) < math.radians(40)):
            new_c, hue = max(new_c, 0.026 + max(0, new_l - 0.22) * 0.09), SURFACE_HUE
    elif lightness < 0.45:
        new_l, new_c = 0.93 - lightness * 0.62, chroma
    else:
        new_l, new_c = min(0.78, lightness + 0.08), chroma
    return css_value(*from_oklch(new_l, new_c, hue), alpha)


def accent_dark(r, g, b, alpha):
    """A BLUE_ACCENTS colour in dark mode: pale cyan, lighter for the darker (hover) blues, as #14384a turns #c4eff8."""
    lightness = to_oklch(r, g, b)[0]
    return css_value(*from_oklch(min(0.94, max(0.72, 0.88 + (0.387 - lightness) * 0.6)), 0.065, math.radians(212)), alpha)


def tokenise(text, found):
    """Replace each colour in a declaration with its token; selectors (an id like #add) and comments are left alone.
    found collects {name: (r, g, b, alpha)}."""
    def replace(match):
        rgba = parse(match.group(0))
        name = token_name(*rgba)
        found[name] = rgba
        return f'var(--c-{name})'

    comments = []

    def hide(match):
        comments.append(match.group(0))
        return f'\x00{len(comments) - 1}\x00'

    text = COMMENT.sub(hide, text)
    # Between braces: text followed by "{" is a selector or an @-rule; text followed by "}" is declarations.
    pieces = re.split(r'([{}])', text)
    for index in range(0, len(pieces), 2):
        following = pieces[index + 1] if index + 1 < len(pieces) else '}'
        if following == '}':
            pieces[index] = COLOUR.sub(replace, pieces[index])
    text = ''.join(pieces)
    return re.sub(r'\x00(\d+)\x00', lambda match: comments[int(match.group(1))], text)


def existing_tokens():
    """Tokens already in theme.css, so a colour that is only there (no longer in a stylesheet) keeps its token."""
    if not THEME.exists():
        return {}
    found = {}
    light = THEME.read_text().split(':root[data-theme="dark"]')[0]
    for name, value in re.findall(r'--c-([0-9a-f]{6}(?:-\d{3})?):\s*([^;]+);', light):
        found[name] = parse(value)
    return found


def write_theme(tokens):
    names = sorted(tokens)
    light = '\n'.join(f'  --c-{name}: {css_value(*tokens[name])};' for name in names)
    dark = '\n'.join(f'  --c-{name}: {DARK_OVERRIDES.get(name) or (accent_dark if name[:6] in BLUE_ACCENTS else dark_value)(*tokens[name])};'
                     for name in names)
    THEME.write_text(f'''/* Light and dark mode: every colour in the app's stylesheets, as a token. Written by scripts/theme_tokens.py:
   don't edit the token lists by hand; change the script's DARK_OVERRIDES and run it again.

   The theme follows the device's setting unless the person chose Light or Dark from their account menu
   (data-theme on <html>, saved in their browser). */

:root {{
  color-scheme: light;
{light}
}}

:root[data-theme="dark"] {{
  color-scheme: dark;
{dark}
}}

@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
{chr(10).join("  " + line for line in dark.splitlines())}
  }}
}}

/* The logo's dark lettering would disappear on a dark page: it is turned light, keeping its blues. */
:root[data-theme="dark"] .platform-logo {{ filter: invert(1) hue-rotate(180deg); }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) .platform-logo {{ filter: invert(1) hue-rotate(180deg); }} }}
''')


def main():
    tokens = existing_tokens()
    changed = []
    for path in sorted(CSS.glob('*.css')):
        if path.name in SKIP:
            continue
        text = path.read_text()
        new = tokenise(text, tokens)
        if new != text:
            path.write_text(new)
            changed.append(path.name)
    write_theme(tokens)
    print(f'{len(tokens)} colour tokens; changed {", ".join(changed) if changed else "no stylesheets"}.')


if __name__ == '__main__':
    sys.exit(main())
