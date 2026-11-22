"""Read-only check of where drafts go (D-032): `python -m app.check_drafts`.

Prints the mailbox the agent reads, the Drafts folder it now saves drafts in,
the folder Outlook's "default Drafts" gives for that mailbox (what the agent
used before), and the folder the agent's latest draft is really in. It
creates, moves and deletes nothing.
"""

from __future__ import annotations

from . import config, outlook_reader, tracking


def _try(label: str, read) -> None:
    try:
        print(f"{label}: {read()}")
    except Exception as error:  # a check must report, not stop
        print(f"{label}: not available ({type(error).__name__}: {error})")


def main() -> None:
    namespace = outlook_reader.connect_outlook()
    folder = outlook_reader.get_folder(namespace, config.OUTLOOK_MAILBOX_NAME)
    store = folder.Store
    print(f"POD_OUTLOOK_MAILBOX: {config.OUTLOOK_MAILBOX_NAME} -> {folder.FolderPath}")
    print(f"Its mailbox: {store.DisplayName}")
    _try("Your own mailbox (Outlook's default)", lambda: namespace.DefaultStore.DisplayName)
    _try("Drafts the agent uses now", lambda: outlook_reader.mailbox_drafts(store).FolderPath)
    _try("Outlook's default Drafts for that mailbox (used before)", lambda: store.GetDefaultFolder(16).FolderPath)

    drafts = [event for event in tracking.latest(tracking.REPLIED) if "draft_entry_id" in event["state"]]
    if not drafts:
        print("Latest agent draft: none in tracking yet")
        return
    latest = max(drafts, key=lambda event: event["state"].get("drafted_at", ""))
    state = latest["state"]
    print(f"Latest agent draft: '{latest['subject']}'")

    def where() -> str:
        store_id = state.get("draft_store_id", "")
        item = (namespace.GetItemFromID(state["draft_entry_id"], store_id) if store_id
                else namespace.GetItemFromID(state["draft_entry_id"]))
        return f"{item.Parent.FolderPath}{' (already sent)' if getattr(item, 'Sent', False) else ''}"

    _try("  it is in", where)


if __name__ == "__main__":
    main()
