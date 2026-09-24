## Summary
<!-- What does this PR do? One to three sentences. -->


## Type of Change
- [ ] feat: new feature
- [ ] fix: bug fix
- [ ] refactor: code change (no new feature/fix)
- [ ] docs: documentation only
- [ ] chore: tooling / config / dependencies
- [ ] security: security fix

## Policy Checklist (Software Development Policy v1.2)

### Core Rules
- [ ] R1 — Requirements understood and acceptance criteria defined before coding
- [ ] R2 — Architecture layers respected (no business logic mixed into controllers/DB)
- [ ] R3 — Code formatted, linted, and follows naming conventions
- [ ] R4 — No duplicate logic, functions are small and focused (DRY/SOLID)
- [ ] R5 — OWASP Top 10 considered; no injection, auth bypasses, or insecure defaults
- [ ] R6 — No hardcoded secrets, keys, passwords, or tokens
- [ ] R7 — Auth/authorization enforced on backend (not just frontend)
- [ ] R8 — Parameterized queries used; no raw SQL string concatenation
- [ ] R9 — No obvious performance issues; N+1 queries checked
- [ ] R10 — Correct HTTP methods/status codes; API response shape consistent
- [ ] R11 — Errors handled and logged; no silent catch blocks
- [ ] R12 — Logs contain meaningful events; no PII/secrets logged
- [ ] R13 — Unit, integration, and edge-case tests written and passing

### AI-Assisted Development (complete if AI tools were used)
- [ ] R19 — I have read, understood, and can explain every AI-generated line
- [ ] R20 — Every library/API the AI referenced was verified in the real registry
- [ ] R21 — SAST / dependency scan run on AI-generated code
- [ ] R22 — No real credentials or customer data were pasted into AI prompts
- [ ] R23 — AI output reviewed for correct architecture layering
- [ ] R24 — AI-generated tests verified to assert real behavior (not just happy-path stubs)

**AI Attribution (Rule 25):**
- [ ] This PR contains AI-generated code
  - Tools used: <!-- e.g., GitHub Copilot, ChatGPT, Claude, Kiro -->
  - Sections affected: <!-- e.g., auth middleware, database queries -->

## Testing Done
<!-- Describe what you tested and how -->


## Screenshots / Evidence (if applicable)
