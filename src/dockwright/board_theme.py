TOKENS = {
    "light": {"bg": "#ffffff", "bg-sub": "#f6f8fa", "bg-input": "#ffffff", "fg": "#1f2328", "muted": "#59636e",
              "border": "#d1d9e0", "control": "#818b98", "accent": "#0969da", "accent-bg": "#ddf4ff",
              "btn": "#0969da", "btn-hover": "#0860ca", "btn-fg": "#ffffff", "danger": "#d1242f",
              "danger-bg": "#ffebe9", "attn": "#9a6700", "attn-bg": "#fff8c5", "ok": "#1a7f37", "ok-bg": "#dafbe1",
              "focus": "#0969da"},
    "dark": {"bg": "#0d1117", "bg-sub": "#151b23", "bg-input": "#0d1117", "fg": "#f0f6fc", "muted": "#9198a1",
             "border": "#3d444d", "control": "#656c76", "accent": "#4493f8", "accent-bg": "#121d2f",
             "btn": "#1f6feb", "btn-hover": "#1a5fd0", "btn-fg": "#ffffff", "danger": "#f85149",
             "danger-bg": "#25171c", "attn": "#d29922", "attn-bg": "#272115", "ok": "#3fb950", "ok-bg": "#122117",
             "focus": "#1f6feb"},
}
TEXT_PAIRS = (("fg", "bg"), ("fg", "bg-sub"), ("fg", "border"), ("fg", "accent-bg"), ("fg", "attn-bg"),
              ("fg", "danger-bg"), ("muted", "bg"), ("muted", "bg-sub"), ("muted", "accent-bg"), ("accent", "bg"),
              ("accent", "bg-sub"), ("accent", "accent-bg"), ("btn-fg", "btn"), ("btn-fg", "btn-hover"),
              ("danger", "danger-bg"), ("attn", "attn-bg"), ("ok", "ok-bg"), ("muted", "bg-input"),
              ("accent", "attn-bg"), ("accent", "danger-bg"))
UI_PAIRS = (("control", "bg"), ("control", "bg-sub"), ("focus", "bg"), ("focus", "bg-sub"), ("focus", "accent-bg"),
            ("focus", "attn-bg"), ("focus", "danger-bg"))
MONO = 'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace'
RULES = "".join((
    "body{margin:0}",
    '.board{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",'
    '"Noto Sans",Helvetica,Arial,sans-serif}',
    ".board *,.board *::before,.board *::after{box-sizing:border-box}",
    ".board a{color:var(--accent)}",
    ".board :focus-visible{outline:2px solid var(--focus);outline-offset:2px}",
    ".vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}",
    ".skip{position:absolute;left:-9999px;top:8px;z-index:10;display:inline-flex;align-items:center;min-height:32px;"
    "padding:0 12px;border-radius:6px;background:var(--btn);color:var(--btn-fg)!important}",
    ".skip:focus{left:16px}",
    ".top{border-bottom:1px solid var(--border);background:var(--bg-sub)}",
    ".top-in{max-width:1280px;margin:0 auto;padding:0 24px;min-height:56px;display:flex;align-items:center;gap:24px;"
    "flex-wrap:wrap}",
    ".brand{font-weight:600;font-size:16px;color:var(--fg)}",
    ".nav{display:flex;gap:4px}",
    ".nav a{display:inline-flex;align-items:center;min-height:40px;padding:0 12px;border-radius:6px;color:var(--fg);"
    "text-decoration:none}",
    ".nav a:hover{background:var(--border)}",
    '.nav a[aria-current="page"]{font-weight:600;box-shadow:inset 0 -2px 0 var(--accent);border-radius:6px 6px 0 0}',
    ".top-end{margin-left:auto;display:flex;gap:16px;align-items:center;flex-wrap:wrap}",
    ".fresh{color:var(--muted);font-size:12px}",
    ".page{max-width:1280px;margin:0 auto;padding:24px;overflow-wrap:anywhere}",
    ".board h1{font-size:20px;line-height:1.3;margin:0;font-weight:600}",
    ".board h2{font-size:16px;line-height:1.4;margin:0 0 12px;font-weight:600;display:flex;align-items:center;gap:8px}",
    ".board h3{font-size:14px;margin:0 0 8px;font-weight:600}",
    ".head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:24px;flex-wrap:wrap}",
    ".sub{color:var(--muted);margin:4px 0 0}",
    ".state{font-weight:600;margin:8px 0 12px}",
    ".count{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;padding:0 6px;"
    "border-radius:999px;background:var(--border);color:var(--fg);font-size:12px;font-weight:600}",
    ".btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:32px;padding:5px 12px;"
    "border:1px solid var(--control);border-radius:6px;background:var(--bg-sub);color:var(--fg)!important;"
    "font:inherit;font-weight:500;text-decoration:none;cursor:pointer;white-space:nowrap}",
    ".btn:hover{background:var(--border)}",
    ".btn-primary{background:var(--btn);border-color:var(--btn);color:var(--btn-fg)!important}",
    ".btn-primary:hover{background:var(--btn-hover)}",
    ".board input,.board select,.board textarea{font:inherit;color:var(--fg);background:var(--bg-input);"
    "border:1px solid var(--control);border-radius:6px;min-height:32px;padding:5px 8px}",
    ".board textarea{width:100%;min-height:96px;resize:vertical;display:block}",
    ".board label,.board legend{font-weight:600;display:block;margin-bottom:4px}",
    ".field{margin-bottom:16px}",
    ".hint{color:var(--muted);font-size:12px;margin:4px 0 0}",
    ".toolbar{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px;margin-bottom:24px}",
    ".toolbar .field,.head .toolbar{margin:0}",
    ".toolbar label{font-size:12px;color:var(--muted);font-weight:500}",
    ".callout{border:1px solid var(--attn);background:var(--attn-bg);border-radius:6px;padding:12px 16px;"
    "margin-bottom:24px}",
    ".callout-danger{border-color:var(--danger);background:var(--danger-bg)}",
    ".callout ul{margin:0;padding-left:20px}",
    ".chip{display:inline-flex;align-items:center;min-height:22px;padding:0 8px;border-radius:999px;font-size:12px;"
    "font-weight:500;border:1px solid var(--border);color:var(--fg);white-space:nowrap;text-decoration:none}",
    ".chip-accent{background:var(--accent-bg);color:var(--accent);border-color:var(--accent)}",
    ".chip-attn{background:var(--attn-bg);color:var(--attn);border-color:var(--attn)}",
    ".chip-danger{background:var(--danger-bg);color:var(--danger);border-color:var(--danger)}",
    ".chip-ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok)}",
    ".chip-link{display:inline-flex;align-items:center;min-height:24px;text-decoration:none}",
    ".chip-link:hover .chip{text-decoration:underline}",
    ".board ::placeholder{color:var(--muted);opacity:1}",
    ".prio{display:inline-flex;align-items:center;justify-content:center;min-width:28px;height:22px;border-radius:4px;"
    f"font:600 12px/22px {MONO};border:1px solid var(--border)}}",
    ".p0{background:var(--danger-bg);color:var(--danger);border-color:var(--danger)}",
    ".p1{background:var(--attn-bg);color:var(--attn);border-color:var(--attn)}",
    ".p2{color:var(--fg)}",
    ".p3{color:var(--muted)}",
    ".section{margin-bottom:32px}",
    ".section details{margin-top:12px}",
    ".items{list-style:none;margin:0;padding:0;border:1px solid var(--border);border-radius:6px;overflow:hidden}",
    ".item{padding:8px 12px 12px;border-top:1px solid var(--border)}",
    ".item:first-child{border-top:0}",
    ".item-head{display:flex;gap:8px;align-items:center;padding:0 4px}",
    ".item-main{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 16px;align-items:center;padding:6px 4px;"
    "border-radius:6px;color:var(--fg)!important;text-decoration:none}",
    "a.item-main:hover{background:var(--bg-sub)}",
    "a.item-main:hover .item-cta{background:var(--border)}",
    "a.item-main:hover .btn-primary.item-cta{background:var(--btn-hover)}",
    ".item-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;grid-column:1}",
    ".item-text{margin:0;grid-column:1}",
    ".item-cta{grid-column:2;grid-row:1 / span 2}",
    ".item-hint{margin:4px 4px 0;color:var(--muted)}",
    ".key{display:inline-flex;align-items:center;min-height:24px;font-weight:600;color:var(--fg)!important;"
    "text-decoration:none}",
    "a.key:hover{text-decoration:underline}",
    ".age{color:var(--muted);font-size:12px}",
    ".muted{color:var(--muted)}",
    ".small{font-size:12px}",
    ".stack{display:flex;flex-direction:column;align-items:flex-start}",
    ".table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:6px;overflow-wrap:normal}",
    "table.grid{width:100%;border-collapse:separate;border-spacing:0}",
    ".grid th{background:var(--bg-sub);text-align:left;font-size:12px;font-weight:600;color:var(--muted);"
    "padding:8px 12px;border-bottom:1px solid var(--border);white-space:nowrap}",
    ".grid td{padding:8px 12px;border-top:1px solid var(--border);vertical-align:top}",
    ".grid tbody tr:first-child td{border-top:0}",
    ".grid tbody tr:hover{background:var(--bg-sub)}",
    ".grid td a{display:inline-flex;align-items:center;min-height:24px;min-width:24px}",
    ".grid td a.cell{display:flex;color:var(--fg);text-decoration:none;flex-direction:column;align-items:flex-start;"
    "justify-content:center}",
    ".grid .title{color:var(--muted);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
    "max-width:460px}",
    ".crumbs{font-size:12px;color:var(--muted);margin-bottom:8px;display:flex;gap:6px;align-items:center;"
    "flex-wrap:wrap}",
    ".crumbs a,.links a,.list a,.tl-head a,.fresh a,.ask-head a{display:inline-flex;align-items:center;min-height:24px;"
    "gap:4px}",
    ".facts{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0 8px}",
    ".links{display:flex;flex-wrap:wrap;gap:4px 24px;font-size:12px;color:var(--muted);align-items:center}",
    ".attention{border:1px solid var(--accent);background:var(--accent-bg);border-radius:6px;padding:16px;"
    "margin:24px 0 32px}",
    ".question{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:16px}",
    ".question+.question{margin-top:12px}",
    ".q-meta{font-size:12px;color:var(--muted);margin-bottom:4px}",
    ".q-text{margin:0 0 12px;white-space:pre-wrap;word-break:break-word}",
    ".form-row{display:flex;justify-content:flex-end;gap:8px;margin-top:8px}",
    ".inline-form{display:flex;gap:8px;align-items:flex-end}",
    ".inline-form .field{margin:0;flex-grow:1}",
    ".inline-form select{width:100%}",
    ".cols{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:32px;align-items:start;margin-top:24px}",
    ".side{grid-column:2;grid-row:1;position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;"
    "display:flex;flex-direction:column;gap:16px}",
    ".main{grid-column:1;grid-row:1;min-width:0}",
    ".card{border:1px solid var(--border);border-radius:6px;padding:16px;background:var(--bg)}",
    ".card h2{font-size:14px}",
    ".radios{border:0;padding:0;margin:12px 0 0}",
    ".radios label{font-weight:400;display:flex;align-items:center;gap:8px;min-height:28px;margin:0}",
    ".radios input{min-height:16px;margin:0}",
    ".prose{white-space:pre-wrap;word-break:break-word;max-width:72ch;margin:0}",
    f".mono{{font:12px/1.5 {MONO}}}",
    ".timeline{list-style:none;padding:0;margin:0;border-top:1px solid var(--border)}",
    ".timeline li{padding:12px 0;border-bottom:1px solid var(--border)}",
    ".tl-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px}",
    ".board details>summary{cursor:pointer;min-height:24px;display:list-item}",
    ".board pre{white-space:pre-wrap;word-break:break-word}",
    ".board details pre{margin:8px 0 0}",
    ".list{list-style:none;padding:0;margin:0}",
    ".list li{display:flex;gap:8px;align-items:center;min-height:32px;border-bottom:1px solid var(--border)}",
    ".sessions li{justify-content:space-between}",
    ".form-card{max-width:720px}",
    "@media (max-width:1011px){.cols{grid-template-columns:minmax(0,1fr);gap:24px}"
    ".side{grid-column:1;grid-row:auto;position:static;max-height:none;overflow:visible}"
    ".main{grid-column:1;grid-row:auto}}",
    "@media (max-width:543px){.top-in{padding:0 16px;gap:8px}.page{padding:16px}"
    ".top-end{width:100%;justify-content:space-between;padding-bottom:8px}"
    ".item-main{grid-template-columns:minmax(0,1fr)}.item-cta{grid-column:1;grid-row:auto;justify-self:start}"
    ".toolbar{flex-direction:column;align-items:stretch}}",
))


def _luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    red, green, blue = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def ratio(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _vars(theme: str) -> str:
    return ";".join(f"--{name}:{value}" for name, value in TOKENS[theme].items())


def css() -> str:
    return (f":root{{color-scheme:light dark;{_vars('light')}}}"
            f"@media (prefers-color-scheme: dark){{:root{{{_vars('dark')}}}}}{RULES}")
