# Handbook

This file holds the facts about the outside world and the rules this
project follows. Each rule names, in brackets, the defect it prevents.
Before you delete a rule, say what will prevent that defect instead.
`docs/DECISIONS.md` holds the decisions and their reasons; this file holds
what must stay true.

Each fact says how we know it:

- **code**: read in this repository;
- **docs**: vendor documentation or well-known platform behaviour;
- **product owner**: told by the product owner;
- **unverified**: still to try in production, and listed under Known gaps
  in `docs/PROGRESS.md` until confirmed.

## 1. Facts about the outside world

### The PC running the agent

- PyTorch installs, but the PC cannot reach Hugging Face, so it cannot
  download a pretrained language model such as DistilBERT. *(product owner)*
- The PC runs Python 3.14. *(product owner)* The models were trained with
  scikit-learn 1.8, which needs Python 3.11 or newer; CI tests 3.13 and
  3.14.
- The PC has no Outlook API access. Outlook Desktop is installed and signed
  in to the POD mailbox. *(product owner)*
- The agent runs only while the PC is on. *(product owner)*

### Outlook Desktop (COM through `pywin32`)

- Outlook on the PC runs in English. *(product owner)* A reply keeps
  Outlook's quoted email by finding its English header (`From:` ...
  `Subject:`). Without that header the agent refuses the reply rather than
  send it without the quote (`outlook_reader._outlook_quoted_html`).
- For internal senders (`SenderEmailType` is `EX`), `SenderEmailAddress`
  holds an Exchange address (`/O=EXCHANGE...`), not SMTP. The SMTP address
  comes from `Sender.GetExchangeUser().PrimarySmtpAddress`. *(docs)*
- `MessageClass` tells real mail (`IPM.Note`) from auto-replies
  (`IPM.Note.Rules.OofTemplate.Microsoft`) and bounces (`REPORT.IPM.Note.*`).
  *(docs)*
- The reading pane can mark an email read just by showing it, depending on
  each user's Outlook setting (File -> Options -> Mail -> Reading Pane).
  *(docs)*
- `Items.Restrict("[UnRead] = True")` returns only unread items. *(docs;
  unverified on the shared mailbox)*
- `MailItem.SentOnBehalfOfName` sends a reply from another mailbox when the
  user has send-as or send-on-behalf rights on it. *(docs; unverified)*
- A saved draft has an `EntryID`; once sent, it moves to Sent Items with a
  new one. A reply keeps the original's `ConversationID`. Which Sent Items
  holds a reply sent on behalf of the POD mailbox (the mailbox's or the
  user's) depends on the account. pywin32 returns Outlook dates as local
  wall-clock time. *(docs; unverified, and the pilot measure relies on
  them)*
- `Items.Restrict` filters on the server; looping over `Items` reads every
  item in the folder. *(docs)*
- Microsoft does not support unattended Office automation on servers.
  *(docs)*
- The mailbox root name must match `POD_OUTLOOK_MAILBOX` exactly; the agent
  never falls back to the personal mailbox. *(code: `app/outlook_reader.py`)*

### Power Automate (cloud)

- The Excel Online (Business) connector reads and writes rows only inside an
  Excel **Table**. *(docs)*
- The Power BI action "Run a query against a dataset" runs a DAX query, and
  its connector is standard. *(docs)*
- Teams "Post adaptive card and wait for a response" can post to a group
  chat and wait for a click. *(docs)*
- SharePoint "When a file is created in a folder" is a standard trigger.
  *(docs)*
- "Add a row into a table" takes a whole row as one JSON object.
  *(unverified)*

### SharePoint and network share

- The handoff folder is a SharePoint library synced to the PC; the agent
  reads and writes it as a local path. *(product owner)*
- The POD Outstanding workbook sits in SharePoint, **not** synced to the PC,
  and is already an Excel Table that Power Automate uses. Connote and
  reference are separate columns. *(product owner)*
- POD scans are PDFs under one root folder per year (2025, 2026, then
  2027), in nested subfolders, about 10,000 files a year, with names that
  follow no single pattern, such as `12345_absd.pdf` and
  `AB-10234_102cd_x1y2345.pdf`. *(product owner)*
- How long the agent takes to walk the share. *(unverified)*

### Power BI dataset

- Connote and reference are separate columns. POD status values include
  "Missing POD" and "Not Received". *(product owner)*
- TMS can hold two rows for one connote with different POD statuses, one of
  them "PickUp". The row that counts is the one that is not PickUp.
  *(product owner)*
- The lookup uses three tables. TMS holds connote, reference, sender,
  receiver, date and POD status. POD Scans holds the scan paths. A second
  table holds sender, receiver, POD status and other details for some
  customers, whose consignments appear there and not in TMS; their POD PDFs
  sit on the same share as the others. *(product owner)* The column names
  and the linking columns go in `.env`.
- Some real POD numbers are missing from the dataset; the team records them
  as "Not in the System". A number missing from Power BI may still be a POD
  number, so the agent must recognise numbers in the email first, and not
  only look them up. *(product owner)*

### Data

- An earlier number model's test set was heavily imbalanced: 53 CONNOTE, 19
  REFERENCE, 1,301 OTHER. Its best CONNOTE F1 was 0.38. That model is gone;
  the CRF replaced it (D-026).

## 2. Design rules

1. **Never reply when unsure.** An ambiguous or unknown number holds the
   whole email for a person. [Contains-matching makes `12345` match
   `123456_x.pdf`, and an automatic reply would send the wrong POD.]
2. **Attach every found file.** [The first version attached only
   `found[0]`, so a customer who asked for three PODs got one.]
3. **Give every requested number an outcome in the reply.** [The first
   version dropped not-found numbers without a word when any number was
   found.]
4. **Say in every automatic reply that it is automatic.** [A customer takes
   a machine reply as a person's promise.]
5. **Never reply to an auto-reply, a bounce, a no-reply sender or the POD
   mailbox itself.** [Two auto-responders reply to each other without end.]
6. **Use Outlook's own reply recipients.** [The first version set `To` to
   `SenderEmailAddress`, which is not an SMTP address for internal senders.]
7. **Process only unread emails.** [A colleague may already have handled a
   read email, and the customer would get two answers.]
8. **Mark a replied email read; leave a held email unread.** [Otherwise the
   team cannot see which emails still need a person.]
9. **Clean file names and numbers with one function.** [Two copies of the
   rule drift apart, and a number stops matching its own file.]
10. **Keep business rules in Python; Power Automate flows carry no logic.**
    [Logic split across flows cannot be tested offline or moved to a
    server.]
11. **Send Power Automate one JSON file per message, never a shared Excel
    file.** [`pandas.to_excel` writes a plain sheet, which the Excel
    connector cannot read, and rewrites the whole file, which clashes with
    SharePoint sync.]
12. **Append to tracking, one row per status event.** [An overwritten status
    loses why an email ended where it did.]
13. **Archive handoff files; never delete them.** [A parser fix cannot
    re-read evidence that is gone.]
14. **Run one cycle at a time.** [The scheduler and a dashboard button would
    run together and reply twice.]
15. **Keep anything site-specific in settings**: folders, Power BI names,
    Outstanding columns, threshold, send mode. [Nobody knows the site's
    names in advance, and a wrong column name would need a code change.]
16. **Let a person choose the active model.** [The old automatic choice
    preferred the smallest train/test gap over the best test score.]
17. **Stop the run when the share cannot be read.** [Every POD would look
    missing and go out as "not received".]
18. **Build everything that can fail before replying.** The agent builds the
    Outstanding rows first. [A reply sent before a failing step leaves the
    customer answered but Outstanding unchanged.]
19. **Never send a reply over the attachment limit.** The agent drafts it
    without the files, and the team attaches them (D-050). [Outlook or the
    recipient's server rejects the email, and the customer gets nothing.]

## 3. Standards

- Every change has a test, including a failure case. Tests run without
  Outlook, the network share or Power Automate, on fakes and temporary
  folders.
- Dependencies that can break the install carry an upper version bound.
- The models are scikit-learn (intent), a CRF (numbers) and regex; nothing
  needs a pretrained model. [Pretrained models come from Hugging Face, which
  the PC cannot reach. A PyTorch network trained from scratch lost to
  XGBoost and to regex on the labeled data (D-021).]
- A number model gets judged email by email against regex, with folds split
  by email. [Per-token scores, with one email's tokens in both training and
  test, made a weak model look strong (D-022, D-026).]
- Errors go into tracking per email, never only to the console. [Nobody
  reads the console of a scheduled job.]

## 4. Security

- The dashboard listens on `localhost` only (`.streamlit/config.toml`).
  [Streamlit listens on every network address by default, so anyone on the
  office network could open it and switch sending on.]
- The dashboard may write only a fixed list of settings to `.env`
  (`config.EDITABLE_SETTINGS`: threshold, targets, send mode and domains,
  finished-email moving, auto-refresh), and checks each value. A choice that
  sends more replies needs a second click to confirm. [One slip of the mouse
  would start sending replies to customers.]
- Replies stay Outlook drafts until the pilot passes
  (`POD_SEND_MODE=draft`). A `STOP` file in the project folder stops all
  drafting and sending.
- Secrets and site paths come from `.env`, which git never sees.
- Never commit customer data: training data, tracking, attachments, logs,
  handoff files.
- Attach only files found under the scan folders set in `.env`. [A path from
  elsewhere could send an unrelated document to a customer.]

## 5. Scope

In scope: POD request emails to the POD mailbox; replies with PODs from the
network share; not-received replies; the POD Outstanding list; the
dashboard for labeling, training and monitoring.

Out of scope: follow-up emails when a POD arrives later; answering non-POD
emails; edits to Outstanding rows beyond adding or removing requested
numbers.
