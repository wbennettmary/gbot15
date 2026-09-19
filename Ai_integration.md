You are working on an existing SaaS app for automated email placement testing / inbox intelligence.

## Goal

Integrate a new AI-managed automation workflow into the email test process.

The feature should allow the app to use an AI Agent, powered through OpenRouter free model access, to manage the full email testing cycle:

- select pushed templates
- select specific lists / workspace sources / user counts
- trigger the email test
- sync and read IMAP results
- classify users as Inbox, Spam, or Grey
- apply safe optimization actions to spam users
- retry the test after modifications
- update the final results
- display rich copy-ready lists in a new dedicated tab

The AI Agent should act as the orchestration layer, not as a replacement for core backend logic. The app backend must still own sending, syncing, updating users/domains, storing results, and enforcing safety rules.

---

## Core Feature Name

Implement the feature as:

**AI Placement Optimization**

or

**AI Test Manager**

Use the name that best matches the existing naming style in the codebase.

---

## Existing Concept

The current email test workflow has areas like:

1. Test Queue
2. Workspace Sources
3. Test Summary
4. Active & Completed Tests
5. User Inbox Analytics

The new AI system should connect to this workflow and add an intelligent automation layer.

---

## AI Provider

Use OpenRouter as the AI provider.

Default model/config key:

```env
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free