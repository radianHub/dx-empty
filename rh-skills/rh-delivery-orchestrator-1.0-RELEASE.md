---
name: rh-delivery-orchestrator
description: >
  Orchestrate the radianHub Agentic Dev + QA Loop in Claude Code. Entry point: the
  architect gives a Story Number (S-XXXXXX), a Story Subject, or an Epic/Sprint name,
  plus a target org alias. Verifies the rh-prod-sobject-all connector, pulls stories
  and acceptance criteria live from Salesforce, runs the dev loop then the QA loop
  across all stories, and routes every failure to a human with a retry-or-intervene
  choice. Production deploy is always human-gated. Use when asked to "run the dev + QA
  loop", "build the story", or process a story/epic/sprint against an org.
---

# radianHub Agentic Dev + QA Loop — Orchestrator

This skill is the single entry point. The rh-developer and rh-qa skill instructions
are embedded below in their respective sections. This file is self-contained — no
external skill files are loaded at runtime.

Shared reference material is embedded at the end of this file under:
- **REFERENCE: Field Mapping** — how each build variable is sourced from Story__c / Acceptance_Criteria__c
- **REFERENCE: Audit CSV Schema** — audit CSV column schema and retry-trail format
- **REFERENCE: Existence Checks** — component-existence SOQL for dev collision detection and QA presence checks
- **REFERENCE: Flow Static Checks** — deterministic flow gate; read when any story produces a flow
- **REFERENCE: Test Persona Provisioning** — resolving who a screen flow test runs as

Read a reference section only when the current phase needs it.

---

## Entry Point

Invoked directly in a Claude Code conversation, e.g.:
> "Run the dev + QA loop for S-001042 against rhapsody-scratch."
> "Run the dev + QA loop for all stories in the 'Checkout Redesign' epic against rhapsody-scratch."

The architect must supply, at minimum:
1. **A story identifier** — a Story Number (`S-\d+`, the `Name` field on `Story__c`), a Story Subject (matched against `Subject__c`), or an Epic/Sprint name for a batch run.
2. **A target org alias** (`target_environment`) — always supplied by the architect via chat/CLI, never inferred from the record.

If either is missing, ask before proceeding — do not guess an org alias.

---

## Prerequisites (confirm before starting)

- [ ] **rh-prod-sobject-all connector is enabled and authenticated** — verify below
- [ ] Architect authenticated to the deploy org: `sf org list` shows `target_environment` connected
- [ ] Repo clean: `git status` shows no uncommitted changes
- [ ] `CLAUDE.md` exists in repo root with data/security policies and the `api_version` value (create if missing per org instructions)
- [ ] `force-app/main/default/` exists as the metadata source root
- [ ] Python 3 available — required for flow static checks
- [ ] **Resolve `skills_root`** — the directory holding the rh-* skill folders. Project install: `.claude/skills`. Personal install: `~/.claude/skills`. Confirm the static check script is reachable before Phase 1:
  ```bash
  ls [skills_root]/rh-delivery-orchestrator/scripts/flow_static_check.py
  ```

Screen flow stories only (check after Phase 0 resolves scope, not before):

- [ ] Node 18+ and `@playwright/test` installed, Chromium bundle present
- [ ] `tests/e2e/` exists (create with `playwright.config.ts` per the Playwright Spec template embedded in the rh-developer section below)

Missing browser tooling **degrades, it does not stop the run**: static checks still gate,
and browser-dependent ACs become `needs human eyes: browser tooling unavailable`. Never
install browsers mid-run without asking.

### Verifying the connector

```
CALL rh-prod-sobject-all:getUserInfo
```

- On auth/credential failure: **do not proceed.** Tell the architect the `rh-prod-sobject-all` connector needs reconnecting in Settings → Connectors, and name it.
- On success: note the returned username/org for the audit log. This is the *data* org, which may differ from `target_environment`, the *deploy* org.

---

## Phase 0 — Story Resolution & Dependency Sequencing

All story data is pulled live from Salesforce via `rh-prod-sobject-all`. There is no
CSV. For the full variable → Salesforce-field mapping, see **REFERENCE: Field Mapping** below.

### Step 1 — Resolve the input to one or more Story__c records

**Story Number pattern (`S-\d+`):**
```sql
SELECT Id, Name, Subject__c, Status__c, Priority__c, Project__c, Epic__c,
       Description__c, Technical_Requirements__c,
       Pre_Deployment_Steps__c, Post_Deployment_Steps__c,
       Predecessor_Story__c, Complexity_Risk__c
FROM Story__c
WHERE Name = 'S-XXXXXX'
```
Exactly one record expected. If none, tell the architect and stop.

**Free text (a Story Subject):**
```sql
SELECT Id, Name, Subject__c, Status__c, Epic__c
FROM Story__c
WHERE Subject__c LIKE '%{search text}%'
```
Zero → stop and report. One → single-story run. Multiple → present the list (Number + Subject + Status) and ask the architect to pick one or confirm a batch.

**Epic or Sprint (batch run):**
- Epic is a direct lookup on `Story__c` — resolve the Epic record by name/subject, then query `WHERE Epic__c = '{epic_id}'`.
- Sprint membership goes through the `Sprint_Story__c` junction. Call `getObjectSchema` on `Sprint_Story__c` first to confirm field names, then query Stories related to the Sprint through the junction.

### Step 2 — Pull full field detail for every resolved Story

Retrieve every field in the mapping (see **REFERENCE: Field Mapping**). If
`Technical_Requirements__c` and `Description__c` are both blank or clearly lack
build-level detail (no metadata component names, formula text, or error strings),
**do not guess** — ask: "S-XXXXXX has no structured technical detail in
Description/Technical Requirements. What should I treat as the implementation spec?"

### Step 3 — Retrieve Acceptance Criteria

```sql
SELECT Id, Name, As_A__c, I_Want_To__c, So_That__c, Steps__c, Status__c, Order__c
FROM Acceptance_Criteria__c
WHERE Story__c = '{story_id}'
ORDER BY Order__c
```

- One or more records → source of truth for QA (`Steps__c` + the As-A/I-Want/So-That statement).
- Zero records → fall back to `Description__c` / `Technical_Requirements__c` for embedded criteria (Given/When/Then or checklist block).
- Neither → stop and ask: "S-XXXXXX has no Acceptance Criteria records and nothing identifiable in Description/Technical Requirements. What should QA treat as pass/fail criteria?" Do not fabricate criteria.

**Regardless of source**, assign each AC a sequential label — `AC-1`, `AC-2`, … — in
read order before handing off to QA. This is the only identifier QA uses; QA never
re-derives labels from `Name` or `Order__c`. Record the source
(`Acceptance_Criteria__c records` / `Description or Technical Requirements fallback` /
`architect-provided`) as the `acceptance_criteria_source` audit column.

### Step 3b — Screen flow resolution (only if a story produces a flow)

Decide `flow_type` from `Technical_Requirements__c`: `screen` if it describes screens,
user input, or a guided experience; `autolaunched` if it describes a record trigger or
schedule. **If the story does not make this clear, ask.** The two follow completely
different QA paths, so a wrong guess wastes a full build-and-verify cycle.

When `flow_type` is `screen`, also resolve:

- **`flow_entry_point`** — how a user actually reaches the flow. Values and what each additionally needs are in **REFERENCE: Field Mapping** below. Unresolvable → ask.
- **`test_persona_username`** — the user whose session runs the browser test, mapped from `Acceptance_Criteria__c.As_A__c`. **Never the deploy admin.** Resolution order, the create-a-user gate, and guest-site handling are in **REFERENCE: Test Persona Provisioning** below.

### Step 4 — Dependency Sequencing

`Predecessor_Story__c` is a single lookup (one predecessor per Story):

- If populated and the predecessor is **in scope**, it deploys first.
- If **not in scope**, note it as an external dependency, assume satisfied, and flag that assumption to the architect.
- Also cross-reference `Technical_Requirements__c` / `Pre_Deployment_Steps__c` text across in-scope stories — a story can implicitly depend on another's metadata without a `Predecessor_Story__c` link.

Print the resolved order before starting:
```
Execution order resolved:
  1. S-001042 — [Subject__c]
  2. S-001043 — [Subject__c]
  ...
```

### Step 5 — Capture the regression baseline

**Before the first deploy**, capture up to 5 existing record Ids per in-scope object and
store them in `build-state.json` under `regression_baseline`. Queries and interpretation
rules are in **REFERENCE: Existence Checks** below.

This must happen now, not in Phase 3. Once QA runs, the newest records in the org are
the ones QA created, so there is no way to find the pre-existing set after the fact. An
empty baseline is a valid outcome in a fresh org and gets reported as `N/A`, never as a
pass.

### Initialize the audit CSV

`force-app/../qa/[batch]_QA_Audit.csv` — one CSV for the whole run, the primary
human-review artifact (no writeback to Salesforce). Schema and retry-trail format are in
**REFERENCE: Audit CSV Schema** below. Keep every row current as the run progresses, not
just at the end.

---

## Phase 1 — Dev Loop (all stories, sequenced)

Follow the **rh-developer** instructions embedded below for this phase.

For each story in sequenced order:

```
PRINT: "▶ Building S-[Number]: [Subject__c]"

STEP 1 — Pre-deployment checks (run as written in Pre_Deployment_Steps__c):
  If a check fails: PAUSE → surface to human → retry or intervene

STEP 2 — Generate metadata (per rh-developer instructions below):
  Write all files to force-app/main/default/[correct subdirectory]/
  Formula logic and error message text used VERBATIM — never paraphrased
  Screen flows: also write tests/e2e/[FlowApiName].spec.ts

STEP 3 — Deploy:
  sf project deploy start --source-dir force-app --target-org [target_environment]
  Capture result (success / partial / failed)
  IF deploy failed:
    PRINT deploy error output in full
    PAUSE → present to human → retry or intervene
    On retry: fix metadata files, redeploy
    On intervene: mark INTERVENED, log in audit, continue to next story
    (batch does not resume until the architect explicitly says "continue")

STEP 4 — Log deploy result:
  PRINT: "✅ S-[Number] deployed" or "❌ S-[Number] deploy failed — [reason]"
  Update the story's audit row (deploy status and flow_type only — QA filled in Phase 2)

CONTINUE to next story
```

The e2e spec is source, not a build artifact. It deploys nowhere and is committed with
the metadata so the suite grows with the project.

When all stories are deployed/skipped/intervened:
```
PRINT: "Dev loop complete. [N] deployed, [N] skipped, [N] intervened."
PRINT: "Handing off to QA loop."
```

---

## Phase 2 — QA Loop (deployed stories only)

Follow the **rh-qa** instructions embedded below for this phase.

QA receives: the deployed story list, their pre-numbered ACs from Phase 0, each
story's `files_written` and `flow_type` (from the dev skill's `PRODUCED` contract, for
the component presence check — QA does not re-derive expected components),
`flow_entry_point`, `test_persona_username`, `api_version`, and the target org alias.

```
PRINT: "🔍 QA: S-[Number]: [Subject__c]"

  Execute QA per rh-qa instructions below — tested literally against the live sandbox,
  not static file analysis. Each AC row = confirmed | needs human eyes.

  IF all AC rows confirmed:
    PRINT: "✅ QA passed: S-[Number]"
    Update audit row: qa_status=PASS
    Optionally set Acceptance_Criteria__c.Status__c = "Approved" per confirmed row

  IF any AC row needs human eyes:
    PRINT the QA status report in full
    PAUSE → ask: "S-[Number] QA failed. Retry (one fix-and-reverify pass) or intervene?"

    IF retry:
      Re-run dev instructions for this story only (one pass) → redeploy → re-run QA
      Before overwriting ac_results/qa_status, append the outgoing attempt to
      retry_history and increment attempt_count (see REFERENCE: Audit CSV Schema below)
      IF passes: PRINT "✅ S-[Number] passed on retry" → update → continue
      IF fails again: PAUSE → ask again (retry or intervene)
      [No autonomous retry budget — every failure asks the human]

    IF intervene:
      Mark INTERVENED → PRINT "⚠️ S-[Number] handed to architect. Batch paused."
      WAIT for explicit "continue" before the next story

CONTINUE to next story
```

A persona-permission failure on a screen flow is a real defect. Never resolve it by
re-running as the deploy admin to get a green result.

---

## Phase 3 — Sprint-Level Gate

Run after all stories pass QA (or are dispositioned). Per the sprint gate section of
the rh-qa instructions below:

- **Re-query the Phase 0 `regression_baseline` Ids** for each object touched, confirming
  no new validation errors, no broken formulas, no nulled values. Report the actual count
  checked. An empty baseline reports `N/A — no pre-existing records at run start`.
- **Run the full committed e2e suite**, not only this batch's specs:
  ```bash
  RH_TARGET_ORG=[target_environment] npx playwright test
  ```
  Skip with a stated reason if browser tooling is unavailable.
- Verify no existing automation was broken by the deployed changes
- Confirm `Post_Deployment_Steps__c` executed correctly for every story
- Confirm QA-created records were torn down; list any leaked Ids

```
IF regression passes:
  PRINT: "✅ Sprint gate passed. Ready for production review."
  PRINT end-of-run summary
IF regression flags anything:
  PAUSE → present to human → retry or intervene (same model as per-story failures)
```

---

## Phase 4 — Production Boundary (human-only)

**The agentic system's responsibility ends at "sandbox-verified and ready."**

```
🚦 PRODUCTION DEPLOY — HUMAN REQUIRED
================================
All stories sandbox-verified. Do NOT deploy to production from this session.
Production deploy requires:
  - Senior developer or architect sign-off
  - Deploy via change set or: sf project deploy start --target-org [prod-alias]
  - Immediate post-deploy spot-checks
  - Team comms before end of day
================================
```

No production deploy command is issued by the agent under any circumstance.

---

## End-of-Run Summary

```
## Agentic Dev + QA Loop — Complete
**Story Input:** [Story Number / Subject / Epic used to resolve scope]
**Salesforce Org (data source):** [org returned by getUserInfo]
**Target Deploy Org:** [target_environment]
**API Version:** [api_version]
**Run completed:** [timestamp]

| Status | Count |
|---|---|
| ✅ QA Passed | [N] |
| 🔁 Passed on retry | [N] |
| ⚠️  Intervened | [N] |
| ⏭️  Skipped | [N] |
| ❌ Deploy failed | [N] |

### Stories Requiring Human Review:
[Each INTERVENED story: Number, Subject, reason]

### Run Artifacts For Architect Review:
[Test personas created this run, if any — these persist in the org]
[Leaked QA test records, if teardown failed]
[Static check WARN findings that were not blocking]

Full per-story results: force-app/../qa/[batch]_QA_Audit.csv
Browser evidence: qa/evidence/
```

---

## Rollback / Escalation

If a deployed story misbehaves in sandbox: deactivate the specific component in Setup
and file a bug story directly in Salesforce (new `Story__c`). No self-healing or
agent-initiated rollback. The bug story re-enters the system like any other — the
architect gives its Number/Subject to a future run.

---

## State File

`build-state.json` at repo root, written at run start and updated after each story,
keyed by Story Id (not row) so it survives Subject edits. On interruption, re-running
the same entry command lets the orchestrator skip already-completed stories.

```json
{
  "story_input": "Checkout Redesign epic",
  "target_org": "rhapsody-scratch",
  "api_version": "67.0",
  "started_at": "2026-07-14T14:00:00Z",
  "regression_baseline": {
    "Gate__c": ["a0X1a000000AAA1AAA", "a0X1a000000AAA2AAA"],
    "Payment__c": []
  },
  "test_personas_created": ["rhqa.S-001043@rhapsody.invalid"],
  "stories": {
    "a0X1a000000ABCDEFA": { "name": "S-001042", "deploy": "PASS", "qa": "PASS", "timestamp": "..." },
    "a0X1a000000ABCDEFB": { "name": "S-001043", "deploy": "PASS", "qa": "INTERVENED", "timestamp": "..." },
    "a0X1a000000ABCDEFC": { "name": "S-001044", "deploy": "PENDING", "qa": "PENDING" }
  }
}
```

`regression_baseline` with an empty array means the object had no records at run start.
That is recorded deliberately so Phase 3 reports `N/A` rather than inventing a pass.

---

# rh-developer — Dev Instructions

Runs in Phase 1 of the orchestrator. The orchestrator has already resolved every
value below from Salesforce — there is no CSV and no mapping to re-derive here.

## Purpose

Receive one resolved story. Produce all required metadata files for it in the correct
`force-app/main/default/` locations. Return the `PRODUCED` (or `BLOCKED`) contract to
the orchestrator.

## The verbatim rule

**Formula logic and error message text are copied exactly from the story's
`Technical_Requirements__c` / `Description__c` content — never paraphrased, inferred, or
approximated.** `Technical_Requirements__c` is a single rich-text blob, not pre-split
fields. When you need formula/condition text or error message text:

- Locate the exact text and copy it verbatim.
- If the field does not clearly delineate where formula logic ends and error message
  text begins, **do not guess the boundary** → return `BLOCKED`.
- If `Technical_Requirements__c` is blank and `Description__c` lacks build-level detail
  → return `BLOCKED`.

## Resolved inputs (handed in by the orchestrator)

`story_number`, `story_record_id`, `subject`, `object`, `metadata_components`,
`implementation_detail`, `formula_logic`, `error_message_text`,
`pre_deployment_steps` (already run — reference only), `post_deployment_steps` (written
to the post-deploy checklist, not executed here), `acceptance_criteria` (reference only
— QA is the primary consumer), `target_environment` (org alias for deploy context),
`api_version` (never hardcode a version in a file), `skills_root` (where the
rh-* skill folders live, for invoking the static check script).

Flow stories also receive `flow_type` (`screen` or `autolaunched`), and screen flows
additionally receive `flow_entry_point`, `test_persona_username`, and
`experience_site_url` where applicable. These are already resolved — if `flow_type` is
absent for a flow story, return `BLOCKED` rather than inferring it.

`story_record_id` is for the orchestrator's audit/build-state only — never write raw
Salesforce Ids into metadata file content.

## Pre-Generation: Collision Detection

Before writing any file, confirm no component of the same API name already exists in
the target org. Use the shared queries in **REFERENCE: Existence Checks** below. On a
hit: surface to the orchestrator with details (→ human). Never overwrite silently.

## File Output Conventions (SFDX source format)

All paths relative to repo root.

| Metadata Type | Directory | File Pattern |
|---|---|---|
| Custom Object | `objects/[Object__c]/` | `[Object__c].object-meta.xml` |
| Custom Field | `objects/[Object__c]/fields/` | `[Field__c].field-meta.xml` |
| Validation Rule | `objects/[Object__c]/validationRules/` | `[Rule].validationRule-meta.xml` |
| Screen Flow | `flows/` | `[FlowAPIName].flow-meta.xml` |
| Record-Triggered / Autolaunched Flow | `flows/` | `[FlowAPIName].flow-meta.xml` |
| Screen flow e2e spec | `tests/e2e/` | `[FlowAPIName].spec.ts` |
| Apex Class | `classes/` | `[ClassName].cls` + `.cls-meta.xml` |
| Apex Trigger | `triggers/` | `[TriggerName].trigger` + `.trigger-meta.xml` |
| Permission Set | `permissionsets/` | `[Name].permissionset-meta.xml` |
| Custom Metadata Type | `customMetadata/` | `[Type].[Record].md-meta.xml` |
| Custom Setting | `customSettings/` | `[Name].customSetting-meta.xml` |
| Layout | `layouts/` | `[Object__c]-[LayoutName].layout-meta.xml` |
| Field on existing layout | `layouts/` | Edit existing layout file — add field reference |

All metadata directories are under `force-app/main/default/`. **`tests/e2e/` is at repo
root, not under `force-app`** — it is test source, not deployable metadata.

**Deploy command (always):**
```bash
sf project deploy start --source-dir force-app --target-org [target_environment]
```
Never use `--metadata` scoped deploy. Never use the declarative Setup UI.

## Templates

### Custom Field
```xml
<?xml version="1.0" encoding="UTF-8"?>
<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>[FieldName__c]</fullName>
    <label>[Label from implementation_detail]</label>
    <type>[Text|Picklist|Number|Date|DateTime|Checkbox|Lookup|MasterDetail|Currency|Percent|Email|Phone|URL|TextArea|LongTextArea|Formula]</type>
    <required>[true|false]</required>
    <description>[story_number] — [subject]</description>
    <!-- Type-specific elements — include only what applies -->
    <!-- Text: <length>255</length> -->
    <!-- Picklist: <valueSet><valueSetDefinition><sorted>false</sorted><value><fullName>Val</fullName><default>false</default><label>Val</label></value></valueSetDefinition></valueSet> -->
    <!-- Number: <precision>18</precision><scale>0</scale> -->
    <!-- Formula: <formula>[verbatim formula excerpt from Technical_Requirements__c]</formula><formulaTreatBlanksAs>BlankAsZero</formulaTreatBlanksAs> -->
    <!-- Lookup: <referenceTo>[Object]</referenceTo><relationshipLabel>[Label]</relationshipLabel><relationshipName>[Name]</relationshipName> -->
</CustomField>
```
`description` is always `[story_number] — [subject]`. Formula fields use the **verbatim** formula excerpt from `Technical_Requirements__c`.

### Validation Rule
```xml
<?xml version="1.0" encoding="UTF-8"?>
<ValidationRule xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>[RuleName]</fullName>
    <active>true</active>
    <description>[story_number] — [subject]</description>
    <errorConditionFormula>[VERBATIM formula excerpt from Technical_Requirements__c]</errorConditionFormula>
    <errorDisplayField>[FieldAPIName, or leave blank for page-level]</errorDisplayField>
    <errorMessage>[VERBATIM error message excerpt from Technical_Requirements__c/Description__c]</errorMessage>
</ValidationRule>
```
Both formula and error message are copied **exactly** from the story. If the boundary between the two isn't clear in `Technical_Requirements__c`, return `BLOCKED`.

### Record-Triggered Flow
`processType` is `AutoLaunchedFlow` with a `triggerType`. No screens permitted.
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>[api_version]</apiVersion>
    <description>[story_number] — [subject]</description>
    <label>[Flow Label from implementation_detail]</label>
    <interviewLabel>[Flow Label] {!$Flow.CurrentDateTime}</interviewLabel>
    <processType>AutoLaunchedFlow</processType>
    <status>Active</status>
    <start>
        <object>[ObjectAPIName]</object>
        <triggerType>[RecordBeforeSave|RecordAfterSave]</triggerType>
        <recordTriggerType>[Create|Update|CreateAndUpdate|Delete]</recordTriggerType>
        <filterLogic>[and|or|custom]</filterLogic>
        <filters>
            <field>[FieldAPIName]</field>
            <operator>[EqualTo|NotEqualTo|IsNull|GreaterThan|LessThan]</operator>
            <value><stringValue>[value]</stringValue></value>
        </filters>
        <connector><targetReference>[FirstElementName]</targetReference></connector>
    </start>
    <!-- Elements, connectors, variables per implementation_detail -->
    <!-- Use verbatim formula/condition text from Technical_Requirements__c -->
</Flow>
```
Non-negotiables: `apiVersion` is `[api_version]`; `description` is `[story_number] — [subject]`; `status` is `Active`; every DML/Get Records element carries a `faultConnector`; no DML inside a loop body; every `decisions` element has a `defaultConnector`; prefer `RecordBeforeSave` for same-record field updates.

Run the static checker before returning PRODUCED:
```bash
python3 [skills_root]/rh-delivery-orchestrator/scripts/flow_static_check.py \
  force-app/main/default/flows/[FlowApiName].flow-meta.xml \
  --api-version [api_version] --story [story_number]
```

### Screen Flow
`processType` is `Flow`. No `triggerType`, no `<start>` filters. Has screens.
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>[api_version]</apiVersion>
    <description>[story_number] — [subject]</description>
    <label>[Flow Label from implementation_detail]</label>
    <interviewLabel>[Flow Label] {!$Flow.CurrentDateTime}</interviewLabel>
    <processType>Flow</processType>
    <status>Active</status>
    <runInMode>DefaultMode</runInMode>
    <start>
        <connector><targetReference>[FirstScreenName]</targetReference></connector>
    </start>
    <variables>
        <name>recordId</name>
        <dataType>String</dataType>
        <isCollection>false</isCollection>
        <isInput>true</isInput>
        <isOutput>false</isOutput>
    </variables>
    <screens>
        <name>[FirstScreenName]</name>
        <label>[Screen Label]</label>
        <locationX>176</locationX><locationY>134</locationY>
        <allowBack>false</allowBack><allowFinish>true</allowFinish><allowPause>false</allowPause>
        <showFooter>true</showFooter><showHeader>true</showHeader>
        <fields>
            <name>[FieldName]</name>
            <fieldText>[Visible label the user reads — verbatim from story]</fieldText>
            <fieldType>InputField</fieldType>
            <dataType>[String|Currency|Number|Date|DateTime|Boolean]</dataType>
            <isRequired>true</isRequired>
        </fields>
        <connector><targetReference>[NextElementName]</targetReference></connector>
    </screens>
    <recordUpdates>
        <name>[UpdateElementName]</name><label>[Label]</label>
        <locationX>176</locationX><locationY>350</locationY>
        <inputReference>[recordVariable]</inputReference>
        <faultConnector><targetReference>[ErrorScreenName]</targetReference></faultConnector>
    </recordUpdates>
    <screens>
        <name>[ErrorScreenName]</name><label>Something went wrong</label>
        <locationX>440</locationX><locationY>350</locationY>
        <allowBack>true</allowBack><allowFinish>true</allowFinish>
        <fields>
            <name>ErrorText</name>
            <fieldText>&lt;p&gt;{!$Flow.FaultMessage}&lt;/p&gt;</fieldText>
            <fieldType>DisplayText</fieldType>
        </fields>
    </screens>
</Flow>
```
Non-negotiables: same as record-triggered plus — `fieldText` is what the user reads and what Playwright locates by (verbatim from story); `runInMode` is `DefaultMode` unless story justifies escalation.

Run the static checker on the file, then write the matching Playwright spec.

### Playwright Spec (screen flows only)
One spec file per screen flow: `tests/e2e/[FlowApiName].spec.ts`. Include in `files_written`.

Create these only if absent — never overwrite existing config:

`playwright.config.ts`
```ts
import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list'], ['html', { outputFolder: 'qa/evidence/_report', open: 'never' }]],
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
  },
});
```

`tests/e2e/support/session.ts`
```ts
import { execFileSync } from 'node:child_process';
export function bridgedUrl(org: string, path: string): string {
  const args = ['org', 'open', '-o', org, '-r', '-p', path, '--json'];
  const out = execFileSync('sf', args, { encoding: 'utf8' });
  const url = JSON.parse(out).result.url as string;
  if (!url) throw new Error(`Could not resolve a bridged URL for ${path}`);
  return url;
}
```

`tests/e2e/[FlowApiName].spec.ts`
```ts
import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { bridgedUrl } from './support/session';

const ORG = process.env.RH_TARGET_ORG!;
const STORY = '[story_number]';
const FLOW = '[FlowApiName]';
const EVIDENCE = `qa/evidence/${STORY}`;
const created: string[] = [];

function soql(query: string): any[] {
  const out = execFileSync(
    'sf', ['data', 'query', '--query', query, '--target-org', ORG, '--json'],
    { encoding: 'utf8' },
  );
  return JSON.parse(out).result.records ?? [];
}

test.describe(`${STORY} — ${FLOW}`, () => {
  test('AC-1: [restate the acceptance criterion verbatim]', async ({ page }) => {
    await page.goto(bridgedUrl(ORG, `/flow/${FLOW}`));
    await page.getByLabel('[fieldText from the flow, verbatim]').fill('value');
    await page.screenshot({ path: `${EVIDENCE}/AC-1-screen-1.png` });
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('[expected on-screen text]')).toBeVisible();
    await page.screenshot({ path: `${EVIDENCE}/AC-1-screen-2.png` });
    await page.getByRole('button', { name: 'Finish' }).click();
    const rows = soql(`SELECT Id, [Field__c] FROM [Object] WHERE [Field__c] = 'value' ORDER BY CreatedDate DESC LIMIT 1`);
    expect(rows.length).toBe(1);
    created.push(rows[0].Id);
  });

  test.afterAll(async () => {
    console.log(`RH_CREATED_IDS=${created.join('|')}`);
    for (const id of created) {
      try {
        execFileSync('sf', ['data', 'delete', 'record', '--sobject', '[Object]',
          '--record-id', id, '--target-org', ORG], { stdio: 'ignore' });
      } catch {
        console.warn(`Teardown failed for ${id}; report it, do not fail the run.`);
      }
    }
  });
});
```
Rules: one `test()` per AC labeled with the AC label; locate by `getByLabel`/`getByRole`; assert the data via SOQL, not just the DOM; error text is verbatim; screenshot each asserted screen; collect created Ids and print as `RH_CREATED_IDS=`; teardown failures warn, they do not fail the test; `workers: 1`.

### Apex Class & Trigger
`[ClassName].cls-meta.xml`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>[api_version]</apiVersion>
    <status>Active</status>
</ApexClass>
```

`[ClassName].cls`
```apex
/**
 * @description [Implementation detail summary — one line]
 * @story [story_number] — [subject]
 * @author radianHub
 */
public with sharing class [ClassName] {
    /**
     * @description [Method purpose]
     * @param records List of [ObjectName] records to process
     */
    public static void [methodName](List<[ObjectName]> records) {
        SObjectAccessDecision decision = Security.stripInaccessible(
            AccessType.UPSERTABLE, records
        );
        List<[ObjectName]> safeRecords = (List<[ObjectName]>) decision.getRecords();
        List<[ObjectName]> toUpdate = new List<[ObjectName]>();
        for ([ObjectName] rec : safeRecords) {
            // [logic per implementation_detail]
            toUpdate.add(rec);
        }
        try {
            if (!toUpdate.isEmpty()) { update toUpdate; }
        } catch (DmlException e) {
            throw e;
        }
    }
}
```

`[TriggerName].trigger`
```apex
/**
 * @description Trigger for [ObjectName] — delegates all logic to handler class.
 * @story [story_number] — [subject]
 */
trigger [TriggerName] on [ObjectName] (
    before insert, before update, before delete,
    after insert, after update, after delete, after undelete
) {
    [TriggerName]Handler.handle(Trigger.new, Trigger.old, Trigger.newMap, Trigger.oldMap, Trigger.operationType);
}
```
Standards: `with sharing` unless justified; bulkified; CRUD/FLS via `stripInaccessible()` or `WITH SECURITY_ENFORCED`; try/catch with Platform Event logging on all DML; JSDoc on classes and public methods; API version `[api_version]`.

## Dev Self-check before returning PRODUCED

For every flow file written, run the static checker (see **REFERENCE: Flow Static Checks** below). A `FAIL` means fix the file before handing off. Do not deploy a failing flow.

Every file must comply with the radianHub standards in the repo's `CLAUDE.md`.

## Output Contract

```
PRODUCED:
  story_number: [S-XXXXXX]
  story_record_id: [Salesforce Id]
  flow_type: [screen | autolaunched | n/a]
  flow_entry_point: [direct | record_page | quick_action | app_page | guest_site | utility_bar | n/a]
  static_check: [PASS | WARN — [rule ids]]
  files_written:
    - force-app/main/default/objects/[Object]/fields/[Field].field-meta.xml
    - force-app/main/default/flows/[FlowApiName].flow-meta.xml
    - tests/e2e/[FlowApiName].spec.ts
    - [etc.]
  post_deployment_steps:
    - [Step 1 from Post_Deployment_Steps__c]
  assumptions: [Any assumptions made, or "None"]
```

`flow_type` is mandatory on any story that produced a `.flow-meta.xml`. QA cannot infer it from `files_written`.

```
BLOCKED:
  story_number: [S-XXXXXX]
  story_record_id: [Salesforce Id]
  reason: [Specific missing/ambiguous info]
```

A `BLOCKED` result halts the dev loop for this story and routes to a human immediately.

---

# rh-qa — QA Instructions

Runs in Phase 2 of the orchestrator. Every value below is handed in already resolved.

## Purpose

For each deployed story, execute every testing step literally against the live sandbox
and report what you find. Each AC row lands in exactly one bucket:

- **confirmed working as specified** — the system behaves exactly as the AC states
- **needs human eyes** — anything else: wrong behavior, ambiguous result, untestable step

Nothing is assumed complete. Nothing is written back to the org. Nothing is fixed.

## Resolved inputs

`story_number`, `subject`, `object`, `target_environment`, `api_version`, `skills_root`,
`acceptance_criteria` (pre-numbered `AC-1`, `AC-2`, … — **use these labels as-is; never
renumber**), `acceptance_criteria_source`, `files_written` (from the dev PRODUCED contract),
`flow_type`, `flow_entry_point`, `test_persona_username`, `post_deployment_steps`, and
`formula_logic` / `error_message_text` (verbatim excerpts, for verifying exact error text).

The three acceptance-criteria sources — `Acceptance_Criteria__c records`, `Description or Technical Requirements fallback`, `architect-provided` — are all tested literally. Flag the unstructured ones in the report so a human double-checks QA's interpretation.

## QA Execution Sequence

### Step 0 — Component Presence Check

Before any AC, confirm every file in `files_written` corresponds to a deployed, active
component. Use the shared queries in **REFERENCE: Existence Checks** below. A missing or
inactive component means the dependent AC rows are `needs human eyes: component not
found/active — [file path]`.

`tests/e2e/*.spec.ts` entries are test source, not deployed components. Confirm the file
exists on disk; do not query the org for them.

### Step 0.5 — Flow Static Checks (any story that produced a flow)

```bash
python3 [skills_root]/rh-delivery-orchestrator/scripts/flow_static_check.py \
  force-app/main/default/flows/ --api-version [api_version] \
  --story [story_number] --json-out qa/[batch]_static_checks.json
```

- Exit `0` → record `static_check_results: PASS` (or `WARN` with rule ids) and continue.
- Exit `1` → every AC row depending on that flow is `needs human eyes: static check FAIL — [rule ids]`. **Do not run the browser.** Record the result and stop.

### Step 1 — Execute AC testing steps (literally as written)

For each pre-numbered AC row, execute its `Steps__c` literally against the live org.

**Record creation / DML:**
```bash
sf data create record --sobject [ObjectName] \
  --values "Field1__c='value' Field2__c='value'" \
  --target-org [target_environment]

sf data query --query "SELECT Id, [FieldName__c] FROM [ObjectName] \
  WHERE Id = '[RecordId]'" --target-org [target_environment]
```

**Validation rule (error must match `error_message_text` VERBATIM):**
```bash
sf data create record --sobject [ObjectName] \
  --values "[conditions that should trigger rule]" \
  --target-org [target_environment]
```

**Field value / formula:**
```bash
sf data query --query "SELECT Id, [FormulaField__c], [SourceField__c] \
  FROM [ObjectName] WHERE Id = '[RecordId]'" --target-org [target_environment]
```

**Record-triggered / autolaunched flow:**
```bash
sf data update record --sobject [ObjectName] --record-id [RecordId] \
  --values "[fields that trigger flow]" --target-org [target_environment]

sf data query --query "SELECT [ExpectedOutcomeField] FROM [ObjectName] \
  WHERE Id = '[RecordId]'" --target-org [target_environment]
```

**Permission / access:**
```bash
sf data query --query "SELECT Id, Field, PermissionsEdit, PermissionsRead \
  FROM FieldPermissions \
  WHERE SobjectType = '[ObjectName]' \
  AND Field = '[ObjectName].[FieldName__c]' \
  AND Parent.Name = '[PermissionSetName]'" --target-org [target_environment]
```

**Post-deployment step verification:** each `Post_Deployment_Steps__c` line is verified like an AC row. UI-only steps → `needs human eyes` with note "requires UI verification".

### Step 1b — Screen flows (`flow_type: screen`)

**Browser preflight:**
```bash
node --version                              # need 18+
npx playwright --version
npx playwright install --dry-run chromium 2>/dev/null || true
```
If tooling is missing: static checks still run; browser ACs become `needs human eyes: browser tooling unavailable`; never install mid-run.

**Run the dev-written spec:**
```bash
RH_TARGET_ORG=[target_environment] \
  npx playwright test tests/e2e/[FlowApiName].spec.ts \
  --reporter=list,html \
  --output=qa/evidence/[story_number]
```

For a persona other than the authenticated CLI user, use the per-persona org alias. For `guest_site` entry point, navigate `experience_site_url` directly as an anonymous visitor.

**Mapping Playwright outcomes to AC results:**

| Playwright outcome | AC row result |
|---|---|
| passed | `confirmed working as specified` |
| failed on assertion | `needs human eyes` with expected/actual |
| timed out | `needs human eyes: timed out reaching [locator]` |
| skipped | `needs human eyes: test skipped` |
| no test exists | `needs human eyes: no automated coverage for this criterion` |

What stays `needs human eyes` regardless: visual judgment (spacing, alignment, color), aesthetic/subjective AC text, accessibility beyond automated checks, behavior on uncovered browsers.

**Three rules that hold regardless:**
- Run as the persona, not the admin. A test that only passes as admin is a failure.
- A passing browser assertion is `confirmed` — same as a SOQL result.
- Missing browser tooling degrades, it does not fail. Never install browsers mid-run.

**Evidence:** collect paths under `qa/evidence/[story_number]/` and record in `ui_evidence_paths`. Include the `npx playwright show-trace` command in the report when a browser AC fails.

Parse `RH_CREATED_IDS=id1|id2|...` from spec output into `qa_created_record_ids`.

### Step 2 — Record the result per AC row

**Confirmed:**
```
[AC-N] ✅ confirmed working as specified
  Command run: [sf command, or playwright test name]
  Result: [actual output]
```

**Needs human eyes:**
```
[AC-N] ⚠️  needs human eyes
  Reason: [specific — wrong value / error message mismatch / component inactive /
           static check FAIL / persona lacks access / visual judgment required /
           ambiguous result / unstructured-source caveat]
  Command run: [sf command, or playwright test name]
  Actual result: [actual output]
  Expected: [what the AC's Steps__c states]
  Evidence: [screenshot or trace path, when a browser ran]
```

## Per-Story Status Report (returned to orchestrator)

```
QA Status Report — S-[Number]: [Subject]
Target Org: [alias]
Acceptance Criteria Source: [Acceptance_Criteria__c records | Description/Technical Requirements fallback | architect-provided]
Flow Type: [screen | autolaunched | n/a]
Static Checks: [PASS | WARN — [rules] | FAIL — [rules] | n/a]
Test Persona: [username | guest (unauthenticated) | n/a]
Tested: [timestamp]

| Criteria ID | Status | Notes |
|---|---|---|
| AC-1 | ✅ confirmed | |
| AC-2 | ⚠️ needs human eyes | Error message: expected "[verbatim]", got "[actual]" |
| Post-deploy: [step] | ✅ confirmed | |

Overall: [PASS — all confirmed | FAIL — [N] items need human eyes]
Evidence: qa/evidence/[story_number]/
Records created: [ids, or None]
```

## Sprint-Level Gate (after all stories pass)

**1. Regression baseline.** Re-query the exact record Ids captured in Phase 0 under
`build-state.json` → `regression_baseline`. Interpretation rules are in
**REFERENCE: Existence Checks** below.

```bash
sf data query --query "SELECT Id, [KeyFields] FROM [ObjectName] \
  WHERE Id IN ('[baseline ids]')" --target-org [target_environment]
```

Confirm no unexpected validation errors, no broken formulas, no missing field values.
Report count actually checked. Empty baseline → `N/A — no pre-existing records at run start`. **Do not substitute a fresh `ORDER BY CreatedDate DESC LIMIT 5`** — after QA runs, that returns QA's own records.

**2. Full e2e suite.**
```bash
RH_TARGET_ORG=[target_environment] npx playwright test
```
Skip with a stated reason if browser tooling is unavailable.

## Audit Output

QA writes into the orchestrator's single audit CSV. Schema in **REFERENCE: Audit CSV Schema** below. QA populates per story: `acceptance_criteria_source`, `static_check_results`, `ac_results`, `qa_status`, `ui_evidence_paths`, `qa_created_record_ids`, `attempt_count`, `retry_history`, `post_deployment_steps_confirmed`, `regression_baseline_checked`, `timestamp`. Update the story's existing row in place — never append a duplicate row.

## What QA Does Not Do

- Does not fix metadata, Apex, or e2e specs, and does not deploy/redeploy anything
- Does not rewrite a failing spec to make it pass
- Does not re-run a failed persona test as the deploy admin to get a green result
- Does not make judgment calls — ambiguous = `needs human eyes`
- Does not access production — sandbox only
- Does not assume anything is complete — every step is explicitly verified
- Does not skip post-deployment steps
- Does not renumber AC identifiers — uses the orchestrator's `AC-N` labels
- Does not install browser tooling mid-run
- Does not run the browser on a flow that failed static checks
- Does not maintain a separate audit file — writes into the orchestrator's CSV

---

# REFERENCE: Field Mapping

Single source of truth. The orchestrator resolves these in Phase 0 and hands the
**already-resolved values** to the dev and QA phases. There is no CSV.

| Variable | Salesforce source | Notes |
|---|---|---|
| `story_number` | `Story__c.Name` | e.g. `S-001042` |
| `story_record_id` | `Story__c.Id` | Cross-reference / build-state key only — never written into metadata |
| `subject` | `Story__c.Subject__c` | File header/comment titles |
| `priority` | `Story__c.Priority__c` | P1–P5 |
| `user_story` | `Story__c.Description__c` | Full story description |
| `object` | *Inferred* from `Technical_Requirements__c` / `Description__c` | **Not a discrete field.** If not resolvable unambiguously, ask the architect — do not guess. |
| `metadata_components` | `Story__c.Technical_Requirements__c` | Drives which files to produce. |
| `implementation_detail` | `Story__c.Technical_Requirements__c` | Primary build spec (same field). |
| `formula_logic` | Verbatim excerpt from `Technical_Requirements__c` | Used **verbatim** — never paraphrased. |
| `error_message_text` | Verbatim excerpt from `Technical_Requirements__c` or `Description__c` | Used **verbatim** — never paraphrased. |
| `pre_deployment_steps` | `Story__c.Pre_Deployment_Steps__c` | Orchestrator runs these in Phase 1 Step 1 |
| `post_deployment_steps` | `Story__c.Post_Deployment_Steps__c` | Written to post-deploy checklist; verified by QA |
| `acceptance_criteria` | `Acceptance_Criteria__c` child records, or fallback | Labeled `AC-1`, `AC-2`, … by the orchestrator in Phase 0 |
| `dependencies` | `Story__c.Predecessor_Story__c` | Single predecessor per Story |
| `target_environment` | Architect-supplied (chat/CLI) | Never pulled from the record |
| `api_version` | Run configuration — `CLAUDE.md`, defaulting to `67.0` | Never hardcoded in a template. |

## Flow-specific variables

| Variable | Source | Notes |
|---|---|---|
| `flow_type` | *Inferred* from `Technical_Requirements__c`, confirmed post-deploy via `FlowDefinitionView.ProcessType` | `screen` or `autolaunched`. If unclear, ask — do not guess. |
| `flow_entry_point` | *Inferred* from `Technical_Requirements__c` / `Description__c` | Required for screen flows only. See table below. |
| `experience_site_url` | `Technical_Requirements__c`, else architect-supplied, else queried from `Network` | Required when `flow_entry_point` is `guest_site`. |
| `test_persona_username` | `Acceptance_Criteria__c.As_A__c` mapped to a real user, else architect-supplied | **Never the deploy admin.** |

### `flow_entry_point` values

| Value | How QA reaches it | Also needs |
|---|---|---|
| `direct` | `/flow/[FlowApiName]` | nothing |
| `record_page` | Record page URL for a seeded record | a record Id and the Lightning page that embeds the flow |
| `quick_action` | Record page, then click the action | the action's label |
| `app_page` | The Lightning app page URL | the page's dev name |
| `guest_site` | `experience_site_url` as an anonymous visitor | `experience_site_url` |
| `utility_bar` | Open the utility item in the app | the app and utility label |

If the entry point is not resolvable unambiguously, ask the architect. A screen flow tested through the wrong entry point can pass while the real user path is broken.

## `api_version` is resolved, not hardcoded

Current default is `67.0` (Summer '26). Confirm against the target org before a run that spans a release boundary:
```bash
sf org display --target-org [target_environment] --json
```

## The verbatim rule

`Technical_Requirements__c` is one rich-text blob — not pre-split fields. When any phase uses formula/condition text or error message text: locate the exact text and copy it **verbatim**. If the field does not clearly delineate where formula logic ends and error message text begins, **do not guess** — the story is BLOCKED.

---

# REFERENCE: Audit CSV Schema

`force-app/../qa/[batch]_QA_Audit.csv` — one CSV for the whole run. Initialize in Phase 0, keep every row current as the run progresses.

| Column | Populated in | Contents |
|---|---|---|
| `story_number` | Phase 0 | `S-XXXXXX` |
| `story_record_id` | Phase 0 | Salesforce Id, cross-reference only |
| `subject` | Phase 0 | `Subject__c` |
| `acceptance_criteria_source` | Phase 0 | `Acceptance_Criteria__c records` / `Description or Technical Requirements fallback` / `architect-provided` |
| `flow_type` | Phase 1 | `screen` / `autolaunched` / `n/a` |
| `flow_entry_point` | Phase 0 | direct / record_page / quick_action / app_page / guest_site / utility_bar / n/a |
| `test_persona_username` | Phase 0 | The user whose session ran the browser tests, `guest (unauthenticated)`, or `n/a` |
| `files_written` | Phase 1 | Pipe-delimited list of every file path produced |
| `deploy_status` | Phase 1 | `PASS` / `FAILED` / `INTERVENED` |
| `deploy_notes` | Phase 1 | Deploy error output, or `None` |
| `assumptions` | Phase 1 | Anything the dev phase flagged as an assumption |
| `static_check_results` | Phase 2 | `PASS` / `WARN` / `FAIL`, then pipe-delimited rule ids. `n/a` when no flow produced. |
| `ac_results` | Phase 2 | Per-AC breakdown for the current/latest attempt: `AC-N: confirmed | needs human eyes — [note]`, pipe-delimited |
| `qa_status` | Phase 2 | `PASS` / `PASS ON RETRY` / `INTERVENED` — current/latest attempt only |
| `ui_evidence_paths` | Phase 2 | Pipe-delimited paths to Playwright traces and screenshots. `n/a` when no browser ran. |
| `qa_created_record_ids` | Phase 2 | Pipe-delimited Ids of every record QA created |
| `attempt_count` | Phase 2 | QA attempts so far for this story (starts at 1) |
| `retry_history` | Phase 2 | Prior attempts — see below |
| `post_deployment_steps_confirmed` | Phase 3 | `Yes` / `No` / `Partial`, with a short note |
| `regression_baseline_checked` | Phase 3 | Count of baseline Ids re-verified, or `N/A — no pre-existing records at run start` |
| `timestamp` | All phases | Last updated for this row |

## Retry trail

`ac_results` and `qa_status` always reflect the **current** attempt. Before a retry overwrites those columns, append the outgoing attempt to `retry_history`, oldest first:

```
Attempt 1 (2026-07-14T14:32:00Z) — INTERVENED: AC-1: confirmed | AC-2: needs human eyes — error message mismatch
```

Separate multiple prior attempts with `;`. Increment `attempt_count` each time a retry runs.

---

# REFERENCE: Existence Checks

Shared SOQL serving two purposes:

- **Dev — collision detection (before writing):** confirm a component does **not** already exist. On a hit, surface to the orchestrator → human. Never overwrite silently.
- **QA — component presence check, Step 0 (after deploy):** confirm every file in `files_written` **does** correspond to a deployed, active component.

Derive the component type and API name from each file path.

```bash
# Field
sf data query --query "SELECT Id, DeveloperName FROM FieldDefinition \
  WHERE EntityDefinition.QualifiedApiName = '[Object]' \
  AND DeveloperName = '[FieldName]'" \
  --target-org [target_environment]

# Validation rule
sf data query --query "SELECT Id, ValidationName, Active FROM ValidationRule \
  WHERE EntityDefinitionId IN \
    (SELECT Id FROM EntityDefinition WHERE QualifiedApiName = '[Object]') \
  AND ValidationName = '[RuleName]'" \
  --target-org [target_environment]

# Flow — use FlowDefinitionView (not Flow, which is Tooling API only)
sf data query --query "SELECT Id, ApiName, Label, ProcessType, TriggerType, \
  IsActive, IsOutOfDate, ActiveVersionId, VersionNumber \
  FROM FlowDefinitionView WHERE ApiName = '[FlowAPIName]'" \
  --target-org [target_environment]
```

## Classifying a flow independently of the dev contract

| `ProcessType` | Means | QA path |
|---|---|---|
| `Flow` | Screen flow | Static checks, then browser execution |
| `AutoLaunchedFlow` | Record-triggered, scheduled, or autolaunched | Static checks, then CLI/SOQL |

`TriggerType` further separates record-triggered (`RecordBeforeSave` / `RecordAfterSave`) from scheduled (`Scheduled`) and plain autolaunched (`None`).

If `ProcessType` from the org disagrees with `flow_type` from the dev contract → `needs human eyes` on every dependent AC.

- `IsActive = false` → `needs human eyes: flow deployed but inactive`
- `IsOutOfDate = true` → `needs human eyes: flow deployed but active version is stale`

## Reading the result

- Dev: a returned row = collision → stop and surface.
- QA: no row, or `Active`/`IsActive` false, or `IsOutOfDate` true = component missing/inactive/stale → dependent AC rows are `needs human eyes`.

## Regression baseline capture (Phase 0, before any deploy)

For each in-scope object, before the first deploy:

```bash
sf data query --query "SELECT Id FROM [Object] ORDER BY CreatedDate DESC LIMIT 5" \
  --target-org [target_environment] --json
```

Store returned Ids in `build-state.json` under `regression_baseline`. Phase 3 re-queries those exact Ids.

| Baseline size | Phase 3 verdict |
|---|---|
| 5 Ids captured | Re-query all 5, confirm no new validation errors, no broken formulas, no null-out |
| 1–4 Ids captured | Re-query what exists, report the actual count checked |
| 0 Ids captured | `N/A — no pre-existing records in [Object] at run start`. Do **not** report a pass. |

When the baseline is empty, the regression signal comes from the committed e2e suite instead.

---

# REFERENCE: Flow Static Checks

One script, two consumers:

- **Dev — self-check before deploy.** Run on every flow file produced. A `FAIL` means fix the metadata, not deploy and hope QA catches it.
- **QA — gate at Step 0.5, after deploy.** A `FAIL` makes every dependent AC `needs human eyes` without touching a browser.

```bash
python3 [skills_root]/rh-delivery-orchestrator/scripts/flow_static_check.py \
  force-app/main/default/flows/ \
  --api-version [api_version] \
  --story [story_number] \
  --json-out qa/[batch]_static_checks.json
```

Exit code `0` = no FAIL findings. Exit `1` = at least one FAIL. Exit `2` = bad invocation or unparseable file.

## Severity contract

| Severity | Meaning | Effect |
|---|---|---|
| `FAIL` | Violates a radianHub standard, provable from the XML | Blocks: dev fixes, or QA marks dependent ACs `needs human eyes` |
| `WARN` | Probably wrong, not provable without intent | Surfaces to the human, does not block |
| `INFO` | Context for the reviewer | Never blocks |

## Rules (all flows)

| Rule | Severity | What it catches |
|---|---|---|
| `api-version` | FAIL | `apiVersion` differs from the run's `api_version` |
| `status` | FAIL | `status` is not `Active` |
| `description` | FAIL/WARN | Missing or not prefixed with the story number |
| `io-in-loop` | FAIL | DML/callout node reachable inside a loop body |
| `missing-fault-path` | FAIL | A node that can throw has no `faultConnector` |
| `decision-no-default` | FAIL | A `decisions` element has no `defaultConnector` |
| `unreachable-node` | FAIL | A node not reachable from `<start>` |
| `hardcoded-id` | WARN | A 15 or 18 char token that looks like a Salesforce Id |
| `run-in-mode` | WARN | `SystemModeWithoutSharing`, which bypasses sharing |

## Rules (screen flows only, `processType: Flow`)

| Rule | Severity | What it catches |
|---|---|---|
| `screen-flow-no-screens` | FAIL | `processType` is `Flow` but no `<screens>` declared |
| `run-in-mode` (absent) | WARN | No `runInMode`; confirm that matches the AC persona |
| `unused-input-variable` | WARN | A variable marked `isInput` that nothing references |
| `input-not-required` | INFO | An `InputField` with no `isRequired` |

## How `io-in-loop` decides

The script builds the connector graph, then from each `loops` element follows
`nextValueConnector` and every downstream connector until control reaches the loop
name again or the path ends. Fault connectors are excluded from that traversal.

---

# REFERENCE: Test Persona Provisioning

Read only when a screen flow story is in scope.

The deploy admin is never a valid test persona. `Acceptance_Criteria__c.As_A__c` names the persona whose session must execute the AC.

## Resolution order

1. Architect supplied `test_persona_username` in chat for this run. Use it as-is.
2. `As_A__c` names a persona that maps to an existing active user in the target org:
   ```bash
   sf data query --query "SELECT Id, Username, IsActive, Profile.Name \
     FROM User WHERE IsActive = true AND Profile.Name = '[ProfileName]' \
     AND Id != '[deploy_admin_id]' LIMIT 5" \
     --target-org [target_environment]
   ```
3. `As_A__c` describes a guest / unauthenticated visitor. Follow the guest section below.
4. Nothing matches. Ask the architect. Do not fall back to the deploy admin.

Record the resolved value in the audit CSV. For a guest flow, record `guest (unauthenticated)`.

## When the org has no seeded users

Ask before creating anything:

> "S-XXXXXX is a screen flow whose AC runs as `[As_A__c]`. The target org has no active non-admin user matching that persona. Options:
>   (a) I create a test user on profile `[X]` with permission set `[Y]`
>   (b) you give me an existing username to run as
>   (c) I run static checks only and mark the browser ACs `needs human eyes`
> Which?"

Only on an explicit `(a)`:
```bash
sf data query --query "SELECT Name, TotalLicenses, UsedLicenses FROM UserLicense" \
  --target-org [target_environment]

sf data create record --sobject User --values \
  "Username='rhqa.[story_number]@[org-suffix].invalid' \
   Alias='rhqa' Email='[architect_email]' LastName='RH QA Persona' \
   ProfileId='[profile_id]' TimeZoneSidKey='America/New_York' \
   LocaleSidKey='en_US' EmailEncodingKey='UTF-8' LanguageLocaleKey='en_US'" \
  --target-org [target_environment]

sf org assign permset --name [PermissionSetName] \
  --target-org [target_environment] --on-behalf-of [username]
```

Created personas are **run artifacts, not fixtures.** Log every one in the audit CSV and list them in the end-of-run summary. Do not deactivate them mid-run.

A newly created user has no password. Bridge the session using `bridgedUrl()` from `tests/e2e/support/session.ts` — no password is ever handled and MFA never appears.

## Guest / Experience Site flows

Guest flows need `experience_site_url`. Resolve in this order:

1. `Technical_Requirements__c` or `Description__c` states the site URL or name.
2. Architect supplies it in chat.
3. Query the org for candidates and present them, do not pick one:
   ```bash
   sf data query --query "SELECT Id, Name, UrlPathPrefix, Status \
     FROM Network WHERE Status = 'Live'" --target-org [target_environment]
   ```

If unresolvable: BLOCKED with reason `experience_site_url unresolved for a guest-access screen flow`. Never guess a host, and never substitute the authenticated `/flow/[ApiName]` path for a guest flow — those exercise different permission stacks.
