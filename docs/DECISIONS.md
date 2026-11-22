# Decisions

Numbered decisions for the POD email agent. A decision is never changed
silently: add a dated note under it saying what changed and why. Answered
questions are struck through, not deleted.

---

## D-001 Where the agent runs and who does what

*Agreed with the product owner.*

**Context.** The PC running the agent has Outlook Desktop but no Outlook API access,
and cannot install PyTorch. POD scans sit on a network share; the Power BI
dataset, the POD Outstanding workbook and Teams are only reachable through
Power Automate. The agent may later be handed to IT to run on a server.

**Decision.**
- The agent runs on a PC in the office and works only while the PC is on.
- Python does: reading and replying to email (Outlook Desktop), intent and
  number classification, searching the network share, all business rules.
- Power Automate (cloud) does only what Python cannot reach: querying the
  Power BI dataset, writing the POD Outstanding workbook, posting to Teams.
- Models are scikit-learn or regex only; PyTorch code and requirements are
  removed.

**Consequences.** Email arriving while the PC is off waits until the next
run (see D-002). Moving to a server means replacing Outlook Desktop with
another way to reach the mailbox (Outlook automation is not supported on
servers); keeping all Outlook access in `app/outlook_reader.py` limits that
change to one file. On a server, Power BI can be queried directly, retiring its flow.

*Note:* the product owner corrected the context: PyTorch **can** be
installed; Hugging Face cannot be reached. The removed PyTorch model
fine-tuned a pretrained model downloaded from Hugging Face, so it still
cannot run. Whether to add any PyTorch model stayed open (D-021 answers it); until
then this decision stood.

*Later note:* the PyTorch candidate was built (D-021), lost to XGBoost for
intent and to regex for numbers, and was removed; numbers use a CRF
(D-026).

**Open questions.** None.

---

## D-002 Which emails are processed

*Agreed with the product owner.*

**Context.** Requests arrive mostly in the morning, up to one every
15 minutes, some at night while the PC is off. Colleagues sometimes handle
a request by hand before the agent sees it.

**Decision.**
- Process only emails that are **unread** in Outlook and not yet processed
  by the agent.
- Run every 5 minutes. The mail window starts at the last successful run
  minus a margin, so the first run after the PC starts catches up.
- A dashboard button runs the same cycle on demand.
- After a reply is drafted or sent, mark the email read. Held emails stay
  unread.
- Skip the POD mailbox's own messages, auto-replies, bounces and no-reply
  senders, to prevent reply loops.

**Consequences.** If the reading pane marks an email read before the agent
runs, the agent skips it silently. Mitigation: in Outlook, File -> Options
-> Mail -> Reading Pane, turn off "mark as read when viewed" or set a delay
of several minutes.

**Open questions.** None.

---

## D-003 Finding POD scans on the network share

*Agreed with the product owner.*

**Context.** Scans are PDFs named inconsistently, e.g. `12345_absd.pdf`,
`AB-10234_102cd_x1y2345.pdf`. Separate root folders per year (2025, 2026,
2027 next year), each with nested subfolders, about 10,000 files a year.

**Decision.**
- Root folders are a setting (list); all subfolders are searched; PDF only.
- Clean both the requested number and the file name (remove spaces, `-`,
  `,`, `.`; upper-case), then match when the file name contains the number.
- One match: FOUND. More than one match: AMBIGUOUS (handled by D-006).
  No match: look up in Power BI (D-004).

**Consequences.** Contains-matching means `12345` also matches
`123456_x.pdf`; that case is caught as AMBIGUOUS, not guessed. *(Replaced
by D-042: a file matches only a whole number in its name.)* The share is
walked once per run; the walk time is measured in production before
deciding whether an index cache is needed.

*Note (bug report, item 13):* leading zeros. A number also matches files
that write it with more or fewer leading zeros (`89855504` and
`0089855504`, either way round), but only when no file has the number
exactly; an exact file wins over its zero-padded copies. A number inside a
longer one stays a candidate, so that case is still AMBIGUOUS. The Power BI
lookup follows the same rule (product owner): the query sends each number as
written and without its leading zeros (and a connote padded with one or two
zeros), and `handoff.match_rows` compares without leading zeros, preferring
a row with the number exactly as written.

*Note (bug report item 17):* customer folders. Some customers' scans sit in
their own subfolder of each year folder. `POD_SENDER_FOLDERS` maps a sender
domain (and its subdomains) to that folder, in `.env` so customer names stay
out of the code (product owner). For such a sender only files in the folder count;
a file that matches only elsewhere is never sent without the team: it goes
to the Teams choice card, which says it is not in the folder.

**Open questions.** None.

---

## D-004 Power BI lookup for numbers without a scan

*Agreed with the product owner.*

**Context.** A number without a scan may be real but not yet received, or
not a real number (typo or wrong extraction). The Power BI dataset holds
connote and reference in separate columns, plus a POD status (Missing POD,
Not Received, ...).

**Decision.**
- Python builds the DAX query; a new, logic-free Power Automate flow runs it
  and returns the rows.
- Number in the dataset: NOT_RECEIVED, with its status and details.
- Number not in the dataset: UNKNOWN (handled by D-006).
- Dataset table and column names are settings.

**Consequences.** All matching rules stay in Python and are testable
offline. The existing Power BI flow is left as is.

**Open questions.** None.

---

## D-005 Reply content

*Agreed with the product owner.*

**Decision.** One reply per email. Every found file is attached;
not-received numbers are listed as "not received yet, we will send it once
received". The greeting is "Hi,". Every reply says it is an automated
email. Template text lives in one module and is in English.

**Consequences.** A request that is partly found gets one complete reply,
not two.

**Open questions.** None.

---

## D-006 Holding emails and notifying the team

*Agreed with the product owner.*

**Context.** A wrong automatic reply to a customer costs more than a late
one.

**Decision.**
- If any number in an email is AMBIGUOUS or UNKNOWN, hold the **whole**
  email; do not reply.
- Notify the team in the Teams group chat through Power Automate, with the
  details. Held emails also appear in the dashboard review queue.
- For AMBIGUOUS, the Teams message is an adaptive card offering each file,
  all files, or "handle manually". On a file choice the agent continues and
  replies; on "handle manually" it stops.

**Consequences.** A new flow posts messages and cards and writes the answer
back for Python to pick up.

**Open questions.** None.

---

## D-007 POD Outstanding list

*Agreed with the product owner.*

**Context.** The POD Outstanding workbook is in SharePoint, not synced to
the PC, already an Excel Table used by Power Automate. Connote and
reference are separate columns. It is checked by the team or another
system.

**Decision.**
- The agent only acts on the current state; it sends no follow-up emails.
- For each requested number:
  - scan found now: send it, and remove the number from Outstanding if
    present;
  - NOT_RECEIVED: reply "not received yet", and add the number to
    Outstanding only if not already present.
- Python sends add/remove instructions; a Power Automate flow checks for an
  existing row (connote column, then reference column) and adds or deletes.
- Outstanding column names and the mapping from Power BI fields to them are
  settings, filled in when known.

**Consequences.** Python never reads the workbook. Whether "Add a row into
a table" accepts a whole row as JSON must be tried in production; if not,
columns are mapped one by one in the flow.

*Note:* the flow guide (`docs/POWER_AUTOMATE_FLOW.md`) maps
the columns one by one as the main way, since it always works; the
whole-row JSON is an option only. Python still builds `row` from
`POD_OUTSTANDING_COLUMNS`, and the flow reads each column from it by
name, so the column map stays the one place that decides the values.

**Open questions.** Outstanding column names (to be supplied).

---

## D-008 Pilot in draft mode

*Agreed with the product owner.*

**Decision.**
- During the pilot, replies are saved as Outlook drafts; a person reviews
  and sends them. A setting switches to direct sending after the pilot.
- The Teams approval flow and `app/approval_handler.py` are removed.
- A `STOP` file in the project folder stops all drafting and sending.

**Consequences.** Nothing reaches a customer without a person until the
pilot numbers are accepted. The old approval files and flow stay untouched
until then.

**Open questions.** None.

*Note:* The product owner requested a dashboard toggle to
activate automatic sending. Off keeps replies as drafts; on sends them
without review from the next run. The automated-email notice is omitted in
draft mode and included in send mode. See D-030.

---

## D-009 Handoff to Power Automate through JSON files

*Technical proposal; the product owner did not object.*

**Context.** The current handoff writes Excel files with `pandas.to_excel`.
That produces a plain sheet, not a Table, so Power Automate's Excel
connector cannot read it. It also rewrites the whole file, which clashes
with SharePoint sync.

**Decision.** One small JSON file per message in a synced SharePoint folder
(`handoff/`), with one subfolder per direction. Consumed files move to
`handoff/archive/`.

**Consequences.** No locks, no Tables, no sync clashes; raw messages are
kept. `app/pod_checker.py` is replaced.

**Open questions.** None.

---

## D-010 Tracking in local SQLite

*Technical proposal; the product owner did not object.*

**Context.** `tracking.xlsx` is rewritten on every status event and sits in
a synced folder.

**Decision.** Tracking moves to `data/tracking.db` (SQLite, Python
standard library), append-only: one row per status event, latest wins.

**Consequences.** No sync clash, no extra install. The file is local to the
PC, so it is not shared through SharePoint.

**Open questions.** None.

---

## D-011 Model training and choice

*Agreed with the product owner.*

**Context.** Intent metrics are printed but not saved. Automatic model
choice prefers the smallest train/test gap over the best test score. The
number model is weak (best CONNOTE F1 0.38), so regex does the work today.

**Decision.**
- Every training run saves, per model, its metrics, confusion matrix, and
  test-set labels and probabilities, as dated history.
- The product owner chooses the active model per task (intent, numbers) in the
  dashboard; `regex` is a choice for numbers. Automatic choice is removed.
- The dashboard shows POD_REQUEST precision and automatic coverage at the
  confidence threshold; the threshold is set in the dashboard.

**Consequences.** Model choice is a deliberate act, visible in history.

**Open questions.** None.

---

## D-012 Dashboard scope

*Agreed with the product owner.*

**Decision.** Streamlit, with tabs: Overview, Review queue, Labeling,
Models, Jobs, Settings.
- Labeling: labels edited in the table (dropdowns), search and filters,
  bulk labeling, found numbers highlighted in the email body.
- Jobs: buttons for historical pull, pull new data, training and run once,
  with visible progress, sharing one run lock with the scheduler.
- Overview charts: KPI tiles; daily volume by outcome; outcome per
  requested number; weekly hit rate; system-decision agreement; top
  requesting domains; review queue age.

**Consequences.** Long jobs block the dashboard session while running;
progress keeps that visible.

**Open questions.** Further labeling features may be added later.

---

## D-013 Refreshing the training data

*Agreed with the product owner.*

**Decision.** A "Pull new data" button brings in:
- (a) emails the agent processed, pre-labeled with its decision and marked
  **System decision**;
- (b) new mailbox emails never processed, unlabeled.

A label the product owner changes is marked **Manual**. The product owner then retrains
and chooses the model for future runs.

**Consequences.** The share of System decision labels left unchanged
measures the model in production (Overview chart).

**Open questions.** None.

---

## D-014 Pilot exit criterion

*Agreed with the product owner.*

**Context.** D-008 keeps replies as drafts until the pilot is accepted; the
acceptance level had to be set.

**Decision.** Switch from drafts to direct sending when at least **95%** of
the agent's drafts are sent without edits over the two-week pilot.

**Consequences.** The agent must measure it: a draft counts as unedited
when the sent reply's body matches the draft it saved; a draft deleted or
never sent counts as edited. The draft text is kept in tracking for this.
(Since D-052 and D-053: only the PDFs are compared, so the text may be
changed; a draft still waiting counts once it is sent or deleted; and the
target is 90%.)

*Note:* built as `app/pilot.py`. The sent reply is the first one in the
same conversation sent after the draft was made, looked for in the POD
mailbox's and the user's Sent Items. "Matches" means the draft's text is
still in it unchanged (spacing aside; a signature Outlook adds below is not
an edit) and the attachments are the same. The share is of all drafts, so a
draft still waiting counts against it. Shown in the Overview and Settings
tabs.

**Open questions.** None.

---

## D-015 No number, no POD request

*Agreed with the product owner.*

**Context.** `historical_pull` keeps only emails in which a number is
found, so the intent model never trains on number-less emails. Today the
runtime classifies intent first and only then looks for numbers.

**Decision.** An email with no connote or reference number is not a POD
request. Numbers are extracted first; an email without any is skipped
(logged, not replied, left unread). The intent model runs only on emails
that contain a number. Training data keeps excluding number-less emails.

**Consequences.** Training and runtime see the same kind of email, so the
intent metrics describe what the agent really meets. A number the
extractor fails to recognise makes a real request look number-less, and
it is skipped silently; the pilot measurement of extraction on real
emails (plan section 5) must report how often that happens.

**Open questions.** None.

*Replaced by D-044 (product owner):* the intent model now runs first; a POD request
with no number in its text has its attachments read.

---

## D-016 Text cleaning for the intent model

*Requested by the product owner; variant chosen by measurement.*

**Context.** The product owner asked for text cleaning before training
(normalization, tokenization). Already in place: the newest message is cut
from quoted history, external-sender warnings and safe links are removed
(`entity_extractor.extract_latest_email_body`); the TF-IDF vectorizer
lower-cases and tokenizes into words and word pairs. Four variants were
compared on the real labeled data (1,094 emails, 150 POD requests,
5-fold cross-validation, XGBoost):

| Variant | POD F1 | Precision at 0.90 | Coverage at 0.90 |
|---|---|---|---|
| Current | 0.779 ± 0.049 | 0.922 | 0.473 |
| Numbers, URLs, emails -> tokens | 0.773 ± 0.051 | 0.943 | 0.547 |
| Same + English stopwords removed | 0.784 ± 0.042 | 0.932 | 0.547 |
| Current + stopwords removed | 0.774 ± 0.056 | 0.939 | 0.513 |

**Decision.** Before vectorizing, subject and body replace every token
containing a digit with `numtoken`, URLs with `urltoken`, email addresses
with `emailtoken`, collapse whitespace and lower-case
(`classifier.normalize_text`, used by both training and prediction).
Sender and recipients are left as they are. Stopwords are kept: removing
them changed nothing measurable. Stemming is not added: it needs a new
library and nothing suggests it would help short, templated emails.

**Consequences.** Models trained before this change see differently
shaped input; retrain after pulling it. F1 differences are within the
spread between folds; the gain is in precision and coverage at the
threshold. The number model is not affected: it must see the number
itself.

*Note:* the sender is no longer kept as it is. 907 of the 1,094 labeled
emails had internal senders stored as Exchange paths (`/O=...`), while
the live agent reads their SMTP address, so the model learned a sender
it never meets live. The sender is now `internal` (an Exchange path, or
the POD mailbox's domain) or the sender's domain (`sender_feature`), and
the historical pull stores SMTP addresses. Same 5-fold CV: POD F1 0.775
to 0.790, precision at 0.95 unchanged (0.971). Retrain the intent models
after pulling it.

*Note (product owner): stemming added.* Measured on 1,587 labeled emails (472 POD),
XGBoost, stratified 5-fold CV over 3 splits. Snowball stemming of each word
as written, before the vectorizer: F1 0.853 to 0.860; at threshold 0.92
the same share of POD requests answered (66%) with 14 instead of 15
non-POD emails answered; at 97% precision 61% instead of 57% answered.
Stemming after punctuation is removed, and WordNet lemmatization with or
without part-of-speech tags, did no better; all differences are about 1 to 2 emails, within the spread
between splits. The product owner chose plain stemming (`classifier.stem_text`,
library `snowballstemmer`). It is part of each saved model's vectorizer,
so a model trained before keeps working unstemmed until retrained.

*Note: other measurements on the same data, not adopted.* Random forest,
AdaBoost and their soft voting with XGBoost: no more POD requests answered
at equal precision; AdaBoost's probabilities stay near 0.5, so any vote
with it never reaches the threshold. The threshold where POD precision
first reaches 95% is 0.91 to 0.92 (66% answered); 0.95 answers 60% with
about 40% fewer non-POD emails answered.

**Open questions.** None.

---

## D-017 Forwarded requests

*Agreed with the product owner.*

**Context.** Number extraction reads only the newest message and stops at
the first "Regards", so signature numbers are not picked up; the product owner
confirms nothing after the sign-off is a requested number. One real
request was a short "please send the POD for these" above its signature,
followed by a forwarded email listing 41 connotes. The quoted header was
not recognised, and regex found no number, so D-015 would have skipped a
real request.

**Decision.** When the newest message has no number, the regex extractor
reads the whole email, including text after the sign-off and quoted
history. When the newest message has a number, nothing else is read.

**Consequences.** Measured on the 150 labeled POD requests: no request is
left without a number (was 1); number recall 0.755 -> 0.865; precision
0.791 -> 0.598. All 142 extra numbers come from that one forwarded email
(183 numbers, 41 labeled); the others may be references or other IDs of
the same table. At runtime, numbers not in Power BI are UNKNOWN, so such an
email is held for the team (D-006), not answered. `outlook_reader` keeps
the whole body as `full_body` for this; it is not written to tracking.

**Open questions.** None.

---

## D-018 Numbers by regex; the master list is not a training source

*Agreed with the product owner.*

**Context.** On the real data the regex extractor beats every number model
(regex precision 0.79 / recall 0.76; best model CONNOTE F1 0.40). The
product owner asked whether the model could learn from the master list
(`DoNumber`, `Reference`). Measured: 2 of 220 labeled connotes and 0 of 78
labeled references are in the master; the master's reference shapes
differ from those customers write; connotes and references share shapes,
so only the surrounding words tell them apart. A model trained on master
shapes scored precision 0.42 / recall 0.42. References have no fixed
format (product owner).

**Decision.**
- The active number model is `regex` (the default when none is chosen).
- The master list is not used to train. Numbers are validated at runtime
  against the live Power BI dataset (D-004), which is always current.
- The number model is retrained from labeled emails when more labels
  exist, and chosen only if it beats regex on the same measure.

*Later note:* the CRF number model beats regex on the same measure
(D-026); regex stays as its yardstick and fallback.

**Consequences.** Regex misses references with unfamiliar shapes (36 of 78
labeled references); they are caught only if the team adds them when an
email is held. `historical_pull` still uses the master for automatic hints.

**Open questions.** None.

---

## D-019 Cold-start gate for the pilot

*Agreed with the product owner.*

**Context.** The intent model is used only when every class has at least
`POD_MIN_TRAINING_SAMPLES_PER_CLASS` labeled emails (default 200). The real
data has 150 POD requests, so the model would never be used.

**Decision.** The default is 150 during the pilot. Labeling continues (936
rows are unlabeled).

**Consequences.** Replies are drafts during the pilot (D-008), so a weaker
model costs review time, not wrong emails. Cross-validated precision at
the 0.90 threshold is about 0.93-0.94, below the 95% exit target (D-014);
more labels are the main lever.

**Open questions.** None.

---

## D-020 Dashboard rules for labeling, the queue and settings

*Agreed with the product owner (answers 45-48).*

**Context.** Phase 3 needed four rules the earlier decisions left open.

**Decision.**
- Agent decisions pulled into the training data do not train the model
  until the product owner confirms or changes them (they stay in the pseudo-label
  columns, marked `System decision`; afterwards `Manual`).
  *Note:* an email decided in the Review queue is a person's decision,
  so it enters the training data as a label at once, marked
  `Manual review` (bug report item 9).
- New mailbox emails for training come from `historical_pull --update`
  (last 365 days, emails with a number, up to 2,000, labels kept).
- The review queue has "mark resolved": a person handled the email; it
  leaves the queue and its time in the queue can be measured.
- The confidence threshold is set in the dashboard. The send mode can be
  changed there, but switching to `send` needs typing `SEND`. `STOP` can be
  switched on and off there.

**Consequences.** The model never learns from its own unchecked guesses.
Only the threshold and send mode can be written to `.env` from the
dashboard; other values are refused. The dashboard is limited to this PC
(`.streamlit/config.toml`), because it can switch sending on.

**Open questions.** None.

---

## D-021 A PyTorch model as an optional candidate

*Agreed with the product owner.*

**Context.** PyTorch can be installed on the PC running the agent, Hugging Face cannot
be reached, so no pretrained language model is available (D-001 note). The
product owner wanted a PyTorch model to compare and be able to choose. PyTorch was
preferred over TensorFlow: it is known to install there, and for a model
this small the two are equivalent.

**Decision.**
- `torch_mlp`: a small network (one hidden layer, dropout, class-weighted
  loss) trained from scratch on the same TF-IDF features, for both intent
  and numbers. It is trained, recorded and chosen like any other model.
- PyTorch is optional (`requirements-torch.txt`). Without it, training
  skips `torch_mlp` and everything else works.
- The active models do not change: intent stays the product owner's choice,
  numbers stay `regex` (D-018).

**Consequences.** Measured on the real labeled data (same split as the
other models): intent POD_REQUEST F1 0.690 (XGBoost 0.815), precision at
0.90 0.895 (XGBoost 1.000 on 16 emails); numbers macro F1 0.527, the best
of the five models, REFERENCE F1 0.261, still far below regex for use.
Training all models takes about 5.5 minutes instead of under one, mostly
the PyTorch number model. Unknown model names now fail instead of quietly
becoming a random forest.

**Open questions.** None.

---

## D-022 Cross-validation instead of one train/test split

*Agreed with the product owner.*

**Context.** Training used one 80/20 split. The product owner asked why there was
no validation set. The test part was in fact also used to choose the
active model and the threshold, so its scores were optimistic; and with
about 30 POD requests in it, one email moved precision by more than 3
points (one split showed 100% where 5-fold CV showed 94%). A separate
validation set would make both parts smaller still.

**Decision.** Every training run scores each model with 5-fold
cross-validation: each example is predicted once, by a model that never saw
it, and all metrics, confusion matrices and threshold curves come from
those predictions. Intent folds are stratified; number folds are split by
email. The saved model is then fit on all data.

**Consequences.** Scores use every labeled example and are less optimistic;
the saved models learn from 100% of the data. Training takes about six
times longer (five folds plus the final fit). Earlier history records (one
split) still show in the dashboard, labeled as such. Measured on the number
task with folds by email, the earlier token-split scores had been inflated
by tokens of one email sitting in both training and test.

**Open questions.** None.

---

## D-023 Numbers not in Power BI go to POD Outstanding as "Not in The System"

*Agreed with the product owner (answers 56, 57).*

**Context.** Some real POD numbers are not in the Power BI dataset; the team
records them as "Not in The System". The agent cannot tell such a number
from a wrong one (a phone or invoice number) by looking it up.

**Decision.** For each UNKNOWN number the agent adds a row to POD
Outstanding with status "Not in The System" and the requested number as its
connote, and tells the team in Teams to check it and delete the row if it
is not a POD number. The email is still held for the team (D-006).

**Consequences.** Number recognition precision is about 0.60 today, so some
rows will be phone or invoice numbers and must be deleted by the team; the
Teams message says so. Flow 2 adds a row only if the number is not already
listed.

**Open questions.** None.

---

## D-024 Intent: XGBoost, with logistic regression as yardstick

*Agreed with the product owner (answer 53).*

**Context.** On the real labeled data XGBoost beat every other intent
model: 5-fold CV POD F1 0.773 against 0.717 (random forest), 0.711 (SVM),
0.671 (logistic regression) and 0.690 (PyTorch, one split); soft voting
matched it but did no better at equal precision. The product owner worried XGBoost
overfits a small data set: it scores 0.998 on its own training data, but it
is still the best on emails it has not seen. A nested-CV tuning comparison
with a tuned random forest was stopped by the product owner before it finished; no
result was recorded.

**Decision.** Intent training keeps two models: `xgboost`, the model to
use, and `logreg`, a simple yardstick. SVM and random forest are removed
from intent (their code too). Number candidates are unchanged.

**Consequences.** Intent training is faster. If after retraining XGBoost's
cross-validated scores fall to logistic regression's level, or the gap
between its training and CV scores widens, the data needs checking. Older
history runs still show their SVM and random forest scores. Overconfidence
is handled by the threshold (about 0.95 suggested; precision at 0.90 was
0.943, below the 95% target).

*Note (bug report item 16, overfitting):* measured on 1,587 labeled emails
(472 POD), same 5-fold CV. The settings as they are (depth 6, 300 trees)
score best on the test folds: macro F1 0.898, and at threshold 0.95
precision 0.965 with 58% of POD requests handled. Shallower trees, larger
`min_child_weight`, `reg_lambda` 10, `reg_alpha` and early stopping on a
20% validation split all narrow the train/test gap (to 0.072 at best) but
lower the test score (0.873 to 0.897) and the share handled at 0.95 (33%
to 51%); none handles more POD requests at 95% precision (best 70%, the
same as now). A train score near 1.0 is usual for boosted trees on TF-IDF;
the test score is what counts. Settings unchanged.

**Open questions.** None.

---

## D-025 Intent confidence threshold 0.95 by default

*Agreed with the product owner (answer T).*

**Context.** XGBoost on the real labeled data, 5-fold CV: at threshold 0.90
precision 0.943 and coverage 0.547; at 0.95 precision 0.971 and coverage
0.447. The pilot target is 95% precision (D-014).

**Decision.** The default threshold is 0.95. The product owner sets the working
value in the dashboard (Models tab to see its effect, Settings tab to
save), after retraining on the live data.

**Consequences.** About one in five POD requests that 0.90 would have
handled goes to manual review instead, and wrong automatic handling is
roughly halved. An old `POD_CONFIDENCE_THRESHOLD=0.90` in `.env` overrides
the default; the test checklist says to change it.

**Open questions.** None.

---

## D-026 Numbers: a CRF, with regex as yardstick and fallback

*Agreed with the product owner (answer 60).*

**Context.** Regex found 87% of the labeled numbers but also many wrong
ones (precision 0.60); each wrong one holds the email and lands in POD
Outstanding as "Not in The System" (D-023). Per-token classifiers
(logistic regression, SVM, random forest, XGBoost) never beat regex,
and the PyTorch candidate did worse and was removed (D-021). Measured on
the 150 labeled POD requests, 5 folds split by email:

| Method | Emails fully right | Precision | Recall | Requests without a number |
|---|---|---|---|---|
| regex | 96 | 0.598 | 0.865 | 0 |
| CRF alone | 107 | 0.924 | 0.854 | 10 |
| CRF at 0.5, else regex | 114 | 0.920 | 0.895 | 0 |

**Decision.**
- The number model is a CRF (`sklearn-crfsuite`) tagging each token of the
  subject and newest message CONNOTE, REFERENCE or O from its shape and the
  words around it. A token is a number when the CRF is at least 0.5 sure
  (`POD_NUMBER_CONFIDENCE_THRESHOLD`).
- If the CRF finds none, regex is used, then regex on the whole email
  (D-017). Choices are `crf` and `regex`; the per-token classifiers are
  removed.
- Training compares the CRF with regex email by email and stores both.

**Consequences.** About seven times fewer wrong numbers, so fewer held
emails and fewer "Not in The System" rows to delete; slightly more numbers
found; no request left without a number. Training takes seconds. The CRF
also says connote or reference. One small library is added; wheels exist
for Python 3.10 and 3.13 on Windows. Retrain on the live data and compare
in the Models tab before choosing `crf`.

**Open questions.** None.

---

## D-027 Power BI lookup over several tables

*Agreed with the product owner (answer 58).*

**Context.** The lookup needs three tables: TMS (connote and reference,
sender, receiver, date), POD Scans (POD status) and DeliveryApp. The query
had assumed one table. The product owner wants to fill in the real table and
column names and the links between them by hand.

**Decision.**
- `.env` holds the main table and its connote and reference columns
  (`POD_PBI_TABLE` and friends), how each other table joins it
  (`POD_PBI_LINKS`: `Table[key]=TMS[key]`), and the columns to bring back
  (`POD_PBI_COLUMNS`: `Name=Table[Column]`). `.env.example` lays them out.
- The query filters the main table by the requested numbers and brings each
  column from another table with `MAXX(FILTER(other, other[key] = main[key]))`,
  comparing keys as text. It does not depend on the model's relationships.
- Some consignments are only in DeliveryApp (product owner). DeliveryApp is a
  second table searched the same way (`POD_PBI_SECOND_*`); its rows are added
  under TMS's with `UNION`, carrying the same column names, so matching
  and the Outstanding row treat both alike. A column DeliveryApp does not map
  is empty for its rows. Their POD status is in DeliveryApp too (product owner), mapped as `POD Status` in `POD_PBI_SECOND_COLUMNS`. Their POD
  PDFs are on the same share, so the file search needs no change.

**Consequences.** Table and column names change without code. If a linked
table has several rows for one connote with different values, the
largest (alphabetically last) is taken; check on the live data. Names left
as `<...>` or an unlinked table are refused at the point of use.

*Note:* TMS can hold two rows for one connote with different POD
statuses, one of them "PickUp" (product owner). The query returns every row; when
a number matches several, `match_rows` takes the one whose POD status
(`POD_PBI_STATUS_COLUMN`) is not PickUp, and a PickUp row only when it is
the only one, so such a connote is still found, not "Not in The System"
(product owner, answer b).

**Open questions.** A number in both tables comes back twice; the
TMS row is expected first but `UNION` does not promise an order.

---

## D-028 AI wording for replies and Teams messages, optional

*Agreed with the product owner (answers 65-69). Planned, not built: its design was
section 10 of the first build plan, `pod-autoreply-plan.md`, since removed
(in the git history).*

**Context.** The product owner wants replies and Teams messages to read less like
templates. AI Builder's **Run a prompt** is available in Power Automate,
but it starts failing after a number of replies, likely when the free
credits run out. A wrong fact in a customer reply (a POD said to be
attached when it is not) costs more than stiff wording.

**Decision.**
- Python decides every fact; AI Builder only words the reply and the
  Teams messages, through a fourth flow and the same file handoff (D-009).
- The prompt carries the facts and the customer's first name only, never
  the customer's email text.
- Python checks each text (every number present, no other number, no
  wrong attachment claim, no links) and adds the greeting, the automated
  notice and the sign-off itself.
- `POD_AI_WORDING=on|off`, also in the dashboard, default off. An error,
  a timeout or a failed check uses the template for that message, and an
  error pauses the AI for a while, so the agent never stops because of it.
- Built only after Flows 1 to 3 run and the template pilot has numbers.

**Consequences.** One more Power Automate round trip per message (about 5
to 10 minutes) while it is on. The templates stay the fallback, editable in
`app/reply_generator.py`. The pilot measure has to split edit rates by
wording, so AI drafts are judged against template drafts.

**Open questions.** AI Builder's credit limit, and how its failure shows in
the flow (an error, or an empty result); check on the first test.

---

## D-029 Outlook categories for completed email actions

*Agreed with the product owner. Built, not live-verified.*

**Context.** The product owner wants processed emails marked in Outlook so it is
clear whether an agent-created reply was sent and whether missing PODs were
recorded in the Outstanding list.

**Decision.**
- Add the `POD Agent Reply` category only after an agent-created draft is
  confirmed sent, whether edited or unedited. Saving a draft is not enough.
- Add the `On List` category after Flow 2 has processed every expected
  Outstanding `add` instruction for the email. Python treats the instruction
  being absent from `outstanding/` and present in `archive/` as confirmation.
- Preserve existing Outlook categories and do not add a duplicate category.
- Python checks and applies these categories on a later run. `On List`
  therefore depends on Flow 2 completing and the SharePoint sync exposing
  its archive move to the PC running Python.

**Consequences.** The two categories are independent and may appear at
different times. If the synced archive is unavailable or Flow 2 has not
archived all `add` instructions, `On List` is not applied yet. An archive
file still syncing (unreadable) counts as not confirmed, and a failure in
this check is counted in the run log but never stops the run.

**Open questions.** Verify both category updates with live Outlook and
SharePoint after sending a draft and processing an `add` instruction.

---

## D-030 Dashboard toggle for automatic sending

*Agreed with the product owner. Built, not live-verified.*

**Context.** Testing needs to produce reviewable Outlook drafts without the
automated-email notice. Once the product owner activates the feature, eligible
replies should be sent directly and identify themselves as agent-generated.

**Decision.**
- The dashboard's **Activate automatic replies** toggle controls
  `POD_SEND_MODE`. It defaults to off (`draft`) and saves the change for the
  next run.
- Off: save replies as Outlook drafts and omit
  "This email was automatically generated by the POD Email Agent."
- On: send replies directly and include that notice.
- The dashboard explains the behavior and warns that enabling the toggle
  sends customer emails without review. Switching it on asks for a
  confirmation ("Yes, send automatically" or Cancel) before anything is
  saved, so one click by mistake cannot start sending; switching it off
  back to drafts needs none.

**Consequences.** The existing pilot measure remains available in Settings
to inform the product owner's decision (D-014), but activating the toggle explicitly
enables sending regardless of the measured result.

**Open questions.** Verify both modes through the dashboard and a live
Outlook run.

---

## D-031 Move finished emails to Actioned/Finished

*Agreed with the product owner. Built, not live-verified.*

**Context.** The team files handled POD requests in a folder named
`Actioned/Finished` (one folder; its name holds the slash) directly under the
mailbox folder the agent reads. The product owner wants the agent to do that for
emails it has finished, switched on and off in the dashboard.

**Decision.**
- A dashboard switch in Settings, off by default. Switching it on records
  the time; only emails whose reply went out after that move, so earlier
  ones stay where they are (product owner).
- An email moves when its status is `Replied`, every requested POD was
  attached (including files the team chose in Teams), and no number was
  "not received": those stay, as the team still waits for that POD.
- A draft counts once it is sent, as Sent Items shows (product owner, like the `POD
  Agent Replay` category); a sent reply counts at once. The time is kept as
  `sent_at`.
- The folder is `POD_FINISHED_FOLDER` (default `Actioned/Finished`) directly
  under `POD_OUTLOOK_MAILBOX`, so it follows that setting (`POD` or
  `POD/Inbox`) and a renamed folder needs only `.env` (product owner).
- Moving runs last in a cycle, after the category check, since a moved email
  gets a new entry ID; the new ID is kept in tracking. A failure is counted
  in the run log and tried again next run; it never stops the run.

**Consequences.** Replies sent before this change have no `sent_at` and are
never moved. A folder that is missing or renamed without `.env` blocks
moving until it is fixed, without affecting replies.

**Open questions.** Verify on live Outlook: the folder is found and the
moved email keeps its categories.

---

## D-032 Drafts saved in the POD mailbox's own Drafts folder

*Agreed with the product owner. Built, not live-verified.*

**Context.** Agent drafts landed in the product owner's personal Drafts although
they were sent on behalf of the POD mailbox, even after drafts were moved to
the mailbox's "default Drafts" folder. The product owner sees the same when replying
by hand from the POD mailbox: Outlook saves drafts of a shared mailbox in the
person's own mailbox, and the shared store's `GetDefaultFolder(olFolderDrafts)`
did not reliably give the shared Drafts. The POD mailbox has its own Drafts
folder directly under its root (product owner).

**Decision.**
- Replies open the original email with its entry ID and the POD mailbox's
  StoreID (from `POD_OUTLOOK_MAILBOX`), and mark it read the same way.
- A draft is saved, then moved to the folder named "Drafts" directly under
  the POD mailbox's root, found by name like `Actioned/Finished`; it is
  looked up before saving, so a missing folder leaves no draft behind.
- After the move the draft must be in that mailbox and folder; otherwise
  the reply fails naming where the draft went. The email is then recorded as
  Error, stays unread and can be processed again; the agent deletes nothing.
- The StoreID is not stored per email: all email comes from one mailbox.
- `python -m app.check_drafts` shows, read-only, which Drafts the agent uses,
  Outlook's default one, and where the latest draft really is.

**Consequences.** Outlook in English is assumed for the folder name
"Drafts" (HANDBOOK). A renamed Drafts folder stops drafting with a clear
error.

**Open questions.** Confirm in production with `app.check_drafts` that
the default Drafts was the personal one (the suspected cause), and that new
drafts appear in POD > Drafts.

---

## D-033 Open an email in Outlook from the dashboard

*Agreed with the product owner. Built, not live-verified.*

**Context.** Reviewing an unclear email in the dashboard, the product owner wants
the original in Outlook Desktop without searching for it.

**Decision.**
- An "Open in Outlook" button in every pop-up that shows one email: Review,
  Label and History (product owner).
- The email is looked up by its entry ID in the POD mailbox only (the
  StoreID from `POD_OUTLOOK_MAILBOX`, as in D-032), never in the personal
  mailbox, and shown in its own Outlook window on this PC. An email the agent
  moved to Actioned/Finished is found by its new ID (D-031).
- Outlook marks the email read when it opens, as when it is opened by hand;
  the agent does not set it back (product owner). Nothing else about it changes.
- An email the team moved to another folder or deleted, or Outlook not
  running, shows an error in the pop-up; the agent does not search other
  folders for it (product owner).

**Consequences.** Opening a Manual Review or Held email marks it read, so
it no longer looks unhandled in Outlook, and Process again then skips it
until it is set unread again. The dashboard must run on the PC with Outlook.

**Open questions.** Verify the window opens from each pop-up in production.

---

## D-034 Connote lists and ranges written in short form

*Agreed with the product owner. Built, not live-verified.*

**Context.** Customers ask for several connotes at once in short form:
`7335842/843/844`, `7335399/7335401/02/03/04`, `7336101-103`,
`614024-610423`. The extractors found only the first number, so the reply
covered one POD and dropped the rest.

**Decision.** In the text the numbers were found in, lists are written out
(`number_lists.expand`), by the product owner's rules:
- A connote is all digits, at least 6 long. After `/`, a shorter number
  completes the previous full one (`7335401/02` is 7335401, 7335402).
- After `-`, an end of 3 or more digits makes a range (`7336101-103`); an
  end of 1 or 2 digits is part of one connote, kept as written
  (`4653685-1`, `4653685-01`).
- Two full numbers joined by `-` are a range when the second is higher and
  the range holds at most 20; otherwise they are two numbers, each looked up
  in Power BI (`614024-610423`; an invoice and a connote, `20260407 - 80655502`).
- A short-end range that runs backwards or holds more than 20 numbers is
  not guessed: the email is held for the team, never answered in part.
- Only lists whose full numbers include one the extractor found are read;
  numbers with letters (`XY62302-03`, `MN675418-3`) are never expanded.
- The regex also recognises numbers with another customer's prefix (the
  prefixes are set in `POD_CONNOTE_PREFIXES` since D-060).

**Consequences.** Measured on the 472 labeled POD emails: 14 outputs change,
3 closer to the manual labels and 11 adding numbers the labels lack (the
invoice numbers and the plan's example), none held. An invoice number not in
TMS or DeliveryApp is "Not in The System" and holds its email (D-023). Number
model scores, compared with the labels, can drop slightly until those
emails' labels include the added numbers.

**Open questions.** Check in production whether invoice numbers such as
`20260522` are in TMS's Reference column; if not, such emails are held.

---

## D-035 Held emails corrected in the dashboard

*Agreed with the product owner. Built, not live-verified.*

**Context.** An email is held when one of its numbers is not on the share
and not in Power BI (D-023), often because an invoice number next to the
connote was read as a number (D-034). The POD for the real connote was found
but not sent, and the team had to answer the whole email by hand.

**Decision.**
- Every Held email can be opened in the Review pop-up, whatever its reason;
  the intent starts as POD_REQUEST. The person corrects the numbers and
  saves; the email becomes `Reviewed` and the agent continues it on its next
  run with those numbers (scan search, Power BI, one reply with every POD
  found). A problem not yet solved simply holds it again.
- Numbers the agent added to POD Outstanding for that email and the person
  took out are not POD numbers: Flow 2 is asked to remove their rows.
- The correction is a manual label, written into the training data as
  `Manual review`, like any review.
- Typed numbers are checked (no count limit since D-047): each 4 to 20 letters,
  digits, `-` and `/`, no spaces. The pop-up shows the list the agent will
  look up, or why it cannot be saved, before Save. The History pop-up shows
  the numbers of each status, so the original and corrected lists can be
  told apart.
- Uploading a POD PDF from the dashboard is not built (product owner): a POD that
  is not on the share is sent by the team replying in Outlook, which the
  agent now recognises and resolves (D-039). The plan for it is removed.

**Consequences.** The Teams message for such a hold points to the Review
queue. The original numbers stay in the email's history.

---

## D-036 Held emails corrected in Teams

*Agreed with the product owner. Built, not live-verified.*

**Context.** The team works in Teams more than in the dashboard. A held
email whose numbers are wrong could only be corrected in the dashboard
(D-035).

**Decision.**
- An email held because a number is not on the share and not in Power BI,
  or because a number list could not be read, gets a Teams card instead of
  a plain message: the email's numbers in a text field, **Save numbers** and
  **Handle manually**. Other holds keep the plain message.
- Saving is the same as a dashboard review: the email becomes `Reviewed`
  (reason names who answered), continues in the same run, rows the agent
  added for numbers taken out are removed, and the correction goes into the
  training data as `Manual review`. If the training file cannot be written
  (open in Excel), the run log says so; the correction still goes on.
- **Handle manually** keeps the email held without a card. Saving without
  usable numbers posts a new card.
- Each hold gets its own card id, so an answer to an older card is never
  read as the current one. The dashboard Review stays available; a card
  answered after a dashboard correction changes nothing.
- Flow 3 passes the typed numbers back in `connotes`; it needs only that
  extra field in its result file.

**Consequences.** A Teams card shows the message, not the full email; the
team opens the email in Outlook to read it. Anyone in the chat can answer;
the name is recorded.

---

## D-037 Teams cards that never reach Teams

*Agreed with the product owner. Built, not live-verified.*

**Context.** File choice cards did not appear in Teams and their files stayed
in `handoff/teams`. A card waiting for an answer also kept its file there,
since Flow 3 moved it only after the answer, so the agent could not tell an
unanswered card from one that never left, and posted cards again every few
minutes.

**Decision.**
- Flow 3 moves the trigger file to the archive right after reading it, and
  writes an `error` result when Teams refuses the card or the wait times out.
- A card whose file is still in `teams/`, or that came back with an error,
  30 minutes after it was written is written again under a new id, at most
  3 times (product owner); the old file goes to the archive. A card that left
  `teams/` is in Teams and is never posted again.
- After 3 resends the agent stops; the Review queue shows the email, its
  request ID (the column was named Teams request ID before D-049) and the reason, for the team to check Flow 3, and
  **Send Teams card again** posts it once more with 3 new resends.
- The same holds for correction cards of held emails (D-036).
- Waiting for an answer records no tracking event; files in `teams/` that
  no open card needs move to the archive after 30 minutes, so none pile up.

**Consequences.** Until Flow 3 moves the file at the start, a card that did
reach Teams but is unanswered looks undelivered: it is posted again up to 3
times. A card posted to the wrong chat counts as delivered; Flow 3's Group
chat setting is checked by hand.

---

## D-038 Emails held for their attachment size

*Agreed with the product owner. Built, not live-verified.*

**Context.** A reply whose files total more than `POD_MAX_ATTACHMENT_MB`
(20 MB, about 27 MB once sent, under Exchange's and most receivers'
limits) is held. The team had only the file names to work from.

**Decision.**
- The Teams message lists every requested number with its status: scan
  found (file and size), not received (with its Power BI status), or not
  in Power BI.
- The files are copied to `POD_MANUAL_UPLOAD_DIR/<request id>/`, a folder
  synced to SharePoint, and the message links that folder through
  `POD_MANUAL_UPLOAD_URL`, for the team to attach by hand. No script is
  handed out: a downloaded `.bat` is often blocked and is a phishing
  pattern.
- The folder must be outside the project (customer files never near git)
  and outside the scan folders (copies would be found as scans).
- The copies are deleted once the email is no longer held for its size:
  marked Resolved in the dashboard, processed again, and later a manual
  reply found in Outlook (D-039). Only folders named by a request id are
  touched.

**Consequences.** If nobody marks the email Resolved, its copies stay; the
email stays in the Review queue meanwhile. SharePoint keeps deleted files in
its recycle bin for its own retention period.

---

## D-039 Emails answered by hand in Outlook

*Agreed with the product owner. Built, not live-verified.*

**Context.** The agent drafted a reply to an email a team member had
already answered from the POD mailbox, once Power BI or the team had
answered.

**Decision.**
- A manual reply is Outlook's own mark on the email, "You replied to this
  message" (the last action Reply or Reply All, and when). It is on the
  email in the POD mailbox whoever replied and from which PC or Outlook on
  the web, so Sent Items are not needed. Forward does not count.
- It is checked right before every reply or draft, and each resume run for
  every email Waiting Power BI, Waiting Team, Held or Reviewed.
- Found: status `Resolved`, reason "replied manually in Outlook" with the
  action and time; no draft, no POD Outstanding change; the email is left as
  it is. Shown in the dashboard only (product owner).
- A Teams card Flow 3 has not taken is withdrawn (its file archived); one
  already in Teams cannot be, and an answer to it is ignored. A Power BI
  result arriving later is archived after 30 minutes. Copies of a large
  email's files are deleted (D-038).
- If Outlook cannot open the email before a reply, nothing is sent and the
  email gets `Error`. A waiting email it cannot open (moved or deleted) is
  left as it is.

**Consequences.** One Outlook lookup per waiting email each resume run.
Whether Outlook mobile sets the mark, and that the time is read in UTC, is
to be checked in production.

---

## D-040 A numbered id for each Power BI request

*Agreed with the product owner. Built, not live-verified.*

**Context.** Every Power BI request of an email had the same file name. After
Reprocess in Power BI or a "Not all" answer, a late answer to the earlier
request, made with the old query, could be read as the answer to the new
one.

**Decision.**
- Each request gets its own id, `<request id>-p<n>`, recorded in the
  email's state as `pbi_request`. Flow 1 already names its result after the
  request id, so it needs no change.
- Only the answer to the current request is read. A late answer to an
  earlier one is never read; it is archived with the other handoff files
  nothing waits for, after 30 minutes (D-037).
- An email waiting from before ids were numbered is read under its plain id.

**Consequences.** The simulation's S15 holds the email wrongly on the old
code and passes on the new.

---

## D-041 Emails moved or deleted in Outlook while they wait

*Agreed with the product owner. Built, not live-verified.*

**Context.** The team files a finished email in `Actioned/Finished`, also
when they handled it without replying. The agent only knew the email's
entry ID, which Outlook changes when the email is moved, so it could not
tell a finished email from one moved elsewhere or deleted.

**Decision.**
- Each new email's Internet Message ID is kept in tracking (`message_id`);
  it does not change when the email is moved.
- Each resume run, every email Waiting Power BI, Waiting Team, Held,
  Reviewed, Manual Review or Other Intent is looked at in Outlook, once,
  for a reply (D-039) and for where it is:
  - still in the POD mailbox folder: it waits on;
  - in `POD_FINISHED_FOLDER`, found by its entry ID or, once moved, by its
    Internet Message ID: **Resolved**, "moved to Actioned/Finished in
    Outlook";
  - a POD request anywhere else, or deleted: **Held**, "moved ... in
    Outlook", once, for the team to Mark resolved or Process again (product owner:
    a POD request is done only in the finished folder);
  - an Other email anywhere else, or deleted: **Resolved** (product owner: Other
    emails may be filed in any folder). Manual Review emails follow the POD
    rule, as they may be POD requests.
- Either way its Teams cards not yet taken and its Power BI request are
  withdrawn, and its cards are dropped, so none is posted again.
- Read or categorised emails are not taken as handled: the team opens
  emails to look, and the agent adds categories itself.

**Consequences.** Emails that were waiting before the Internet Message ID
was kept cannot be found in the finished folder once moved: they are Held
instead of Resolved. Any Outlook error opening an email counts as moved or
deleted.

---

## D-042 Scans matched by the whole numbers in their names

*Agreed with the product owner. Built, not live-verified.*

**Context.** Scans are named by the connote, sometimes with a reference or
a date after it, `_` or `-` between, then Signed and a copy number:
`1234567 - Signed.pdf`, `1234567_80655501 - Signed.pdf`,
`1234567-07-04-2026 - signed(2).pdf`. Matching on the name with spaces and
`-` removed (D-003) ran the connote and the reference together, so a scan
asked for by its reference was AMBIGUOUS; any number inside another, such as
`7398507` in the pallet number `PLT73985078J`, was offered to the team; and
every connote with a second copy went to the team.

**Decision.**
- A file name is read as parts, split at spaces, `_`, `-`, `.`, `,` and
  brackets, after the word Signed or Unsigned and its copy number are
  left out (`pod_files.name_tokens`). A letter prefix or a short part number
  written apart also counts joined (`AB-10234` is also `AB10234`,
  `4653685-01` is also `465368501`), as requested numbers are.
- A file matches a number when one whole part is the number: as written
  first, else with more or fewer leading zeros (D-003 note). A number
  inside another is never a match (product owner: `PLT...` is a pallet number);
  with no file, the number goes to Power BI. A date written `07_04_2026` or
  `07-04-2026` falls apart into short parts, so an invoice number never
  matches it.
- Among the matches, a scan with "signed" in its name wins over one
  without (`1234567 - Signed.pdf` over `1234567..pdf`); several signed scans
  go to the team. Files with "unsigned" in their name are not indexed at all
  (`1234567..pdf` over `1234567 - Unsigned.pdf`).

**Consequences.** Fewer Teams cards: a reference after `-`, a signed copy
next to a plain one, and numbers inside pallet numbers no longer need the
team. An email that only has an Unsigned scan is looked up in Power BI.
Emails already waiting keep their cards.

---

## D-043 Choosing the right scan: Power BI scan path; ranking deferred

*Agreed with the product owner. Stages 1 and 2 built, not live-verified.*

**Context.** The agent once drafted a reply with a scan whose name only
contained the requested number (`7398507` in the pallet number
`PLT73985078J`). A customer's reference (`80655501`) was stored in Power
BI as `0080655501`, and its scan is named by the connote. Plan 2 set rules
first, and machine learning only to rank candidates, never to choose.

**Decision.**
- A file matches only a whole number in its name; a signed scan wins over
  one without the word; Unsigned scans are never used (D-042). Several
  candidates go to the team; no draft while a choice waits (D-006).
- A Power BI row's scan path (`POD_PBI_SCAN_PATH_COLUMN`, a path or
  `file://` URI) naming a PDF indexed under `POD_SCAN_FOLDERS` is the scan.
  Otherwise a reference matched in Power BI is searched on the share by its
  row's connote, then the team chooses. *(D-044 makes this every sender's,
  whatever the status, and checks some customers' rows by sender.)*
- Ranking the candidates with a model is deferred. Each email's tracking
  keeps the candidates (`ambiguous`) and the team's choice (`chosen`, or
  "Not all"), so no collection work is needed now. Once Teams cards work
  and have run a few weeks, count the file-choice cards and what the team
  chose: if they are rare, nothing more is needed; if the team's choices
  follow a pattern (the customer's folder, the latest year), make it a
  rule; only many cards with no clear pattern would justify a ranking
  model, measured on confirmed choices and never sending on its own.

**Consequences.** Plan 2 is removed. The production checks, including a
later count of file choices, are in `docs/TEST.md`.

---

## D-044 Intent first; numbers from attachments; the product owner's checking order

*Agreed with the product owner. Built, not live-verified.*

**Context.** Customers often send a POD request whose number is only in an
attached screenshot or PDF (D-015 skipped those). When several scans matched
a number, the team was asked even though Power BI's own data (its cleansed
scan path) names one. A reference whose connote was another number of the
same email led to a question for one document.

**Decision.**
- Intent first, read as a person reads it (product owner). The model always says
  POD request or not; the threshold says how sure.
  - Not a POD request, sure: done (Other Intent with a number, Skipped
    without).
  - Not a POD request, unsure: a number in the text or the attachments
    means Manual Review; none means done.
  - A POD request, sure or unsure: its numbers (text, else attachments)
    go through the usual checks; found: executed. Not found: a sure one is
    held with the Teams correction card (D-036); an unsure one goes to
    Manual Review, with no "Not in The System" row. No number anywhere:
    Manual Review.
  - No intent model in use (cold start): Manual Review with a number in
    the text, Skipped without.
- Numbers from the text; if none, from the attachments in this order: the
  names of PDFs, the text in pictures (Windows' own OCR, on the PC), the
  text of the PDFs (their first 3 pages, OCR for scans). The next source is
  used only when none of the numbers before is on the share or in Power BI;
  numbers read from attachments that another number made unneeded are
  dropped, and they add no "Not in The System" rows (product owner). Limits: 5 pictures
  (logos under 20 KB skipped), 5 PDFs, 3 pages, 10 MB each. No number
  anywhere: Manual Review. Files left unread by the limits and no number:
  Held, the team told to check them by hand. Where the numbers came from is
  in the state (`number_source`) and the History pop-up.
- **On the share and not ambiguous: done**, no Power BI question (product owner).
  Missing, or several files left after D-042's rules: Power BI is asked.
- Power BI: a row by connote (DoNumber) first, else by reference. For a
  sender in `POD_PBI_SENDER_CHECK` (`domain=word`), only rows whose sender
  column holds the word count; others are not what it asked for (product owner).
  The row's scan path naming an indexed PDF is the scan, whatever its
  status (it may lag). Else a number with several files goes to the team
  ("Power BI does not name its scan"); any other has its row's connote
  looked for on the share (its folder first), and with no scan is not
  received (reply, POD Outstanding). Not in Power BI: as before (D-023).
- A reference whose row's connote is another number of the email is that
  document: attached once, the reply says "for <connote> (your reference
  <reference>)".
- A reference matching several consignments whose scans differ is
  ambiguous: the team chooses (product owner).

**Consequences.** `POD_PBI_SCAN_PATH_SENDERS` is gone: the scan path is
every sender's. New dependencies: `pypdf`, `pypdfium2`, and `winrt` (Windows
only). The intent model now runs on every email, also those without a
number, which it never trained on. Outlook's attachments are saved to a
temporary folder and deleted at once. Plan 4 (numbers from pictures and
PDF names) is done by this and removed; unlike it, the numbers are used
without a person confirming them first, and PDF text is read too (product owner).

## D-045 A number the team keeps is answered "not in our system"

*Agreed with the product owner. Built, not live-verified.*

**Context.** An email held because Power BI did not know a number came
back to the team after they saved the same number on the card: Power BI
still did not know it, so the email was held again with a new card, and
the customer never got the PODs that were found. The card also listed every
number of the email, though only the unknown one was in question.

**Decision.**
- The correction card holds only the numbers not found; the others are
  kept. The team corrects the number, keeps it if it is right, or clears it
  if it is not a POD number (product owner).
- A number Power BI did not know that a person saves unchanged (card or
  dashboard) is confirmed: it is right and not in the system. It is not
  asked about again, also when the card is answered later. A number the
  person changed is checked again; still unknown, it is asked again (product owner).
- Once every unknown number is confirmed, the email gets one reply: the PODs found,
  then "Regarding <number>, we were unable to find it in our system. Could
  you please check the number and let us know?" (product owner: polite). With no
  POD found, the reply has only that line.
- Its "Not in The System" row stays in POD Outstanding.

**Consequences.** The confirmed numbers are kept in the review
(`review["confirmed"]`). This replaces D-036's "the email's numbers in a text field" for UNKNOWN holds; an
unreadable number list still shows all its numbers.

## D-046 Numbers from spreadsheets; a POD request reviewed without numbers

*Agreed with the product owner. Built, not live-verified.*

**Context.** A customer sends the numbers of a POD request in an Excel list
and none in the text. The agent did not read spreadsheets, so the email went
to Manual Review, where `POD_REQUEST` could not be saved without typing a
number.

**Decision.**
- Spreadsheets are a number source: `.xlsx`, `.xlsm`, `.csv` and `.xls`
  (product owner). The order is PDF names, spreadsheets, pictures, PDF text (product owner).
- Only columns whose header names a connote or reference are read: Connote,
  Consignment, Con Note, DO Number, Reference, Ref, Order No (product owner). The
  header is looked for in a sheet's first 10 rows. A spreadsheet without
  such a column is listed as not read, for the team.
- The cells under those headers go through the email text's own scheme:
  the regex, then lists written short (D-034, so `7335842/843` is 7335842
  and 7335843). No new rule (product owner).
- Limits: 10 MB a file, as the other attachments; 3 spreadsheets, their
  first 3 sheets. The number of numbers is not limited (product owner).
- `POD_REQUEST` can be saved in the Review queue without numbers: the agent
  then reads the email's attachments, as for a new email. None there: back
  to Manual Review. A Teams correction card saved empty still posts a new
  card (D-036).

**Consequences.** New dependency `xlrd`, for `.xls` only. A spreadsheet
whose number column has another header is not read; the header list is in
`attachments.NUMBER_HEADER`.

## D-047 Fixes from a review of the whole project

*Agreed with the product owner. Built, not live-verified.*

**Context.** A read of the code found three bugs and two risks.

**Decision.**
- No limit on how many numbers a person saves (product owner). The limit of 20
  (D-035) made a held email with more numbers impossible to save, on the
  card or in the dashboard, so it was held again forever. Each number is
  still checked for its form; a short range still holds at most 20 (D-034).
- An unsure POD request stays unsure when the agent tries the next
  attachment source: with none known, Manual Review, not the correction
  card (D-044). A review is carried on too.
- One scan that is two numbers' file (a connote and its reference in its
  name) is attached once.
- A dashboard review or Mark resolved waits up to 30 seconds for a running
  cycle and is then saved under the run lock, so the cycle cannot record
  over it; if the cycle is still running, nothing is saved and the
  dashboard says so.
- A Power Automate result that cannot be parsed within 5 minutes of its
  last change is still being written or synced: it is read again next run.
  Older, it is an error, as before.

## D-048 Manual Review in Teams too

*Agreed with the product owner. Built, not live-verified.*

**Context.** A Manual Review email waited in the dashboard Review queue
only, and the team does not always open the dashboard.

**Decision.**
- Every email that goes to Manual Review also gets a Teams card, except at
  cold start, when every email with a number would post one (product owner).
- The card is like the dashboard's review (product owner): the sender, subject,
  received time, why it is in Manual Review, the first 500 characters of
  the email's newest message, a Connotes field filled with the numbers
  read, an empty References field, and three buttons:
  - **POD request**: a review with those numbers, as from the dashboard;
    with none, the agent reads the attachments (D-046);
  - **Not a POD request**: `Reviewed - Other`, nothing is sent;
  - **Handle manually**: stays in Manual Review, without a card.
- Whichever answers first wins: a dashboard review withdraws the card file
  Flow 3 has not taken, and a later answer on the card is not read. So do
  Mark resolved and Process again.
- A number that cannot be one posts a new card saying why. The answer is a
  training label, as a dashboard review.
- A number the card's reason names as not in Power BI, kept unchanged, is
  confirmed (D-045): the reply says it is not in our system, and no
  correction card asks about it again.
- Resent after 30 minutes, at most 3 times, then shown in the dashboard
  (D-037).
- Only emails that go to Manual Review from now on; those already there
  stay in the dashboard (product owner).
- Flow 3 passes the References field back as `references`. An answer
  without it (Flow 3 not updated yet) is not used: the email stays in
  Manual Review, its reason saying so, as a typed reference would be lost.

**Consequences.** The customer's email text, up to 500 characters, is in
the team chat (product owner). Flow 3's File Content gets one more field.

## D-049 Flow 1 watched as Flow 3 is; a backslash typed on a card

*Agreed with the product owner. Built, not live-verified.*

**Context.** With Flow 1 off or failing, an email waited for Power BI with
no warning: only its age in the Review queue grew. A `\` typed in a Teams
card's number field reached the answer file as it was, which JSON refuses,
so after 5 minutes the email became Error (D-047).

**Decision.**
- A Power BI request with no answer 30 minutes after it was written is
  written again under a new id, the old one archived, at most 3 times, as
  a Teams card is (D-037). Then the Review queue shows a warning with the
  request id, to look up in Flow 1's run history, and **Reprocess in Power
  BI** starts it again with three more resends. Flow 1 leaves its request
  file in place, so "not taken" and "not answered" look the same.
- The Review queue's column is now **Request ID**: the Teams card's, or
  for Waiting Power BI the Power BI request's.
- A stray `\` in an answer file is read as a backslash; Flow 3's File
  Content also turns it into a space. A number with it is refused as not a
  number, and a new card says why.

## D-050 Files too large: a draft without them, and POD Outstanding updated

*Agreed with the product owner. Built, not live-verified.*

**Context.** A reply whose scans total more than `POD_MAX_ATTACHMENT_MB`
was held (D-038) before the agent wrote its POD Outstanding rows, so
numbers with no scan were missing from the POD list, and the team had no
draft to complete.

**Decision.**
- The reply is made as usual, with its POD Outstanding rows (product owner): a
  number not received gets its Power BI status, one not in our system
  `Not in The System`, and a found number's row is removed. Its text is
  the usual one.
- It is always a Reply All **draft without the attachments**, also in send
  mode, without the automated notice. The email is `Replied`, its reason
  "draft without its attachments", and marked read.
- The files are copied to `POD_MANUAL_UPLOAD_DIR` and the Teams message
  lists each number's status with the folder's link (D-038); it says the
  draft is in the POD mailbox's Drafts, for the team to attach the files
  and send.
- The copies stay until the pilot measure sees the draft sent or deleted.
  An email held for its size before this keeps its copies until it is no
  longer held.
- These drafts are left out of the pilot's unedited share (D-014): their
  files are always added by hand.

## D-051 A button on every Teams card to open the mailbox

*Agreed with the product owner. Changed by D-061.*

**Context.** A Teams card showed only part of the email, so the reviewer
could not read the original from Teams. The agent reads mail through
Outlook Desktop, whose entry ID cannot be turned into a link; a link that
opens Outlook Desktop (`outlook:`) is off by default and blocked by Teams.

**Decision.**
- Every card (Manual Review, correction, file choice) has a button that
  opens the POD mailbox in Outlook on the web, where the email is (product owner).
  The person who clicks needs access to the POD mailbox. Plain messages get
  no button.
- A lookup of the email's own link was built first and removed by D-061:
  it needed access the agent does not have.

## D-052 The Overview's figures counted as they read

*Agreed with the product owner. Built, not live-verified.*

**Context.** A review of the Overview found figures that did not mean what
their names said, the pilot's unedited share first: a draft sent untouched
counted as edited when the company signature replaced the stored sign-off,
or when the signature's logo came with it as an attachment.

**Decision.**
- Pilot (D-014): the share is of the drafts sent or deleted; one still in
  Drafts does not count until it is decided. A deleted draft still counts
  against it, as one the team did not use. What counts as a good draft is
  D-053.
- POD requests: an email the intent model called OTHER is not counted as
  one, even while it waits in Manual Review. The same goes for the numbers
  chart and the top domains.
- Headline figures: "Held for the team" is Held and Error only; Manual
  Review is its own figure; "Replies with a POD missing" counts replies
  with a number not received or not in our system.
- Numbers: a number not in our system (D-045) counts with those not in
  Power BI; a reference attached as another number's scan (D-044) counts
  as found; numbers of an email a person resolved are "Handled by team";
  an attachment number another made unneeded is not counted. "Still
  waiting" is only for emails still open. The weekly share found is over
  the numbers with a result.
- The email outcome for a reply with no POD attached reads "Replied, no
  POD attached" (not received, or not in our system).
- The waiting chart leaves out Other Intent emails, which the team handles
  in Outlook; the Review queue still lists them.
- The daily chart says what it shows: by the day the agent first saw each
  email, coloured by its outcome now.

## D-053 The pilot counts the PODs, not the wording

*Agreed with the product owner. Built, not live-verified.*

**Context.** The pilot (D-014) counted a draft as good only when its text
went out unchanged. The team may well reword a reply, add a sentence or cut
one; what the agent must get right is which PODs it attaches.

**Decision.**
- A draft counts as **sent, same PODs** when the reply sent carries exactly
  the PDFs the agent attached, whatever its text (product owner). Other
  attachments, such as a signature's logo, are not compared. A PDF added,
  removed or swapped makes it **sent, PODs changed**.
- The Overview figure is **PODs unchanged**: drafts sent with the same PODs
  out of those sent or deleted (D-052). The target is lowered from 95% to
  **90%** (product owner), and set in the dashboard since D-054.
- Drafts recorded before this as "sent unedited" count as the same PODs;
  those recorded as "sent edited" count as PODs changed, since which part
  was edited was not kept.

## D-054 Both targets 90%, set by the product owner in the dashboard

*Agreed with the product owner. Built, not live-verified.*

**Context.** The pilot target (D-014) and the intent model's precision
target were both 95%, written in the code and the text. The product owner lowered
the pilot target to 90% (D-053) and wanted to change either again without
a developer.

**Decision.**
- Two settings, both **90%** by default (product owner): `POD_PILOT_TARGET`, the
  share of drafts sent with the same PODs at which to switch to sending
  (Overview), and `POD_PRECISION_TARGET`, the share of the emails the agent
  acts on that must be POD requests (Models tab, and the threshold's line
  in the Settings tab).
- Both are set in the dashboard Settings tab, 50% to 100%, and kept in
  `.env`; the dashboard shows the new value at once.
- A target only judges the figures: it changes nothing the agent does. The
  confidence threshold stays as set (0.92, chosen for 95% precision); with
  a lower precision target the product owner may lower it in the Settings tab,
  where the precision at each threshold is shown against the target.

## D-055 The POD mailbox in CC on every reply

*Agreed with the product owner. Built, not live-verified.*

**Context.** Reply All kept the POD mailbox in **To** next to the
customer, as the email had been sent to it. The team keeps a copy of every
reply coming back to the POD inbox, so they can see an email was answered.

**Decision.**
- On every reply the POD mailbox (`POD_SHARED_MAILBOX_ADDRESS`, product owner) is
  in **CC** and never in To: moved from To, or added when the email was not
  sent to it. The other recipients stay where Reply All put them.
- The copy that comes back to the POD inbox is never read as a new email:
  the guard skips an email sent by the POD mailbox, and now also one sent
  on behalf of it (its sender is then the person who sent it). The copy is
  left unread, as the team's flow has it.

**Consequences.** Only that address is moved; the product owner confirmed it is the
one customers write to.

## D-056 The attachments read when the text's numbers are not in Power BI

*Agreed with the product owner. Built, not live-verified.*

**Context.** D-044 read the attachments only when the email's text had no
number. An email had a wrong number in its body (an invoice) and the right
connote only in its attachment, so it was held.

**Decision** (product owner):
- The start is unchanged: the intent, the numbers in the subject and body,
  the share, Power BI.
- When **every** number of the text is not in Power BI (none on the share,
  none in Power BI, none merely not received), the attachments are read in
  D-044's order and the agent goes on with their numbers.
- An attachment number that is known: the text's numbers are dropped, with
  no "not in our system" line, no POD Outstanding row and no card.
- The attachments do not help: it ends as before, with the correction card
  for the text's numbers and their "Not in The System" rows; the card says
  the attachments were read too.
- An unsure POD request goes the same way, and ends in Manual Review.
- Numbers a person saved (on a card or in the dashboard) are not checked
  against the attachments: D-045 decides them.

## D-057 Partial sending by the sender's domain

*Agreed with the product owner. Built, not live-verified.*

**Context.** Sending was all or nothing (D-008): every reply a draft, or
every reply sent. The product owner wants some customers answered directly while
the rest are still checked as drafts.

**Decision** (product owner):
- `POD_SEND_MODE` is `draft`, `partial` or `send`. Partial sends the reply
  when the sender's domain, the part after the @ exactly, is one of
  `POD_SEND_DOMAINS` (`domain;domain`); every other reply is a draft.
  Subdomains do not count.
- The flow is the same in every mode; only draft or send differs, as in
  full sending (unsure POD requests too, as D-044 handles them). Replies
  with no POD attached are sent as well. A reply too large to attach stays
  a draft (D-050); a sent reply has the automated-email notice, a draft
  does not.
- In the dashboard Settings tab the choice is Off, Partial or Full, with
  the domain list. Widening what is sent (switching up, or adding a domain
  in partial) asks for confirmation; narrowing it does not.
- `POD_SENDER_FOLDERS` and `POD_PBI_SENDER_CHECK` now also match the domain
  after the @ exactly, not its subdomains (product owner).

**Consequences.** In partial mode the pilot measure (D-014) counts the
drafts only, the replies to the domains not chosen yet.

## D-058 The historical pull takes each email once

*Agreed with the product owner. Built, not live-verified.*

**Context.** A pull failed with pandas' `InvalidIndexError` while merging:
the training workbook held one email on two identical rows. The collector
fetched each email a second time by its place in `folder.Items`, which is a
new, unsorted list on every read, and places shift when an email arrives or
is moved during the export. So an email could be taken twice and another
missed, and the emails taken were not the newest.

**Decision.**
- The collector keeps each email's entry ID and fetches it by that ID; an
  ID already taken is skipped.
- The merge keeps one row per email: rows of one email are merged, each
  column taking its first filled value; rows that disagree on a person's
  label stop the pull before anything is written, naming the email and the
  column. The merged workbook is checked to hold each email once.

**Consequences.** The next pull merges the identical duplicate in the
active workbook into one row, losing nothing; no separate repair is
needed for it.

## D-059 An email moved by hand is found again; the pull lists emails quickly

*Agreed with the product owner. Built, not live-verified.*

**Context.** A reply's category failed every run with Outlook's
`0x8004010F` (item not found): the team had moved the email to
Actioned/Finished by hand after sending the agent's draft, which gives it a
new entry ID, and a Replied email is not watched for moves (D-041). The
cleanup of files to send by hand stopped on a folder Windows would not
delete; that folder was left by tests run before they
blanked `POD_MANUAL_UPLOAD_DIR`. And the historical pull seemed to hang: it
opened every email of the last year in every folder, one by one, before it
printed anything.

**Decision.**
- When an email cannot be opened by its entry ID, the categories and the
  move to Actioned/Finished look it up by its Internet Message ID: in the
  POD mailbox folder and Actioned/Finished first, then in every folder of
  the mailbox (product owner). The new entry ID is kept; an email found already in
  Actioned/Finished is recorded as moved.
- The cleanup removes each folder on its own, read-only files included; a
  folder it cannot remove is counted and tried again next run.
- The historical pull lists each folder's emails through Outlook's table
  (entry ID and received time only, newest first), skips folders that hold
  no emails (calendars, contacts, tasks), and names each folder before it
  reads it, with the number of emails found. Archive folders stay in.

## D-060 Connote prefixes, targets and file names set in .env

*Agreed with the product owner. Built.*

**Context.** The letters a connote may start with were written in the code,
and they belong to particular customers. The two targets (D-054) were
already read from `.env`, but `.env.example` did not list them.

**Decision.**
- `POD_CONNOTE_PREFIXES` lists the letters a connote may start with,
  separated by ";", tried in the order given, as the code did. Empty: only
  connotes of 6 to 10 digits. The regex reads it on every check.
- `.env.example` lists `POD_PILOT_TARGET` and `POD_PRECISION_TARGET`; the
  code keeps 0.90 only for a `.env` without them.
- The training workbook's file name is `POD_TRAINING_DATA_FILE`, in
  `POD_SHAREPOINT_DATA_DIR`: required, the agent, the dashboard and
  training stop at start naming it. The master number list
  (`POD_MASTER_NUMBER_DATA`) has no default name: empty, it is not used.
- Examples in the docs and tests use neutral prefixes and names.

**Consequences.** A `.env` from before this needs the
`POD_CONNOTE_PREFIXES` line with the prefixes it relied on, or prefixed
connotes are no longer read from emails; a `POD_TRAINING_DATA_FILE` line
with the workbook's name; and a `POD_MASTER_NUMBER_DATA` line if it relied
on the master list's old default name.

## D-061 No Microsoft Graph: a card's button opens the POD mailbox

*Agreed with the product owner. Built.*

**Context.** D-051's button asked Microsoft Graph, through Flow 3, for the
email's own link in Outlook on the web. There is no access to Microsoft
Graph at all (product owner), so that lookup never found anything, and the button
always opened the POD mailbox.

**Decision.** Microsoft Graph is not used. Every card's button is **Open
the POD mailbox** and opens it in Outlook on the web; the Teams file no
longer carries `open_email`, and Flow 3 posts the card as written, with no
HTTP step.

**Consequences.** Nothing changes for the team: the button opens what it
opened before. A Flow 3 that still has the HTTP step from D-051 needs its
Message set back to the card as written (`docs/POWER_AUTOMATE_FLOW.md`).

