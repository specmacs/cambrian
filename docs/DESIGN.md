# Design System — Agentic Trading Terminal

> Context file for Claude Code. Read this before writing any UI. Every color, size, and
> radius in the codebase must come from `tokens.css`. Never hardcode a hex value.

---

## 1. The one idea

This is not a dashboard. It is an **instrument** — a dense, low-chrome surface a person
stares at for eight hours without fatigue, where an autonomous agent is also acting.

That second part drives the whole system: **two actors touch this interface, and the user
must always know which one did a thing.** Provenance is not a feature we bolt on. It is a
visual primitive, encoded in a dedicated hue and a dedicated structural element.

Design rule that follows: **violet means agent. Nothing else is ever violet.** Not a
primary button, not a focus ring, not a chart line. When a trader sees violet, an agent
touched it. That constraint is worth more than any decorative flourish.

---

## 2. Palette

Cool blue-black base — an instrument housing, not a hacker terminal. Deliberately *not*
pure `#000`, which vibrates against white text and reads cheap.

| Token | Hex | Use |
|---|---|---|
| `--bg` | `#0B0E14` | App background, deepest layer |
| `--surface` | `#12161F` | Panels, table bodies |
| `--surface-raised` | `#1A1F2B` | Popovers, modals, hover rows |
| `--border` | `#232936` | Hairline dividers, panel edges |
| `--border-strong` | `#333B4D` | Focused/active panel edge |
| `--text` | `#E6E9EF` | Primary text, numbers |
| `--text-dim` | `#8B94A7` | Labels, column headers |
| `--text-faint` | `#5A6376` | Timestamps, disabled, units |

**Semantic — market direction**

| Token | Hex | Use |
|---|---|---|
| `--up` | `#2ED3A7` | Positive P&L, bid side, fills up |
| `--down` | `#FF6B7A` | Negative P&L, ask side, fills down |

Teal/coral instead of the standard green/red pair. Two reasons, both functional: it
survives deuteranopia far better than pure red-green, and it stops a busy blotter from
reading as a Christmas tree. Keep the pairing everywhere — never mix in a second green.

**Semantic — actor & state**

| Token | Hex | Use |
|---|---|---|
| `--agent` | `#8A7BFF` | Agent-originated anything. Reserved. |
| `--pending` | `#F2B544` | Working, partial fill, unconfirmed |
| `--focus` | `#4DA6FF` | Keyboard focus ring only |

Human-originated actions get **no** accent — they're the default, rendered in `--text`.
That asymmetry is the point: the agent is the thing that needs marking.

### Opacity ladder
Use these instead of inventing tints: `0.04` (row hover), `0.08` (selected row),
`0.16` (accent chip background), `0.4` (disabled).
E.g. an agent-filled row: `background: color-mix(in oklab, var(--agent) 8%, transparent)`.

---

## 3. Typography

Two families, three roles. No third family — density is achieved by scale, not variety.

- **UI / labels / prose → Geist Sans.** Tight apertures, near-neutral, engineered rather
  than friendly. Install: `npm i geist`.
- **All numerics → Commit Mono.** Every price, size, quantity, timestamp, symbol, and
  order ID. Free (OFL) from commitmono.com — self-host the woff2. Tabular by default,
  which is non-negotiable: columns of numbers must not shift width as digits change.
  Fallback chain: `"Commit Mono", "Geist Mono", ui-monospace, monospace`.

**Never** set a number in the sans face. If it can be compared vertically, it is mono.

### Scale
Compact by design. Base is 13px, not 16px — this is a professional tool, and the extra
rows are worth more than the extra comfort.

| Token | Size / line-height | Use |
|---|---|---|
| `--t-micro` | 10px / 14px, `0.08em` tracking, uppercase | Column headers, eyebrows |
| `--t-xs` | 11px / 16px | Timestamps, units, dense meta |
| `--t-sm` | 12px / 18px | Secondary UI, tooltips |
| `--t-base` | 13px / 20px | Table cells, body, default |
| `--t-md` | 15px / 22px | Panel titles, emphasized values |
| `--t-lg` | 20px / 26px | Section heads |
| `--t-xl` | 28px / 32px, `-0.02em` | Hero numbers only (equity, day P&L) |

Weights: 400 body, 500 UI/labels, 600 emphasis. Never 700 — at this density bold turns
to mud. Big numbers get their weight from size and tracking, not from `font-weight`.

---

## 4. Space & structure

**4px base unit.** Every margin, padding, and gap is a multiple: 4, 8, 12, 16, 24, 32.
Nothing else. If a value needs to be 10px, the layout is wrong.

**Row heights:** `28px` dense (blotter, order book), `32px` default, `40px` comfortable
(nav, headers). Pick one per surface and hold it — mixed row heights read as sloppy.

**Radii:** `--r-sm: 2px` (inputs, chips, cells), `--r-md: 4px` (panels, buttons,
modals). Nothing above 4px anywhere. This is the single biggest lever on "sharp."

**Elevation without shadow.** Do not use `box-shadow` for depth. Layers separate via
background lift plus a hairline border. The only permitted shadow is on true overlays
(modal, popover, command palette): `0 8px 32px rgba(0,0,0,0.5)`. Soft glows and colored
shadows are out.

**Borders are 1px, always.** Thicker borders read as toy UI. Emphasis comes from
`--border-strong`, not from more pixels.

### Layout shell

```
┌──┬──────────────────────────────────────────────────┬────────────────┐
│  │ TOPBAR  symbol · session · connection · equity   │                │
│  ├──────────────────────────┬───────────────────────┤   AGENT RAIL   │
│R │                          │                       │                │
│A │        CHART             │      ORDER BOOK       │  live reasoning│
│I │                          │                       │  trace, stream-│
│L │                          ├───────────────────────┤  ing, newest at│
│  │                          │      TICKET           │  top           │
│  ├──────────────────────────┴───────────────────────┤                │
│  │ BLOTTER  fills · working orders · positions      │  [ pause ] all │
└──┴──────────────────────────────────────────────────┴────────────────┘
  48px            fluid, resizable panes                    320px
```

Panes are resizable and hairline-divided — no gaps, no floating cards, no drop shadows
between panels. A terminal is one continuous surface subdivided, not a scatter of cards
on a background. This is the difference between "trading terminal" and "SaaS dashboard,"
and it's mostly achieved by removing gutters between panels.

The **agent rail** is persistent and always visible. An autonomous system you can't see
working is one you can't trust; hiding it behind a drawer would be a trust failure, not a
space saving.

---

## 5. Signature element — the provenance gutter

**This is the thing the product is remembered by. Build it first, and keep everything
else quiet around it.**

Every row that represents an event — a fill, an order, a position change, a log line —
carries a **2px left edge** in its first 2px of horizontal space:

| Edge | Meaning |
|---|---|
| transparent | Human-initiated. The default. |
| `--agent` solid | Agent-initiated, executed |
| `--agent` at 40% | Agent-proposed, awaiting human confirmation |
| `--pending` solid | In flight, unconfirmed by venue |

The gutter costs 2px, never moves, and lets a trader scan a hundred-row blotter and see
the agent's footprint without reading a word. Agent rows also expose a **reasoning
disclosure**: a hairline caret at the row's right edge that expands in place to show the
agent's stated rationale and the inputs it acted on. Expansion is in-place, never a modal
— pulling a trader out of context to read *why* defeats the purpose.

Do not add a second signature. No gradient meshes, no glow effects, no animated
backgrounds, no glassmorphism. The gutter is where the boldness is spent.

---

## 6. Motion

Motion here means *state legibility*, not delight. Budget is deliberately tiny.

- Transitions: `120ms ease-out`. Nothing slower — latency feels like lag in a trading UI.
- **Value flash:** when a number updates, flash its background `--up`/`--down` at 16%
  for 400ms, then decay. This is the one animation traders actually rely on.
- Agent rail entries fade + translate 4px on arrival, `160ms`. That's the full budget.
- No page transitions, no scroll-triggered reveals, no parallax, no skeleton shimmer —
  use a static hairline placeholder instead.
- Honor `prefers-reduced-motion: reduce`: keep the value flash (it's information), drop
  the translate.

---

## 7. Components

**Buttons.** `--r-sm`, 28px tall, 12px horizontal padding, `--t-sm` at weight 500.
- *Primary:* `--text` background, `--bg` text. Solid white-on-dark. Used sparingly.
- *Buy / Sell:* `--up` / `--down` at 16% background, full-strength text, 1px matching
  border. Never solid-filled — solid green/red buttons scream retail app.
- *Ghost:* transparent, `--border` outline, `--text-dim` text.
- Never a violet button. Violet is reserved.

**Inputs.** `--surface-raised` background, 1px `--border`, `--r-sm`, 28px tall, mono face
for any numeric field. Focus: border → `--focus`, plus a 1px outline offset by 1px. Focus
must be visible on every interactive element — traders keyboard through tickets.

**Tables.** No zebra striping. Separation comes from row hover (`--text` at 4%) and a
hairline under the header only. Headers use `--t-micro` uppercase in `--text-dim`. Right-
align every numeric column. Sticky header, always.

**Chips / tags.** 18px tall, `--r-sm`, `--t-xs`, accent at 16% background with full-
strength accent text.

**Command palette.** `⌘K`. This is a keyboard-first tool — every action reachable by
typing. Overlay on `--surface-raised`, the one place a shadow is allowed.

---

## 8. Voice

Terminal register: terse, literal, active. The interface states facts.

- Label by what the user controls: "Working orders," not "Order state manager."
- Actions keep their name through the flow: a **Send** button produces a **Sent** toast.
- Errors say what happened and what to do — `Rejected: insufficient margin. Reduce size
  or add collateral.` They never apologize and are never vague.
- Empty states are instructions, not mood: `No working orders. ⌘K to open a ticket.`
- Agent language is always attributed and hedged honestly: `Agent proposes: sell 200 @
  mkt` — never `Recommended for you`, never `AI-powered`.
- Sentence case everywhere except `--t-micro` labels. No exclamation marks. Ever.

---

## 9. Quality floor

Not optional, and not worth announcing in the UI:

- Every interactive element has a visible keyboard focus state.
- Full keyboard operation — a trader should be able to work without the mouse.
- `prefers-reduced-motion` respected per §6.
- Contrast: `--text` on `--surface` and both P&L colors on `--surface` clear WCAG AA.
  `--text-faint` is for non-essential meta only; never put required information in it.
- Never encode meaning in color alone. P&L carries a sign; agent rows carry the gutter
  *and* an attributed label.
- Layout is dense but must survive down to 1280px without horizontal scroll; below that,
  collapse the agent rail to an overlay.

---

## 10. Rejected on purpose

Listed so they don't creep back in during later sessions:

- Neon green on pure black — the terminal cliché, and it fails contrast at small sizes.
- Glassmorphism / blur panels — destroys number legibility, costs GPU on a page already
  rendering live charts.
- Cards floating on a background with gaps — reads SaaS dashboard, wastes vertical space.
- Rounded corners above 4px, gradient buttons, colored glows.
- Any second accent hue. The palette is closed. New meaning gets a new *shape*, not a new
  color.
