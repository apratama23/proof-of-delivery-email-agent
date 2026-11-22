# Progress

Session log, newest first, and the list of known gaps. A gap stays listed
until it is closed. States: **designed, not built**; **built, not
measured**; **measured**.

---

## Prepared for publishing

**Done**
- Names that pointed at one business (its systems, tables, customers'
  prefixes, location and time zone label) are replaced by neutral ones in
  the code comments, docs, examples and tests; the time offset, rules and
  settings the agent acts on are unchanged.
- The second Power BI table's settings are `POD_PBI_SECOND_*`.
- Connote prefixes are set in `.env` (`POD_CONNOTE_PREFIXES`), and the
  targets are listed in `.env.example` (D-060). The training workbook's
  file name is `POD_TRAINING_DATA_FILE`; the master list has no default.
- Microsoft Graph is not used: there is no access to it. A card's button
  opens the POD mailbox, as it always did without Graph (D-061).
- The server migration plan is removed: it relied on Microsoft Graph.

**Verified**
- The test suite and the simulation pass, with and without a local
  `.env`; the prefix check gives the same result as before for every token
  tried, with the same prefixes set.

---

## An email moved by hand is found again; the pull lists emails quickly

**Done**
- The categories and the move to Actioned/Finished find an email moved by
  hand by its Internet Message ID and keep its new entry ID; the cleanup of
  files to send by hand removes each folder on its own, read-only files
  included; the historical pull lists emails through Outlook's table, skips
  folders with no emails and names each folder as it goes (D-059). Both
  came from a bug review.

**Verified**
- 386 tests pass; the new ones fail on the code before. The simulation
  passes 34 of 34.

**Not verified**
- Outlook's table and the lookup by Internet Message ID on the production
  mailbox; how long a real pull takes now.

---

## CI on the agent's Python version

**Done**
- The agent runs on Python 3.14, not 3.10 (product owner). CI now tests 3.13 and
  3.14 on Windows; scikit-learn is 1.8 or newer, as the models were
  trained with it; the docs naming 3.10 are updated.

**Verified**
- The GitHub run for this change (see its result).

---

## The historical pull takes each email once

**Done**
- The pull collects by entry ID instead of by place in the folder, skips
  an email already taken, and merges a workbook that holds an email twice
  into one row; different labels on such rows stop the pull (D-058). The
  fix plan is done and removed.
- A test read the POD mailbox address from the local `.env`, so CI failed
  without it; the test now sets its own.

**Verified**
- 381 tests pass; the new ones fail on the code before, with the same
  `InvalidIndexError`.

**Not verified**
- A real pull in production.

---

## Partial sending by the sender's domain

**Done**
- `POD_SEND_MODE` has `partial`: replies to the senders of
  `POD_SEND_DOMAINS` (the domain after the @ exactly) are sent, the others
  are drafts; the flow is unchanged. The Settings tab chooses Off, Partial
  or Full with the domain list, asking first when more is sent.
  `POD_SENDER_FOLDERS` and `POD_PBI_SENDER_CHECK` match the exact domain too
  (D-057). From a bug review.

**Verified**
- 373 tests pass; the simulation passes 34 of 34 (it now runs in partial
  mode; S34 new).

**Not verified**
- A real sent reply in production.

---

## The attachments read when the text's numbers are not in Power BI

**Done**
- When every number of the text is not in Power BI, the attachments are
  read and the agent goes on with their numbers; a known one drops the
  text's numbers; none known ends as before (D-056). From a bug review.

**Verified**
- 370 tests pass; the ones that change the flow fail on the code before.
  The simulation passes 33 of 33 (S33 new).

**Not verified**
- A real email with a wrong number in its body and the connote in its
  attachment, in production.

---

## The POD mailbox in CC on every reply

**Done**
- Every reply has the POD mailbox in CC, never in To: moved, or added; the
  copy that comes back to the POD inbox is skipped by the guard, also when
  sent on behalf of the POD mailbox (D-055). From a bug review.

**Verified**
- 364 tests pass; the new ones fail on the code before. The simulation
  passes 32 of 32.

**Not verified**
- A real reply in production: To and CC in Outlook, and the copy back
  in the inbox shown as Skipped - Guard.

---

## Both targets 90%, set in the dashboard

**Done**
- The pilot counts the PODs only: a draft sent with exactly the PDFs the
  agent attached is "sent, same PODs", whatever its text; the Overview's
  figure is PODs unchanged (D-053).
- The pilot target and the intent precision target are 90%, set in the
  dashboard Settings tab (`POD_PILOT_TARGET`, `POD_PRECISION_TARGET`,
  D-054).

**Verified**
- 360 tests pass; the simulation passes 32 of 32.

**Not verified**
- In production's real data.

---

## The Overview's figures counted as they read

**Done**
- The pilot counts a draft sent with exactly the PDFs the agent attached,
  whatever its text, as "sent, same PODs"; the Overview's PODs unchanged
  is of drafts sent or deleted (D-052, D-053). The headline figures, the numbers
  chart, the top domains and the waiting chart count what their names say
  (D-052). From a bug review.

**Verified**
- 358 tests pass; each new test fails on the code before. The simulation
  passes 32 of 32.

**Not verified**
- The Overview on real tracking data in production, and a draft sent
  with its PODs unchanged counted so on the real Sent Items.

---

## A button on every Teams card to open the mailbox

**Done**
- Manual Review, correction and file choice cards have a button that opens
  the POD mailbox in Outlook on the web (D-051, D-061). From a bug review.

**Verified**
- 353 tests pass; the simulation passes 32 of 32.

---

## Files too large: a draft without them, POD Outstanding updated

**Done**
- A reply over `POD_MAX_ATTACHMENT_MB` is a Reply All draft without its
  attachments, also in send mode, with its POD Outstanding rows written;
  the copies for the team stay until the draft is sent or deleted; the
  pilot measure leaves these drafts out (D-050). From a bug review.

**Verified**
- 351 tests pass; the new ones fail on the code before. The simulation
  passes 32 of 32 (S07 changed: the team sends the draft).

**Not verified**
- A real large email in production.

---

## Flow 1 watched; a backslash typed on a card

**Done**
- A Power BI request unanswered for 30 minutes is written again, at most 3
  times, then shown in the Review queue with its request id; a `\` typed on
  a Teams card no longer makes its answer unreadable (D-049).

**Verified**
- 349 tests pass; each new test fails on the code before the fix. The
  simulation passes 32 of 32.

**Not verified**
- Flow 1 switched off in production, to see the warning.

---

## Manual Review in Teams too

**Done**
- An email that goes to Manual Review gets a Teams card like the
  dashboard's review: the email's first 500 characters, Connotes and
  References fields, POD request / Not a POD request / Handle manually
  (D-048). Not at cold start; not for emails already in Manual Review.

**Verified**
- 346 tests pass; the simulation passes 32 of 32 (S31, S32 new).

**Not verified**
- Flow 3 with the `references` field and the card in the real Teams chat.

---

## Fixes from a review of the whole project

**Done**
- No limit on how many numbers a person saves; an unsure POD request stays
  unsure across attachment sources; one scan of two numbers is attached
  once; dashboard reviews and Mark resolved wait for a running cycle; a
  half-written Power Automate result is read again next run (D-047).

**Verified**
- 334 tests pass; each new test fails on the code before the fix. The
  simulation passes 30 of 30.

**Not verified**
- The dashboard's wait for a running cycle in production.

---

## Numbers from spreadsheets; a POD request reviewed without numbers

**Done**
- A POD request whose numbers are only in an Excel list is answered: the
  connote and reference columns of `.xlsx`, `.xlsm`, `.csv` and `.xls`
  files are read, after the PDF names, through the email text's own scheme
  (D-046).
- The Review queue saves `POD_REQUEST` without numbers; the agent then reads
  the attachments again. From a bug review.

**Verified**
- 328 tests pass; the simulation passes 30 of 30 (S29, S30 new; S29 fails
  when the Invoice column is read).

**Not verified**
- The customer's real list and its header in production; `.xls` read by
  the real `xlrd` (the test fakes it).

---

## A separator between the draft and the email trail

**Done**
- Classic Outlook draws the line above "From:" as the top border of the div
  around it; the agent cut the quote at "From:" inside that div and lost the
  line. The quote now starts at that div (or the `<hr>` newer Outlook uses);
  a quote with neither gets the same line drawn. From a bug review.

**Verified**
- 319 tests pass. A real Reply All from classic Outlook (made,
  read and discarded): the old code dropped the separator, the new keeps it.

**Not verified**
- A draft from the POD mailbox opened in Outlook in production.

---

## A number the team keeps is answered "not in our system"

**Done**
- The correction card asks only the numbers not found; a number the team
  keeps unchanged that Power BI still does not know goes in the reply as
  not in our system, with no new card; a changed number still unknown is
  asked again; the POD Outstanding row stays (D-045). From a bug review.

**Verified**
- 317 tests pass; the simulation passes 28 of 28 (S27, S28 new).

**Not verified**
- The Teams card and the draft in production.

---

## Intent first; numbers from attachments; the product owner's checking order

**Done**
- Intent first. A POD request with no number in its text has its PDF
  names, pictures (Windows OCR) and PDF text read, one source after
  another; none: Manual Review (D-044).
- Several scans on the share ask Power BI first; Power BI by DoNumber then
  Reference; its scan path is used for every sender, whatever the status;
  special customers' rows checked by sender (`POD_PBI_SENDER_CHECK`); a
  reference that is another number's document is attached once.
- Below the intent threshold, as a person reads it: an unsure POD request
  goes on and is executed when found, else Manual Review; an unsure "not a
  POD request" goes to Manual Review only when it has a number.

**Verified**
- 313 tests pass, including real Windows OCR on a picture and a scanned
  PDF; the simulation passes 26 of 26 (S20 to S26 new).

**Not verified**
- In production: real attachments, the OCR language, the special
  customer's flows a to d.

Plan 4 is done by D-044 and removed.

---

## Plan 3 closed

**Done**
- Plan 3's number correction for held emails is built (D-035, D-036). Its
  PDF upload is not built (product owner): a POD not on the share is sent by the
  team replying in Outlook, which D-039 resolves. Plan 3 is removed.

---

## Plan 2 closed

**Done**
- Plan 2's built stages (whole-number matching, the Power BI scan path,
  references searched by their connote) and its deferred ranking are in
  D-043; plan 2 is removed. Counting the file choices later is a
  production check.

---

## Intent model: stemming; model and threshold measured

**Done**
- The intent model stems each word before the vectorizer (Snowball,
  `classifier.stem_text`), chosen by the product owner (D-016 note). Models
  trained before keep working unstemmed until retrained.

**Verified**
- 283 tests pass; the simulation passes 19 of 19.
- Measured on 1,587 labeled emails: stemming F1 0.853 to 0.860, at 97%
  precision 61% of POD requests answered instead of 57%. Voting, AdaBoost,
  random forest and lemmatization: no gain. Threshold 0.92 is the lowest
  with 95% POD precision. CRF number threshold 0.6 adds about 2 points of
  precision.

**Not verified**
- In production: `pip install -r requirements.txt`, then retraining.

---

## Scans matched by the whole numbers in their names

**Done**
- A scan's name is read as parts (connote, reference or date, `_` or `-`,
  Signed and its copy number); a file matches a whole part only. A signed
  scan wins over one without the word, Unsigned scans are not indexed, and
  a number inside a pallet number is never a match (D-042).

**Verified**
- 281 tests pass, with the product owner's name patterns.
- The simulation's new S19 passes (19 of 19); on the old matching it went
  to the team instead.

**Not verified**
- Against the real share's file names in production.

---

## Emails moved or deleted in Outlook

**Done**
- A waiting email filed in `Actioned/Finished` is Resolved; a POD request
  moved elsewhere or deleted is Held for the team; an Other email moved
  anywhere is Resolved. Moved emails are found by their Internet Message
  ID, now kept in tracking (D-041). Plan 1 is done and removed.

**Verified**
- 280 tests pass: in place, moved to the finished folder (same entry ID,
  and found by message ID), moved elsewhere (held once), an Other email,
  the message ID on later events, a held email's card not posted again,
  through a full run.
- The simulation's S16 to S18 pass (18 of 18).

**Not verified**
- In production: whether Outlook changes the entry ID when an email is
  moved, and the search by Internet Message ID in the finished folder.

---

## A numbered id for each Power BI request

**Done**
- Each Power BI request has its own id; only the answer to the current one
  is read, a late answer to an earlier one is archived unread (D-040). No
  Flow 1 change.

**Verified**
- 270 tests pass, including a late answer after Reprocess and an email
  waiting from before ids were numbered.
- The simulation's new S15 (a late, empty answer to the first request
  before the right one) passes; on the old code it holds the email wrongly.

**Not verified**
- In production, with the real Flow 1.

---

## End-to-end simulation

**Done**
- `python -m sim.run`: 14 scenarios through the real agent with a fake
  Outlook, fake Flows 1 to 3 and dummy data, outside the project;
  `--dashboard` opens the dashboard on it (`docs/SIMULATION.md`).

**Verified**
- 14 of 14 scenarios pass; the dashboard renders the simulated emails; the
  real tracking data is untouched.

**Not verified**
- Everything the simulation fakes (Outlook, Power Automate, Teams,
  SharePoint): "Testing in production" in `docs/TEST.md`.

---

## Emails answered by hand; setup check

**Done**
- An email a person answered in Outlook (Reply or Reply All) gets no
  draft: checked before every reply and, for waiting emails, each resume
  run; it becomes Resolved, "replied manually in Outlook" (D-039).
- `python -m app.check_setup`: the newer settings, files flows have not
  taken, the git identity. The production checks, in order, are in
  "Testing in production" in `docs/TEST.md`.

**Verified**
- Unit tests: no draft after a manual reply; a failed check sends nothing;
  a waiting email resolved and its card withdrawn; a late Power BI result
  archived; reading Outlook's mark; the setup check's warnings.

**Not verified**
- Outlook's mark in production, from Outlook on the web and mobile, and
  its time in UTC.

---

## Emails held for their attachment size

**Done**
- The Teams message lists each requested number with its status and links
  a SharePoint folder with the files to attach by hand; the copies are
  deleted once the email is no longer held (D-038).

**Verified**
- Unit tests: the list, the copy and link, cleaning after Resolved, folders
  that are not the agent's left alone, a folder inside the project or a
  scan folder refused.

**Not verified**
- `POD_MANUAL_UPLOAD_DIR` and `POD_MANUAL_UPLOAD_URL` in production, and
  that the link opens the folder in SharePoint.

---

## Review findings fixed

**Done**
- The customer whose Power BI rows give a scan path is set in `.env`
  (`POD_PBI_SCAN_PATH_SENDERS`, `POD_PBI_SENDER_COLUMN`,
  `POD_PBI_SCAN_PATH_COLUMN`); no customer, server or real column names
  are left in tracked files; `.env.example` is back to placeholders.
- A Power BI scan path is looked up in a table built once per index, not by
  resolving every indexed file.
- `POD_MAX_ATTACHMENT_MB` defaults to 20 again.
- References are searched without padded forms, which they already contain.

**Verified**
- 257 tests pass.

**Not verified**
- The three scan path settings in the production `.env`.

---

## Teams cards that never reach Teams

**Done**
- A card not taken by Flow 3, or refused by Teams, is posted again after 30
  minutes, at most 3 times; then the Review queue shows it with its request
  ID and Send Teams card again (D-037). Also for correction cards.
- No tracking event while waiting for an answer; stale files in `teams/`
  are archived.
- After "Not all", the earlier Power BI answers for the other numbers stay.
- The dashboard auto-refresh is paused while a pop-up is open.

**Verified**
- Unit tests: no resend while a card waits for an answer; resends, archive
  and the warning after 3; an error result; an older card's answer; the
  correction card; the sweep; "Not all" with a second number.

**Not verified**
- Flow 3 changes (Move file first, error file) in production; the
  auto-refresh pause in a browser.

---

## Held emails corrected in the dashboard

**Done**
- Held emails open in the Review pop-up with the intent POD_REQUEST; after
  the numbers are corrected the agent continues them and replies with every
  POD found. POD Outstanding rows the agent added for numbers taken out are
  removed through Flow 2 (D-035).

- The same correction on a Teams card for holds of an unknown number or an
  unreadable number list: Save numbers or Handle manually (D-036).
- Typed numbers are checked (no count limit since D-047; letters, digits, `-` and `/`, 4 to
  20 characters); the pop-up shows the list to look up before Save; History
  shows the numbers of each status.

**Verified**
- Unit tests: a held email with an invoice number taken out is replied with
  the POD found, and the remove request for the invoice row is written; from
  the dashboard and from the Teams card. Handle manually, empty numbers (a
  new card) and plain messages for other holds. Typed-number checks, the
  held pop-up (AppTest) and numbers per status in History.

**Not verified**
- In production: add `connotes` to Flow 3's result file
  (`docs/POWER_AUTOMATE_FLOW.md`), then correct a held email in Teams and in
  the dashboard and check the draft and the POD Outstanding row.

---

## Connote lists in short form

**Done**
- `app/number_lists.py`: lists and ranges in short form are written out in
  the text the numbers were found in, on the CRF and regex paths alike; an
  unreadable list holds the email (D-034). The regex reads the first number
  of `7336101-103`; `4653685-1` and `-01` stay one connote. Numbers with another
  customer's prefix are recognised by the regex.

**Verified**
- 220 tests pass (14 new): the product owner's examples, ranges, the 20 limit,
  backwards ranges, letters, dates, part numbers, invoice plus connote, both
  extraction paths, the hold.
- On the 472 labeled POD emails: 14 outputs change, 3 closer to the labels,
  11 adding numbers the labels lack (invoice numbers, the plan's example),
  none held.

**Not verified**
- Whether invoice numbers are in TMS's Reference column (else held).

---

## Open in Outlook from the dashboard

**Done**
- "Open in Outlook" in the Review, Label and History pop-ups
  (`outlook_reader.open_in_outlook`): the email in its own Outlook window,
  looked up in the POD mailbox only; an email the agent moved opens by its
  new ID (`dashboard_data.current_entry_id`). Outlook marks it read (product owner).
  A missing email or Outlook not running shows an error (D-033).

**Verified**
- 206 tests pass (3 new): only Display is called, in the POD mailbox; a
  missing email raises; a moved email's new ID is used; the button is in
  the Review and Label pop-ups.

**Not verified**
- In production: the window opens from each pop-up. The History
  pop-up's button is not covered by a test (the test runner cannot select
  a History row).

---

## Drafts in the POD mailbox's Drafts folder

**Done**
- Replies open the email in the POD mailbox (its StoreID) and move the
  draft to the "Drafts" folder found by name under the mailbox's root; a
  draft that lands anywhere else fails the reply, naming where (D-032).
- `python -m app.check_drafts`: read-only check of which Drafts is used,
  Outlook's default one, and where the latest draft is.

**Verified**
- 203 tests pass (4 new): the email opened with the POD StoreID, the draft
  moved to the mailbox's own Drafts, a draft elsewhere is an error naming
  where, no draft made without a Drafts folder, the check's output.

**Not verified**
- In production: run `app.check_drafts`, then one draft-mode reply.

---

## Intake waits for the lock; quiet resume runs

**Done**
- A run that takes new emails waits for the lock up to 2 minutes, retrying
  every 5 seconds, instead of skipping while a resume run holds it; resume
  still skips at once.
- A resume-only run that did nothing is not written to `run_log.txt`;
  intake always is.

**Verified**
- 200 tests pass (4 new): intake waits and then runs, gives up after the
  wait with a log line; resume skips at once without a line; an idle
  resume run leaves no line, a busy one does.

---

## Separate intake and async resume commands

**Done**
- `python -m app.main --process-new` runs only unread-email intake.
- `python -m app.main --resume` runs only review, Power Automate/Teams
  follow-up, draft checks, category updates and finished-email moves.
- The existing no-option command continues to run both parts together;
  `--reset` remains reset-only.
- Runbook includes separate Task Scheduler commands and configurable
  `$intakeMinutes` / `$resumeMinutes` intervals (5 and 1 minutes by default).

**Verified**
- 196 tests pass, including separated-mode behavior and CLI options.
- `python -m app.main --help` shows the available mutually exclusive modes.

**Not verified**
- Separate Task Scheduler jobs have not been registered or exercised in
  production.

---

## local time (UTC+8)

**Done**
- Every time shown or logged is local time (UTC+8) (`config.LOCAL_TIME`, UTC+8, no
  daylight saving), whatever the PC's zone (product owner): dashboard dates and
  ages, label dates and their filter, the "On since" of the finished-email
  switch, and `run_log.txt` lines (`... UTC+8`). Stored times stay UTC, so
  every comparison is unchanged (product owner). Outlook's own times are read as
  the PC's wall clock, UTC+8 in production (product owner).

**Verified**
- 188 tests pass (2 new): a UTC time shows as local time (UTC+8) across midnight,
  label dates are local (UTC+8) dates, the log line is stamped UTC+8.

---

## Move finished emails to Actioned/Finished

**Done**
- Settings: "Move finished emails to Actioned/Finished" switch; switching
  on records the time (`POD_MOVE_FINISHED`, `POD_MOVE_FINISHED_SINCE`).
- `main._move_finished`, last in each cycle: replied emails with every POD
  attached and a reply sent after the switch-on move to `POD_FINISHED_FOLDER`
  under `POD_OUTLOOK_MAILBOX` (`outlook_reader.move_to_child_folder`), once.
- Replies record `sent_at`: at once when sent, from Sent Items when a draft
  is sent (`pilot.first_reply`). D-031.

**Verified**
- 186 tests pass (8 new): only finished replies sent after the switch-on
  move, once; nothing moves with the switch off; a failure is counted and
  the others still move; the folder name follows the setting; `sent_at`
  for sent replies and sent drafts; the folder with a "/" in its name is
  found among the children; the switch saves its time.

**Not verified**
- On live Outlook: the folder found, the email moved, its categories kept.

---

## Dashboard split by tab

**Done**
- `dashboard/app.py` (1,137 lines) split into the page (`app.py`, 49
  lines), one module per tab (`tab_overview`, `tab_review`, `tab_history`,
  `tab_labeling`, `tab_models`, `tab_jobs`, `tab_settings`; 50 to 250 lines)
  and `common.py` for what they share. Every function and constant moved
  verbatim; imports worked out per module. Same command, same behaviour.

**Verified**
- All 32 top-level statements are in the new files exactly once and
  unchanged (compared as syntax trees); no undefined names, no unused
  imports. 178 tests pass, including the dashboard smoke tests. `python
  dashboard/app.py` (the import path `streamlit run` uses) renders every
  tab without an error.

**Not verified**
- Clicking through the dashboard in a browser after the split.

---

## Unsaved changes in the pop-ups

**Done**
- Labeling and Review pop-ups: Previous or Next with unsaved changes no
  longer drops them silently; a warning offers Save and go on, or Discard
  and go on (product owner, option a; bug report item 4). Without changes they move
  at once. Discard clears the fields, so the email shows its saved values.
- `labeling.step_after_save`: where Save and go on lands, next or previous.

**Verified**
- 177 tests pass (2 new). Streamlit's test runner cannot click inside a
  pop-up, so the warning itself is checked by hand only.

---

## Leading zeros in the Power BI lookup

**Done**
- Like the scan search, Power BI ignores leading zeros both ways
  (product owner): 0086055559 asked finds 86055559 stored, and the reverse. The
  query sends each number as written, cleaned and without zeros (connotes
  also padded with one or two zeros; references are searched as text, so
  the zero-less form finds a padded one). `match_rows` compares without
  zeros and prefers a row with the number exactly as written (D-003 note).

**Verified**
- 175 tests pass (2 new).

**Not verified**
- On the live dataset: a connote padded with more than two zeros is not
  found (shortcut in `build_pbi_query`).

---

## Dashboard automatic-reply toggle

**Done**
- Replaced the typed send-mode confirmation with an **Activate automatic
  replies** toggle in Settings; changes apply from the next run.
- Toggle off saves replies as drafts and omits the automated-email notice.
- Toggle on sends replies without review and includes the notice. Switching
  it on asks for a confirmation first; switching it off needs none (tested
  in `test_dashboard_app.py`, against a temporary `.env`).
- Added dashboard warning/info copy, runbook and architecture updates, and
  decision D-030.

**Verified**
- Focused workflow tests cover no notice in draft mode and notice plus send
  mode when automatic replies are active.

**Not verified**
- No live Outlook send or dashboard interaction was performed.

---

## Silent Task Scheduler run

**Done**
- Documented running the scheduled task directly with `.venv\Scripts\pythonw.exe`
  and `-m app.main` so no CMD window opens.
- Console progress now safely skips output when Python is launched without
  standard output; normal console runs still show progress.
- Added instructions to change an existing task's action without replacing
  its trigger. Completed run summaries remain in `logs\run_log.txt`.

**Not verified**
- The Task Scheduler action has not been changed on the user's PC. Outlook
  still requires the task to run as the signed-in user.

---

## Outlook completion categories

**Done**
- Added `POD Agent Reply` to an email after the agent-created draft is
  confirmed sent (edited or unedited), not when it is merely created.
- Added `On List` after Flow 2 has archived every expected Outstanding
  `add` instruction for that email and none remain in `outstanding/`.
- Outlook category updates preserve all existing categories and skip
  duplicates.
- The unknown-number hold path now also records any Power BI-matched
  NOT_RECEIVED numbers in Outstanding, so the category covers every expected
  add instruction for the email.
- Documented the category timing and SharePoint-sync dependency in the
  architecture and Power Automate flow guide.
- Recorded the feature as decision D-029 in `docs/DECISIONS.md`.

**Decision**
- Treat Flow 2's successful move to `archive/` as the completion signal for
  `On List`; the category may therefore appear on the next scheduled Python
  run, not immediately when the flow completes.

**Not verified**
- Live Outlook and SharePoint behavior requires testing on the user's PC:
  send an agent-created draft, then process an Outstanding `add` through
  Flow 2 and confirm the two categories appear. `On List` also depends on the
  archive folder being visible through the local SharePoint sync.

---

## CI stale notice assertion

**Found**
- CI failed one assertion in `test_main.ReplyFlowTest` because it still
  expected "This is an automated email." after the reply note changed to
  "This email was automatically generated by the POD Email Agent."

**Done**
- Updated the workflow assertion to use `reply_generator.AUTOMATED_NOTICE`.
- Updated the pilot test fixture to build its reply from the canonical
  reply template instead of embedding the retired notice text.

**Decision**
- Tests should use the shared reply template/constant for generated wording
  so future copy edits don't leave stale expectations.

**Verified**
- Full `python -m unittest discover -s tests`: 160 tests pass on the local
  Python 3.14 environment; only the expected Streamlit bare-mode warnings
  appeared.

---

## Reply formatting and run progress

**Done**
- Direct `python -m app.main` runs print progress through mailbox setup,
  email classification, scan indexing/search, draft creation, and pending
  Power BI/team checks. Scheduled and dashboard runs remain quiet.
- Replies use the company signature with company details,
  colors, and a clickable website, replacing the personal Outlook signature.
- The automated-email note reads "This email was automatically generated by
  the POD Email Agent." and is formatted as 9pt italic gray text.
- Reply All keeps Outlook's quoted-message HTML and embedded styles so the
  trail retains Outlook formatting.

**Decision**
- Keep Outlook's native quoted trail rather than reconstructing it. Remove
  the personal reply signature before the quote; if Outlook's quote boundary
  cannot be identified, stop rather than risk creating a draft with a lost
  trail. No new product-level decision was added to `docs/DECISIONS.md`.

**Verified**
- `tests/test_outlook_reader.py`: 6 tests pass.
- `tests/test_main.py`: 43 tests pass.
- Pylance syntax checks and `git diff --check` pass.

**Not verified**
- The new HTML signature and preserved quote have not yet been checked in a
  live Outlook draft on the user's mailbox.

---

## Known gaps

### Built today but wrong or missing (found in review)

- ~~No network share search~~ Built (phase 1, `app/pod_files.py`).
- ~~Only the first found file is attached; not-found numbers dropped~~ One
  reply with every file and every number's outcome (phase 1).
- ~~Excel handoff files are not Tables~~ Replaced by JSON files (phase 1,
  D-009).
- ~~`requirements.txt` includes `torch` and `transformers`~~ Removed;
  dependencies pinned with upper bounds (phase 0).
- ~~Intent training metrics are printed, not saved~~ Every run saved to
  `models/history/` (phase 0).
- ~~Automatic model choice prefers the smallest train/test gap~~ Removed;
  the product owner chooses (phase 0).
- ~~`send_reply` sets `To` to a non-SMTP address~~ Outlook's own reply
  recipients are kept; sender resolved to SMTP (phase 1).
- ~~No guard against auto-replies, bounces or self-replies~~ Built
  (phase 1, `main.skip_reason`).
- ~~Every run iterates the whole mailbox folder~~ Only unread items, via
  `Restrict` (phase 1).
- ~~`tests/` is empty~~ 64 tests (phases 0 and 1).
- The runtime classifies intent before looking for numbers, so number-less
  emails reach an intent model that never trained on them. ~~Resolved by
  design in D-015~~ Built in phase 0; built, not measured on real email.
- `historical_pull` can write `intent_label_auto = CHECK`, which is not an
  intent class (`POD_REQUEST`, `OTHER`).
- Trained models (`models/*.pkl`) are committed. A TF-IDF vocabulary holds
  words and numbers from the training emails, so customer text is in git
  history. `models/**/*.pkl` added to `.gitignore` and untracked with
  `git rm --cached`; the published repository holds no model file.

- ~~The cold-start gate needs 200 labeled emails per class; real data
  has 150 POD requests~~ Gate lowered to 150 for the pilot (D-019).
- Regex misses 91 of 371 labeled numbers: 41 connotes are in one email
  whose short request ("please send the POD for these") is followed by its
  signature and then a forwarded email listing the numbers; the quoted
  header was not recognised, and regex stops at "Regards". Fixed by the
  whole-email fallback (D-017): recall 0.865, precision 0.598. 36
  references have shapes regex does not know; the product owner says references
  have no fixed format. 10 connotes are not in the subject or body at all.
- The master number list cannot label or teach the number model as it
  stands: 2 of 220 labeled connotes and 0 of 78 labeled references are in
  it; its reference shapes differ from those in emails; and connotes and
  references share shapes (e.g. 7 digits), so only the words around a
  number tell them apart. A model trained on master shapes scored
  precision 0.42 / recall 0.42, against regex 0.79 / 0.76. Decided: regex,
  no master training, Power BI validates (D-018).
- Intent precision at 0.90 is about 0.93-0.94 across folds, below the
  95% pilot target (D-014); one test split showed 1.000 because it held
  only 30 POD requests. More labels are the main lever.

### Designed, not built

- Dashboard on live data: figures, the labeling table with
  2,000+ rows, and every job button against Outlook. Built, not measured.
- Pilot measure (D-014): built, not measured on live Outlook (test
  checklist step 6b).
- AI wording through AI Builder (D-028): planned in the first build plan
  (removed; in the git history), off by default, with automatic fallback
  to the templates. Start after Flows 1 to 3 run and the
  template pilot has numbers.

### To confirm in production

- Phase 1 has run only against a fake Outlook and temporary folders.
  First run in draft mode: follow `docs/TEST.md`.
- `Items.Restrict("[UnRead] = True")` on the shared mailbox.
- Replies from the POD mailbox (`SentOnBehalfOfName`) need send-as or
  send-on-behalf rights; and where Outlook saves those drafts (the POD
  mailbox's Drafts or the user's).
- `sqlite3` present in the installed Python
  (`python -c "import sqlite3; print(sqlite3.sqlite_version)"`).
- Time to walk the scan folders.
- ~~"Add a row into a table" accepts a whole row as JSON~~ No longer
  needed: the guide maps columns one by one (D-007 note).
- Power BI table and column names.
- POD Outstanding column names.
- Matcher results on the last 200 real POD emails (found / ambiguous /
  missing).
- Flows 1 to 3 built from `docs/POWER_AUTOMATE_FLOW.md`, each with its
  test file; record which **Check** items needed a change.
- Two weeks in draft mode: share of drafts needing edits.
- Before pulling the commit that untracks `models/*.pkl`, copy `models\`
  aside and restore it after the pull; the pull deletes the files there.
- After pulling phase 0, no model is active, so every email with a number
  goes to manual review until models are chosen
  (`python -m app.model_registry --task intent --activate <name>`). The
  existing models keep the same file names, but were trained with
  scikit-learn 1.8; if the install upgrades scikit-learn, retrain.
  Retrain the intent model anyway after pulling D-016 (text cleaning).
  Choose `regex` for numbers
  (`python -m app.model_registry --task number --activate regex`, D-018).
- Run the test suite in production (`python -m unittest discover -s
  tests`); CI runs it on Python 3.10 and 3.13.

### Open decisions

- ~~Q65-Q69: AI wording?~~ AI Builder Run a prompt words replies and
  Teams messages; facts and first name only; Python checks the text; on
  or off in the dashboard; templates on any error; English; built after
  the template pilot (product owner, D-028).
- ~~Q51: add a PyTorch model?~~ Built, then removed after losing to XGBoost
  and regex; numbers use a CRF (D-021 note, D-026).
- ~~H: hold the whole email when a number is "Not in The System"?~~ Yes, as
  built (D-023).
- ~~Q58: which Power BI tables?~~ TMS, POD Scans and DeliveryApp
  (product owner). The query now joins them through `POD_PBI_LINKS` and returns
  `POD_PBI_COLUMNS` (D-027). Still to fill in `.env`: the real column names
  and linking columns.
- ~~Q61, Q63: what DeliveryApp holds; what the reply does~~ Those customers' POD PDFs are
  on the same share, so they are found and attached as usual; DeliveryApp
  holds the details (sender, receiver...) that fill POD Outstanding for
  them (product owner).
- ~~Q62: are those consignments also in TMS?~~ Only in DeliveryApp (product owner);
  DeliveryApp is searched as a second table (`POD_PBI_SECOND_*`, D-027).
- ~~Q64: where is such a consignment's POD status?~~ In DeliveryApp (product owner);
  mapped in `POD_PBI_SECOND_COLUMNS`, no code change.
- Q59 (on hold, after Q58): POD Outstanding column map. Columns: EMAIL
  DATE, CUSTOMER EMAIL, CONSIGNMENT, PO REF, SENDER, RECEIVER, DATE IN
  TMS, COMMENTS, SENT TO DEPOT DATE, POD_STATUS.
- ~~The agent's Python version~~ 3.14 (product owner); CI tests 3.13 and 3.14.

- Q42: keep SQLite for tracking, or local CSV.
- ~~Q43: attachment limit~~ 20 MB accepted by the product owner.
- ~~Q44: Teams message for Manual Review / Other Intent?~~ No: dashboard
  and unread status only during the pilot (product owner).
- Success-measure targets other than the pilot exit criterion (PRD Q-A).

### Docs not yet written

- ~~`docs/RUNBOOK.md`~~ Written; `handoff.md` retired.

### Repository hygiene

- ~~No `.gitattributes`~~ Added: `* text=auto eol=lf`; `*.pkl`
  and `*.xlsx` binary.

---

## Company signature out of the repository

**Done**
- The reply signature with the company's name, address, phone and
  website moved from `reply_generator.py` to an HTML file named by
  `POD_SIGNATURE_FILE` (`config.signature_html`), kept outside git; empty
  gives a plain "Kind Regards, POD Teams". A missing file or `<...>` stops
  the run before any email. Tests use a made-up company and person.

**Verified**
- 167 tests pass (2 new).

**Not verified**
- In production: copy `data\signature.html` there and set
  `POD_SIGNATURE_FILE=data\signature.html` in `.env`.

---

## Tests for the untested paths

**Done**
- Measured line coverage with the standard `trace` module (no new
  package). Lowest before: `main._cycle` never ran, the dashboard had no
  test, `outlook_reader` 24%, and neither `generate_pseudo_labels` ever
  labeled a row.
- `test_main.CycleTest`: a whole `_cycle` with a fake Outlook: an email is
  replied, one that fails is recorded as Error and still marked processed,
  a Reviewed email is continued in the same run, and an unreadable share
  leaves new and Reviewed emails for the next run.
- `test_outlook_reader.py`: only unread, unprocessed emails are read, the
  newest message is split from the quoted history, internal senders get
  their SMTP address; draft mode saves and never sends, send mode sends,
  an unknown mode is refused before Outlook is touched.
- `test_training.py`: intent pseudo-labels fill only unlabeled rows and
  keep manual labels, agent decisions and other columns; number
  pseudo-labels fill rows without a manual intent.
- `test_dashboard_app.py`: every tab renders with data in Streamlit's test
  runner, and the review pop-up opens.

**Verified**
- 153 tests pass (11 new). Coverage now: `main.py` 94%, `outlook_reader.py`
  48% (the rest is folder walking and Sent Items), `classifier.py` 88%,
  `number_crf.py` 98%. The dashboard test runs a copy of `dashboard/app.py`,
  so `trace` cannot attribute its lines.

---

## Customer scan folders (bug report item 17)

**Done**
- `POD_SENDER_FOLDERS=domain=folder;...` (`config.sender_folder`): the
  folder is a subfolder of each year folder (product owner); subdomains count.
- `pod_files.search(..., folder)`: for such a sender only files in the
  folder count; a match only elsewhere is AMBIGUOUS (`outside_folder`),
  and the Teams card says it is not in the folder.
- A malformed or `<...>` value stops the run before any email is read.
- `.env.example`, runbook, test checklist, ARCHITECTURE, D-003 note.

**Verified**
- 142 tests pass (3 new).

**Not verified**
- On the live share: fill in the customer's domain and folder name.

---

## XGBoost overfitting measured (bug report item 16)

**Done**
- Nine settings measured against the current ones, same 5-fold CV on
  the updated labels (1,587 emails, 472 POD); D-024 note. No change: the
  current settings score best on the test folds, the regularised ones
  only narrow the train/test gap.

**Verified**
- Current settings: test macro F1 0.898; at 0.95, precision 0.965 and
  58% of POD requests handled (with 150 POD labels it was 45%). At
  0.88, 70% handled at 95% precision.

**Not verified**
- The saved intent model is older than these labels: retrain, then
  read precision and coverage per threshold in the Models tab.

---

## History of every processed email (bug report items 10, 11)

**Done**
- Dashboard tab History: every email in tracking, filters by status,
  outcome, sender and email date, newest first.
- Open one: the stored email, and its processing (statuses with time
  and reason, files, Power BI, review, attachments, draft outcome,
  reply text); whether it is already in the training data.
- Add to training data with an intent and numbers the person chooses,
  label source `Manual`, by entry ID (product owner). Emails skipped for no
  number can be labeled OTHER or POD_REQUEST; guard emails cannot be
  added (product owner: the model never sees them).

**Verified**
- 139 tests pass (3 new). The tab and the pop-up open in Streamlit's
  test runner on a temporary tracking file.

---

## Reviews into the training data (bug report items 9, 12)

**Done**
- Every Save in the review pop-up also writes the email into
  `training_data.xlsx` (`training_refresh.add_review`), matched by entry
  ID: intent, connotes and references become its manual labels, label
  source `Manual review`, with `labeled_at`. An existing row keeps its
  other columns; a new row gets the email and the agent's view, like
  Pull new data. An earlier label is replaced and the pop-up says so
  (product owner: automatic on Save; its own source; the review wins).
- Labeling tab: "Labeled by" has Manual review.
- If the file cannot be written (open in Excel), the review stays saved
  and the pop-up says to label the email in the Labeling tab.

**Verified**
- 137 tests pass (2 new): one row per entry ID on a second save, Pull
  new data does not add it again, an earlier label is replaced with the
  other columns kept, the row counts for training.

---

## Review of unsure emails (bug report items 1, 3-7, 14)

**Done**
- Review queue: "Review one by one" pop-up for Manual Review and Other
  Intent emails: intent, connotes and references, Previous / Save / Next
  (Next does not save; Save moves on).
- `main.record_review`: POD_REQUEST -> `Reviewed` with the numbers;
  OTHER -> `Reviewed - Other` (counts as not a POD request, nothing
  sent). Refused unless the email is still reviewable, and a POD request
  needs a number.
- The cycle continues `Reviewed` emails first (`_resume_reviewed`) with
  the person's numbers: scan search, then the usual Power BI request
  (`pbi_requests/`, same contract), Teams or reply. A share that cannot
  be read leaves them `Reviewed` for the next run (product owner: the agent
  continues, not the dashboard; connotes and references).

**Verified**
- 135 tests pass (5 new): reply with a file on the share, Power BI
  request in the existing format and only once, OTHER sends nothing,
  saving twice or without a number records nothing. The pop-up opens in
  Streamlit's test runner (Save disabled until an intent is chosen).

**Not verified**
- On live Outlook and Flow 1.
- Training data: done in the next section (item 9).

---

## Bug report items 2, 15, 13 and 8

**Done**
- Review queue: Email date column (received time as Outlook shows it).
- Labeling: every manual label records `labeled_at`; filter Labeled
  between, sort by label date, Labeled at column. Older labels have no
  date. The table's unsaved edits are cleared after each save.
- Scan search: leading zeros either way; an exact file wins over
  zero-padded copies; a number inside a longer one stays ambiguous
  (D-003 note).
- Review queue: Mark resolved and Process again, each with a
  confirmation (product owner: keep the history, answer 1a). Process again is for
  Manual Review, Other Intent, Held and Error; it adds a `Reset` event and
  takes the email out of `processed_ids.txt` under the run lock. Archived
  handoff files of an email processed again get a number instead of
  overwriting the earlier ones.

**Verified**
- 130 tests pass. The Review queue's Process again and Confirm were
  driven in Streamlit's test runner on a temporary tracking file.

**Not verified**
- On live Outlook: an email processed again is read again only while it
  is unread; the Email date against the time Outlook shows.

---

## Review: sender feature and master list

**Done**
- Intent model: the sender is `internal` or its domain
  (`classifier.sender_feature`, D-016 note). Training rows had internal
  senders as Exchange paths, the live agent as SMTP addresses.
- Historical pull stores the SMTP address (`outlook_reader.smtp_address`).
- Master number list: read from `.xlsx` or `.csv`; the default is now
  the master list file that exists. The pull had
  been skipping it (it looked for a `.csv`) and used regex alone.
- Training and pseudo-labeling stop when `POD_SHARED_MAILBOX_ADDRESS` is
  unset, since the sender feature needs its domain.

**Verified**
- 122 tests pass (4 new). Same 5-fold CV on the labeled data: POD F1
  0.775 to 0.790, precision at 0.95 unchanged (0.971), coverage at 0.95
  unchanged (0.447).
- Measured, not adopted (differences of 3 to 10 of 150 POD emails, within
  noise): POD class weights (precision at 0.95 falls to 0.904); sublinear
  TF and min_df 2 (coverage 0.473); character n-grams (precision 1.000,
  coverage 0.433; 0.540 at threshold 0.87).

**Not verified**
- The live gain: CV cannot show the mismatch the fix removes; compare the
  pilot's manual-review share before and after retraining.

---

## Labeling pop-up

**Done**
- Labeling tab: "Label one by one" opens a pop-up with the email
  (subject, sender, received, body with numbers highlighted, the agent's
  decision and confidence) and its intent, connotes and references.
  Previous and Next move through the filtered rows, starting at the first
  selected row; Save writes that email and marks it Manual.
- The button is disabled while the table holds unsaved edits, so the two
  cannot overwrite each other (`labeling.has_label_edits`).
- The pop-up shows the pseudo-labels (intent with confidence, connotes,
  references) as a recommendation; "Use recommendation" fills the fields
  and Save stores them as Manual. Blank cells no longer show as "nan"
  (saving would have stored the text "nan").
- Jobs tab: "Number pseudo-labels" runs the CRF's pseudo-labeling.

**Verified**
- 120 tests pass (3 new). The pop-up opens on fake data in Streamlit's
  test runner.

**Not verified**
- Previous, Save and Next in a real browser (the test runner cannot click
  inside a pop-up).

---

## Consignments from a second table (DeliveryApp)

**Done**
- `POD_PBI_SECOND_*` (`config.pbi_second_table`): the query also searches DeliveryApp and
  adds its rows under TMS's with `UNION`, same column names, so those
  numbers are no longer "Not in The System" and their Outstanding row
  gets DeliveryApp's sender, receiver, date and POD status (D-027).
- DeliveryApp link dropped from `POD_PBI_LINKS` in `.env.example`; runbook,
  test checklist, ARCHITECTURE and HANDBOOK updated.
- A Power BI name still left as a `<...>` placeholder from `.env.example`
  is refused with the setting named, as the docs said.
- Settings step keeps an existing `.env` as `.env.old` before copying
  `.env.example`.

**Verified**
- 116 tests pass (3 new).

**Not verified**
- The query on the live dataset (checklist step 3, results item 8).

---

## Power BI over several tables

**Done**
- `POD_PBI_LINKS` and `POD_PBI_COLUMNS` (`config.pbi_links`,
  `config.pbi_columns`): the DAX query filters the main table and brings
  columns from linked tables with `MAXX(FILTER(...))`, independent of the
  model's relationships (D-027). Wrong or unlinked names are refused with
  the setting named.
- `.env.example`: every setting, with TMS, POD Scans and DeliveryApp laid
  out and `<...>` for the real column names; the POD Outstanding map laid
  out for its ten columns (on hold, Q59).
- Test checklist and runbook use it; a step runs the generated query in
  Power BI before Flow 1.

**Verified**
- 113 tests pass (3 new).

**Not verified**
- The query on the live dataset (checklist step 3).

---

## Train vs test for the number model

**Done**
- Number training prints, and the Models tab shows, the saved CRF's
  per-email scores on its own training emails next to the cross-validated
  scores and regex, plus the per-token macro F1 gap, with a note on reading
  over- and underfitting. Stored as `emails_train` in the history.

**Verified**
- 110 tests pass. On the labeled data: CRF train 131 of 150 emails fully
  right (precision 0.980, recall 0.930), test 114 (0.920, 0.895), regex 96;
  per-token macro F1 train 0.983, test 0.904.

---

## Runbook

**Done**
- `docs/RUNBOOK.md`: requirements, install, settings, training and model
  choice, running and scheduling (`run_agent.cmd` for Task Scheduler),
  daily use, stop and undo, problems, data and security. `handoff.md`
  removed: its useful parts moved here, the rest was outdated (it still
  told to run `python -m app.main --help`, which now starts a cycle).
- `.gitattributes` keeps `.cmd` files in CRLF.

**Not verified**
- The Task Scheduler commands and `run_agent.cmd` have not been run here.

---

## Pilot measure

**Done**
- `app/pilot.py` and `main._check_drafts`: each run finds, for every
  undecided draft, the first reply sent after it in the same conversation
  (POD mailbox's and user's Sent Items) and records sent unedited, sent
  edited or deleted. `outlook_reader.reply` now returns the draft's entry
  ID and conversation ID; new `draft_exists` and `sent_replies_since`.
- Dashboard: pilot row in Overview, and in Settings next to the send mode.
- D-014 note; HANDBOOK lists the Outlook behaviour this relies on
  (unverified); test checklist step 6b checks it.

**Verified**
- 110 tests pass (9 new), with fake Outlook; dashboard loads with drafts in
  every state.

**Not verified**
- Live Outlook: where sent-on-behalf replies land, conversation IDs, and
  the date conversion (step 6b).

---

## Training history kept when runs share a clock time

**Found**
- The first CI run on Python 3.10 failed one test: two training runs saved
  close together got the same file name, because Python 3.10 on Windows
  reads the clock in about 15 ms steps (3.13 is far finer), so the second
  overwrote the first. The agent ran on Python 3.10 then.

**Done**
- `model_registry.save_history` moves a clashing time on by a microsecond
  until the name is free; names still sort oldest first.
- New test with a frozen clock; it fails without the fix. 101 tests pass.

---

## CRF number model; PyTorch removed

**Done**
- PyTorch removed entirely: code, tests, optional requirements, CI step
  (D-021 note).
- `app/number_crf.py`: CRF tagging connote / reference numbers; training
  cross-validates it against regex by email and stores per-email scores;
  pseudo-labels with the active CRF. `number_classifier_sklearn.py`
  removed. Number choices are `crf` and `regex` (D-026). Default number
  threshold 0.5. `sklearn-crfsuite` added to requirements.
- Models tab shows the per-email comparison for numbers.
- Dates removed from this log (product owner).

**Verified**
- 100 tests pass (about 3 s), including CRF learning, extraction, saving
  and the fallback to regex.
- App training on a copy of the labeled data, read-only: CRF 114 of 150
  emails fully right, precision 0.920, recall 0.895, no request without a
  number; regex 96, 0.598, 0.865. Same as the experiment.
- `sklearn-crfsuite` wheels exist for Python 3.10 and 3.13 on Windows.

---

## Intent models narrowed; "Not in The System"

**Done**
- Intent trains `xgboost` and `logreg` only (D-024); SVM and random
  forest code removed from intent. Dashboard colours keep older runs'
  models apart.
- Numbers not in Power BI go to POD Outstanding as "Not in The System"
  and the team is told to check them (D-023); a fact added to HANDBOOK.
- Regex combined with the XGBoost number model, 5 folds by email: union
  adds at most 4 numbers; filtering out tokens XGBoost calls OTHER keeps
  precision up only by dropping most real numbers (recall 0.49 at best).
  Regex stays alone.
- Tuning comparison (tuned random forest vs tuned XGBoost) stopped by the
  product owner after 3 of 5 folds; no result.
- Default intent threshold 0.95 (D-025); still set in the dashboard.
- 101 tests pass (about 10 s).

---

## Cross-validated training; model comparisons

**Done**
- Training scores every model with 5-fold cross-validation and fits the
  saved model on all data (D-022); number folds split by email.
  `model_registry.cross_validate` replaces the single split; the
  dashboard explains how a run was evaluated.
- PyTorch removed from the intent candidates (product owner); numbers only.
- 3 new tests, including one proving no example is scored by a model
  that saw it. 100 tests pass.

**Measured** (real data, read-only)
- Intent, 5-fold CV: soft voting (logreg+svm+rf) F1 0.740, all four
  0.773, XGBoost 0.773; at equal precision voting and XGBoost cover the
  same share, so voting is not added. XGBoost at threshold 0.90:
  precision 0.943, coverage 0.547; at 0.95: 0.971 and 0.447.
- Numbers, 150 POD requests, 5 folds by email: regex precision 0.598,
  recall 0.865, 96 emails exactly right; XGBoost number model identical
  (never confident, regex always used); PyTorch with regex fallback
  0.377 / 0.709, 77 emails; PyTorch alone 0.373 / 0.458; regex plus
  PyTorch 0.416 / 0.903, 81 emails. PyTorch number training took about
  5 minutes per fit (xgboost under 1).

---

## PyTorch candidate model

**Done**
- `app/torch_model.py`: `torch_mlp`, trained from scratch, in both
  intent and number training; optional `requirements-torch.txt`; CI
  installs it. Unknown model names are now refused.
- HANDBOOK and D-001 corrected: PyTorch installs, Hugging Face is
  blocked. D-021 records the choice.
- Notes reworded so they describe production and the test
  environment without naming where work was done.

**Verified**
- 97 tests pass with PyTorch 2.14.1 (7 new); a PyTorch wheel exists for
  Python 3.10 on Windows.
- Real labeled data, read-only: results in D-021.

---

## CI and merge into main

**Done**
- The three GitHub workflow templates added on `main` (Python package,
  Pylint, SLSA generator) failed: they ran on Linux, where `pywin32`
  cannot be installed and `msvcrt` does not exist; Pylint had none of the
  project's libraries and failed on any style message; SLSA was an unused
  example with write permissions. Replaced, with the product owner's approval,
  by `.github/workflows/tests.yml`: Windows, Python 3.10 and 3.13,
  install, compile, unit tests.
- `requirements.txt`: scikit-learn `>=1.7` (was `>=1.8`), since 1.8+ does
  not exist for Python 3.10.
- Phases 2 and 3 merged into `main` (not committed; the product owner commits).
  From now on work is on `main`, without branches per phase.

**Verified**
- Every requirement resolves for Python 3.10 on Windows (pip download):
  scikit-learn 1.7.2, pandas 2.3.3, numpy 2.2.6, xgboost 3.2.0,
  streamlit 1.64.0.
- All 31 Python files parse as Python 3.10.
- 90 tests pass on the merged tree (Python 3.13).

**Not verified**
- The tests on Python 3.10: first run is the GitHub workflow after the
  push.

---

## Phase 3 (branch `phase-3-dashboard`, from `phase-2-flows`)

**Done**
- Dashboard rewritten with six tabs (ARCHITECTURE section 7): Overview
  with the seven charts of D-012, Review queue with "mark resolved",
  interactive Labeling (editable table, filters, bulk, confirm, number
  highlighting), Models (metrics, confusion matrix, threshold precision
  and coverage, history), Jobs with live progress, Settings.
- New modules: `dashboard_data.py`, `labeling.py`, `jobs.py`,
  `training_refresh.py`; `model_registry.at_threshold`;
  `config.save_setting` / `current_setting`; tracking keeps
  `to_recipients` and a `Resolved` status.
- `historical_pull` and pseudo-labeling keep `label_source`; pseudo-
  labeling no longer overwrites System decision rows.
- `.streamlit/config.toml`: dashboard on `localhost` only.
- D-020 records answers 45-48.

**Verified**
- 90 tests pass (24 new), no warnings.
- Every tab rendered in Microsoft Edge (the browser in use), light and
  dark, with seeded data; fixed what the screenshots showed (thin day
  bars, vertical labels, decimal counts, unreadable dark confusion
  matrix, "None" and "nan%" in the threshold table, raw column names).

**Found**
- Streamlit listens on every network address by default: anyone on the
  network could open the dashboard. Now limited to this PC.

**Not verified**
- Live data and Outlook (see "To confirm in production").

---

## Phase 2 (branch `phase-2-flows`, from `phase-1-core`)

**Done**
- `docs/POWER_AUTOMATE_FLOW.md`: step-by-step build of Flows 1 to 3,
  with the JSON each flow reads and writes, Parse JSON schemas,
  expressions, and a test file per flow.
- Python now writes the ready Teams adaptive card for a choice
  (`handoff.choice_card`), so Flow 3 only posts it; 2 new tests (66).

**Not verified**
- No flow has been built or run. Points that depend on the tenant are
  marked **Check** in the guide, each with how to confirm it: the shape
  of *Get file content* output, Excel filter syntax for column names
  with spaces, and the answer fields of the Teams card action.

---

## Phase 1 (branch `phase-1-core`, from `phase-0-cleanup`)

**Done**
- `app/pod_files.py`: cleaning rule, scan index, FOUND / AMBIGUOUS /
  MISSING, attachment path check.
- `app/handoff.py`: JSON files for Flows 1 to 3 (DAX built in Python,
  written via `_tmp/` then moved in, consumed files archived).
- `app/tracking.py`: SQLite status log, status names.
- `app/main.py` rewritten as a per-email state machine: guard, numbers,
  intent, scan search, Power BI, Teams cards, one reply, mark read,
  Outstanding add/remove, `STOP` file, run lock, attachment size hold.
- `app/outlook_reader.py`: unread only, SMTP sender, message class,
  reply with several files as draft or send, mark read.
- `app/reply_generator.py`: one combined template.
- Removed `approval_handler.py`, `pod_checker.py`, `follow_up.py`
  (Teams approval replaced by draft mode, D-008; follow-ups dropped,
  D-007).
- Dashboard reads tracking from SQLite; "Need a person" metric.
- ARCHITECTURE, README, handoff.md, HANDBOOK updated.

**Verified**
- 64 tests pass (44 new), including end to end: all found; mixed found
  and not received; unknown held; Power BI error held; ambiguous ->
  card -> file; team chooses manual; answer outside the options
  refused; attachments over the limit held; wrong column map fails
  before any reply; `STOP`; second run while one runs.
- Dashboard loads without error.

**Not verified**
- Real Outlook, network share, SharePoint sync and the flows (see "To
  confirm in production").

**Changed from the plan**
- No "since last run" mail window: filtering unread items already
  catches up after the PC was off, and needs no date format.

---

## Real-data check and text cleaning (same branch)

**Done**
- Trained on a copy of the labeled training data, read-only (hash checked
  before and after; models written to a temporary folder).
- Measured regex number extraction against the manual labels.
- Compared four text-cleaning variants with 5-fold CV; built the chosen
  one (D-016) with 7 tests.
- Tested a number model trained on the master list (D-018: not used).
- Built the forwarded-request fallback (D-017) with 4 tests; cold-start
  gate 150 (D-019). 31 tests in total.

**Measured** (real data, 1,094 labeled emails, 150 POD requests)
- Intent, one 80/20 split, after cleaning: XGBoost POD F1 0.815
  (0.755 before), 16 of 30 POD requests at >= 0.90, none wrong. Other
  models: SVM 0.702, random forest 0.667, logistic regression 0.629.
- Number model: best CONNOTE F1 0.395 (XGBoost), REFERENCE 0.18 at best.
- Regex on the 150 POD requests: right on 96 emails; number precision
  0.791, recall 0.755; finds no number in 1 POD request (the D-015 risk).
  Regex is clearly better than the number model today.
- Full training of all eight models on real data: about 50 s.

---

## Phase 0 (branch `phase-0-cleanup`)

**Done**
- Removed PyTorch: code paths, `--backend pytorch`, `--number-model`,
  and the settings `POD_NUMBER_CLASSIFIER_BACKEND`,
  `POD_NUMBER_EXTRACTION_MODE`, `POD_NUMBER_CLASSIFIER_MIN_F1`.
- New `app/model_registry.py`: the product owner chooses the active model per
  task (`models/active.json`); every training run is saved to
  `models/history/` with train and test metrics, confusion matrices, and
  each test example's prediction and confidence. Automatic choice and
  `training_metadata.json` are gone.
- Number extraction keeps REFERENCE as well as CONNOTE, and training
  and prediction share one candidate-context function (it was copied).
- `main.py` looks for numbers first; an email without one is
  `Skipped - No Number` (D-015).
- Dashboard sidebar chooses the active models.
- `requirements.txt` pinned with upper bounds.
- Tests: `tests/test_model_registry.py`, `test_training.py`,
  `test_main.py` (20 tests, unittest, no Outlook needed).
- Updated README, ARCHITECTURE, handoff.md.

**Verified**
- `python -m compileall app dashboard` clean.
- 20 tests pass in about 3 s; no warnings with `-W default`.
- Dashboard loads without error (Streamlit `AppTest`); sidebar shows
  both model choices.

**Not verified**
- Outlook, SharePoint and Power Automate parts (not covered by the automated tests).
- Training on real labeled data.
- The Python and library versions in production.

**Found**
- The committed models were trained with scikit-learn 1.8.0 and warn
  when loaded with 1.9.

---

## Review and plan

**Done**
- Read the codebase: Outlook reader, intent and number classifiers, Excel
  handoff, reply templates, follow-up, dashboard, training scripts, saved
  model metrics.
- Agreed the target workflow with the product owner in 30 questions (plus the pilot exit criterion); recorded as
  D-001 to D-061 in `docs/DECISIONS.md`.
- Wrote `docs/ARCHITECTURE.md` (current code), `docs/PRD.md` (replacing an
  earlier spec), a first build plan (since removed), `docs/HANDBOOK.md` and
  this file.

**Found**
- The gaps listed under "Built today but wrong or missing".
- The number model is weak (best CONNOTE F1 0.38, REFERENCE 0.17), so regex
  does the extraction in practice.

**Verified**
- Nothing run yet.

**Not changed**
- No code changed.
