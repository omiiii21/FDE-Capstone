# Design system: the operator console

One surface, one job. A support engineer opens this to answer a single question:
*what did the system decide about this ticket, and why.* Everything here serves
scanning that answer quickly and trusting it, in an office, on a laptop, forty
times a day.

This document is the authority. Where it and a component disagree, the component
is wrong.

---

## 1 The idea

**An instrument panel, not a dashboard.** The difference matters and it is the
whole direction.

A dashboard floats rounded cards on a neutral field and hopes the drop shadows
imply hierarchy. An instrument panel is a single machined surface divided by
hairlines: readings sit in cells, the cells share edges, and nothing floats.
Think of a mixing desk, an aircraft centre console, a Braun calculator. Density
is the point, not a compromise.

Three consequences that bind every decision below:

1. **Panels share edges.** Dividers are 1px, full-bleed, and structural. If two
   readings are adjacent, one line separates them; there is no gutter, no
   shadow, no radius between them.
2. **The instrument head is a solid colour field.** The page is light. One
   saturated block at the top carries the standing figures and the controls that
   change the system's state. That block is the only place colour is used at
   volume, and its weight is what stops the page reading as a document.
3. **Nothing floats.** No drop shadows anywhere. Elevation is expressed by
   surface colour and by hairlines, never by blur.

### What this is not

Recorded so the next person does not drift back into it:

- Not rounded cards on a grey field with a coloured accent bar.
- Not a big-number hero strip with four supporting stats beneath it.
- Not an indigo or violet gradient. The reference that prompted this direction
  was indigo; the structure was the point, the hue was not, and the project
  already owns a colour.
- Not an eyebrow above a heading. The heading carries itself.

---

## 2 Colour

The palette is one hue plus three semantics on a near-neutral ground. Restraint
is deliberate: in an Operate surface the accent means *this is actionable or
this is the current selection*, and a page that uses it decoratively has thrown
away its only strong signal.

### Tokens

```
/* ground and surfaces */
--field          #0c3f38   the instrument head, a deep teal
--field-2        #0a342e   a cell inside the head, one step down
--field-line     #1d5b51   a hairline on the head
--field-ink      #eef4f2   text on the head
--field-ink-dim  #9dc0b8   labels and secondary text on the head

--paper          #f2f4f1   the page behind the instrument
--surface        #ffffff   a cell in the body
--surface-2      #e9ede7   a cell header, an inset
--rule           #ccd4ca   the structural hairline
--rule-soft      #e0e5dd   a hairline inside a cell

/* text on light */
--ink            #0e1614   primary
--ink-2          #38443f   secondary
--muted          #59675f   labels and metadata

/* the one accent */
--accent         #0b5a4e
--accent-weak    #dceae6
--accent-ink     #ffffff

/* the three outcomes, and only these three */
--answered       #1c6430   --answered-field  #e2efe4   --answered-line  #b9d5bf
--escalated      #8c4a0c   --escalated-field #f8ecdd   --escalated-line #e6cfaf
--blocked        #8c2c1c   --blocked-field   #f9e4df   --blocked-line   #ecc4ba
```

### Rules

- **Every text and background pair clears 4.5:1**, computed rather than judged:

  | Pair | Ratio |
  |---|---|
  | `--muted` on `--surface-2` | 5.02:1 |
  | `--muted` on `--paper` | 5.38:1 |
  | `--escalated` on `--escalated-field` | 5.82:1 |
  | `--field-ink-dim` on `--field` | 5.98:1 |
  | `--answered` on `--answered-field` | 6.07:1 |
  | `--blocked` on `--blocked-field` | 6.90:1 |
  | `--accent` on `--surface` | 8.12:1 |
  | `--ink-2` on `--surface` | 10.16:1 |
  | `--field-ink` on `--field` | 10.57:1 |
  | `--ink` on `--surface` | 18.36:1 |

  `--muted` on `--surface-2` is the tightest at 5.02:1, so that is the pair to
  recompute if either token moves. `--field-line` sits at 1.50:1 on the field and
  is meant to: it is a hairline dividing two readings, not a control boundary or
  a state indicator, and a divider loud enough to pass 3:1 would fight the
  figures it separates. Any new pair is computed before it ships.
- Secondary text on the colour field is tinted from the field's own hue. Grey on
  a coloured surface is the tell that a palette was assembled rather than chosen.
- The three outcome colours are reserved. Answered, escalated, blocked. They
  never decorate anything else, because the moment they do they stop meaning
  anything.
- No gradients. Not on the field, not on a button, not behind text.

---

## 3 Type

**Two families, and each has a job it does not share.**

- **Fira Sans** for everything a person reads as language: headings, prose,
  labels, buttons, explanatory copy.
- **Fira Code** for everything a person reads as data: identifiers, scores,
  percentages, rule names, document ids, timestamps, code. Monospace here is not
  costume for "technical", it is what lets a column of figures line up and what
  makes `DOC-DEPLOY-002#1` legible as a string rather than a phrase.

### Scale

Fixed rem, not fluid. Product UI is read at a consistent distance and a
clamp-sized heading that shrinks inside a panel looks worse, not better.

```
--t-2xl  32px   the one page heading
--t-xl   24px   a reading on the instrument head
--t-lg   18px   a panel heading
--t-md   16px   body
--t-sm   14px   dense body, table cells
--t-xs   12px   labels, metadata
```

Nothing outside that list. Line height 1.5 for prose, 1.35 for dense cells.

### Treatments

- **Labels are Fira Code, 12px, uppercase, `letter-spacing: .1em`.** This is the
  instrument's voice and it is used consistently for every field label on the
  page.
- Headings: weight 600, `letter-spacing: -.01em`, `text-wrap: balance`. No
  tracking tighter than -.02em at these sizes.
- **`font-variant-numeric: tabular-nums` on every figure.** Not optional: the
  figures sit in columns and proportional digits make them jitter.
- Prose measure 65–75ch. Tables and dense cells may run wider.

---

## 4 Space and structure

A 4px base, used as 8 / 12 / 16 / 20 / 24 / 32. Nothing between.

```
--s-1 8px   --s-2 12px   --s-3 16px   --s-4 20px   --s-5 24px   --s-6 32px
```

### The grid

- The instrument head is one block, full width of the content column, square
  corners, no shadow, no outer border.
- Readings inside it are cells in a CSS grid with a **1px gap filled by
  `--field-line`**, so the dividers are the grid itself rather than borders on
  each child. The same technique is used for cell groups in the body with
  `--rule`.
- Body panels are `--surface` with a 1px `--rule` border and **radius 0**. A
  panel header is `--surface-2` with a 1px bottom rule.
- More space above a heading than below it: the heading belongs to what follows.

### Radius

`0` for panels, cells, the head and the body of anything structural. `3px` for
small controls only: chips, badges, inputs, buttons. Nothing is a pill.

---

## 5 Components

Every interactive element ships **default, hover, focus-visible, active,
disabled**, and where they apply **loading** and **error**. Half a set is not a
set.

| | Rule |
|---|---|
| Buttons | Square-ish (3px), 1px border, no shadow. Primary is `--accent` filled. Destructive is `--blocked` outlined, filled only on hover, because halting automation should take a deliberate second. |
| Chips | 12px Fira Code, 3px radius, 1px border, the semantic field as background. Used for rule identifiers, channel, tier, and state. |
| Rows | A selectable row shows selection with a 2px left edge in `--accent` and `--surface-2` behind it, not with a colour wash. |
| Inputs | 1px `--rule`, 3px radius, `--surface`. Focus is a 2px `--accent` ring offset 1px, never a removed outline. |
| Definition | Any term a non-engineer would not know carries `border-bottom: 1px dotted`. It opens on hover **and** focus, closes on Escape, and is wired with `aria-describedby`. |
| Tables | Header row is `--surface-2`, 12px uppercase Fira Code. Rows separated by `--rule-soft`. Figures right-aligned and tabular. |

### Icons

Lucide outline, inlined as SVG, 16px at `stroke-width: 1.5`, `stroke="currentColor"`,
`aria-hidden="true"`. One library, one weight, no exceptions. No emoji, ever, and
no unicode glyph standing in for an icon.

---

## 6 Motion

150–250ms, `cubic-bezier(.2, 0, 0, 1)`. Motion reports a state change and does
nothing else.

There is **one authored moment**: a ticket resolving into a decision. The verdict
band and the panels beneath it settle in, briefly, from an already-visible
resting state. Nothing else on the page animates on load, because an operator
loading a task does not want to watch it arrive.

Everything is wrapped in `@media (prefers-reduced-motion: reduce)`, which
collapses durations to 0 and renders the final state.

---

## 7 The surfaces the browser draws

These ship with defaults belonging to no design system, and theming them is the
cheapest signal that a page was built rather than assembled.

```css
::selection            { background: var(--accent-weak); color: var(--ink); }
:root                  { caret-color: var(--accent); accent-color: var(--accent); }
::-webkit-scrollbar     { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--rule); border-radius: 0; }
:focus-visible         { outline: 2px solid var(--accent); outline-offset: 1px; }
a                      { text-underline-offset: 2px; }
```

Plus `tabular-nums` wherever digits line up, and `scrollbar-color` for Firefox.

---

## 8 Responsive

Structural, not fluid. Type never scales with the viewport.

| Width | What changes |
|---|---|
| 1440 | Two columns in the work area, six readings across the head |
| 1024 | Two columns, three readings across |
| 768 | One column, the ticket rail moves above the work area, two readings across |
| 375 | One column throughout, readings stack in pairs, the head's controls wrap |

No horizontal page scroll at any width. A wide table gets its own
`overflow-x: auto` container. Touch targets 44×44px minimum.

---

## 9 First run

The walkthrough exists to reach one moment: **a ticket arrives and the system
shows its full reasoning and the rule that decided it.** Not to tour the
interface.

Four steps, skippable at every one and with Escape, replayable from a quiet
control, anchored to real elements, and it spotlights rather than blocking
behind a modal. It fires once, from `localStorage`, wrapped in try/catch, and
the page is perfect if that throws.

---

## 10 Writing

British English. The product's own words. A control names its action; an error
names the problem and the recovery.

**Every definition is written for CloudServe's head of support, who is not an
engineer**, and contains no jargon inside the definition of a jargon term. The
test: read it aloud to someone who has never seen the system. If they ask a
follow-up question, the definition has failed.
