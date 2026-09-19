# GBot Interface Direction

## Product Character

GBot is a dense operational tool. Interfaces should feel quiet, precise, and work-focused, with clear state feedback and fast scanning prioritized over decorative presentation.

## Visual System

- Use the existing Inbox Intelligence palette: slate text, white surfaces, blue primary actions, green inbox outcomes, amber waiting or mixed outcomes, and red spam or destructive actions.
- Use 8px or smaller corner radii for controls and framed surfaces.
- Keep typography compact inside panels: 12-13px supporting text, 17-20px section headings, and larger type only for primary metrics.
- Use existing `ii-*` components and spacing before adding new variants.
- Use Font Awesome icons already loaded by the application for compact action recognition.
- The AFRAID Process tab is a scoped claymorphism variant: raised operational panels, inset form fields, and strong blue selected states improve scanning without changing the shared Inbox Intelligence surfaces.

## Layout And Behavior

- Prefer unframed full-width sections separated by rules; reserve cards for repeated records or tightly scoped tools.
- Keep common actions visible beside the content they affect.
- Provide explicit empty, loading, error, disabled, and success states for asynchronous workflows.
- Preserve user selections across adjacent analytics views where possible.
- On narrow screens, collapse multi-column work areas to one column and allow data tables to scroll horizontally.

## Inbox Intelligence Analytics

- Green sections identify accounts and domains that can be kept or scaled.
- Amber sections identify mixed placement requiring Workspace identity changes.
- Red sections identify spam-only accounts requiring domain changes.
- Domain rankings sort by inbox rate first and observation volume second.
- Copy actions emit one normalized value per line, with `account,user` reserved for Workspace rename operations.
- Best-domain decisions use a compact table with page-scoped selection, bulk Inbox/Spam assignment, and visible saved-status totals.
- Legacy archives remain behind an explicit disclosure so the operational dashboard stays focused on current decisions.
