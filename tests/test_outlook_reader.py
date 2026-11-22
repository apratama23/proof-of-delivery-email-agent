"""Reading unread mail and replying through Outlook, against fake COM objects (D-002, D-008)."""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import config, outlook_reader, reply_generator


class FakeItems:
    """Outlook's Items collection: 1-based Item(), Count, Restrict and Sort."""

    def __init__(self, items):
        self.items = list(items)
        self.filters = []

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index - 1]

    def Restrict(self, condition):
        self.filters.append(condition)
        return self

    def Sort(self, key, descending=False):
        pass


def _mail(entry_id, body, **extra):
    fields = {
        "EntryID": entry_id, "MessageClass": "IPM.Note", "Subject": "POD request", "SenderEmailType": "SMTP",
        "SenderEmailAddress": "ops@example1.com", "To": "POD", "ReceivedTime": datetime(2026, 10, 14, 9, 0), "Body": body,
    }
    return SimpleNamespace(**{**fields, **extra})


class FakeRecipient:
    def __init__(self, recipients, address, kind):
        self.recipients, self.Address, self.Type = recipients, address, kind
        self.AddressEntry = SimpleNamespace(GetExchangeUser=lambda: None)


class FakeRecipients(FakeItems):
    """A reply's Recipients: Add and ResolveAll."""

    def Add(self, address):
        recipient = FakeRecipient(self, address, outlook_reader.OL_TO)
        self.items.append(recipient)
        return recipient

    def ResolveAll(self):
        return True

    def of(self, kind):
        return [recipient.Address for recipient in self.items if recipient.Type == kind]


class PodMailboxInCcTest(unittest.TestCase):
    """The POD mailbox goes in CC, never To, whatever Reply All gave (product owner, D-055)."""

    def recipients(self, *entries):
        recipients = FakeRecipients([])
        recipients.items = [FakeRecipient(recipients, address, kind) for address, kind in entries]
        return recipients

    def test_moved_from_to_to_cc(self):
        recipients = self.recipients(("ops@example1.com", outlook_reader.OL_TO), ("POD@example.com", outlook_reader.OL_TO))
        outlook_reader._pod_mailbox_in_cc(recipients, "pod@example.com")
        self.assertEqual((recipients.of(outlook_reader.OL_TO), recipients.of(outlook_reader.OL_CC)),
                         (["ops@example1.com"], ["POD@example.com"]))

    def test_added_to_cc_when_the_email_was_not_sent_to_it(self):
        recipients = self.recipients(("ops@example1.com", outlook_reader.OL_TO), ("boss@example1.com", outlook_reader.OL_CC))
        outlook_reader._pod_mailbox_in_cc(recipients, "pod@example.com")
        self.assertEqual((recipients.of(outlook_reader.OL_TO), recipients.of(outlook_reader.OL_CC)),
                         (["ops@example1.com"], ["boss@example1.com", "pod@example.com"]))

    def test_the_team_reply_says_whom_it_was_sent_for(self):
        accessor = SimpleNamespace(GetProperty=lambda name: "pod@example.com" if name == outlook_reader._SENT_FOR else "")
        email = outlook_reader.get_unread_emails(
            SimpleNamespace(Items=FakeItems([_mail("E9", "Hi", PropertyAccessor=accessor)])), processed_ids=set())[0]
        self.assertEqual(email["sent_for"], "pod@example.com")


class FindByMessageIdTest(unittest.TestCase):
    """An email moved by hand is found by its Internet Message ID (D-059)."""

    def folder(self, name, ids=(), children=()):
        def restrict(condition):
            return FakeItems([SimpleNamespace(EntryID=f"{name}-{i}") for i in ids if f"'{i}'" in condition])
        return SimpleNamespace(Name=name, Items=SimpleNamespace(Restrict=restrict), Folders=FakeItems(children))

    def mailbox(self, inbox_ids=(), finished_ids=(), other_ids=()):
        finished = self.folder("Actioned/Finished", finished_ids)
        inbox = self.folder("Inbox", inbox_ids, [finished])
        other = self.folder("Customers", other_ids)
        inbox.Store = SimpleNamespace(GetRootFolder=lambda: self.folder("POD", (), [inbox, other]))
        return inbox

    def test_the_finished_folder_and_then_any_folder(self):
        find = outlook_reader.find_by_message_id
        self.assertEqual(find(self.mailbox(finished_ids=["<m1>"]), "<m1>", "Actioned/Finished"),
                         {"entry_id": "Actioned/Finished-<m1>", "in_finished": True})
        self.assertEqual(find(self.mailbox(other_ids=["<m1>"]), "<m1>", "Actioned/Finished"),
                         {"entry_id": "Customers-<m1>", "in_finished": False})
        self.assertIsNone(find(self.mailbox(), "<m1>", "Actioned/Finished"))
        self.assertIsNone(find(self.mailbox(finished_ids=["<m1>"]), "", "Actioned/Finished"))


class UnreadEmailsTest(unittest.TestCase):
    def test_only_unread_and_new_emails_are_read_with_the_newest_message_apart(self):
        internal = _mail(
            "E2", "Please send 1234567\n\nFrom: Old Sender\nSent: Monday\nTo: POD\nSubject: old\nAlso 7654321",
            SenderEmailType="EX", SenderEmailAddress="/O=EXCHANGELABS/CN=RECIPIENTS/CN=jsmith",
            Sender=SimpleNamespace(GetExchangeUser=lambda: SimpleNamespace(PrimarySmtpAddress="j.smith@example6.com")),
        )
        items = FakeItems([_mail("OLD", "already handled"), internal])
        emails = outlook_reader.get_unread_emails(SimpleNamespace(Items=items), processed_ids={"OLD"})

        self.assertEqual(items.filters, ["[UnRead] = True"])  # read emails are never touched
        self.assertEqual([email["entry_id"] for email in emails], ["E2"])
        email = emails[0]
        self.assertEqual(email["sender_email"], "j.smith@example6.com")
        self.assertIn("1234567", email["body"])
        self.assertNotIn("7654321", email["body"])  # quoted history is left out of the body
        self.assertIn("7654321", email["full_body"])  # but kept for the forwarded-request fallback


class WhereIsTheEmailTest(unittest.TestCase):
    """Finding a waiting email again after a person moved it (D-041)."""

    def test_the_internet_message_id_is_read_with_each_new_email(self):
        mail = _mail("E3", "POD 1234567", PropertyAccessor=SimpleNamespace(GetProperty=lambda name: "<m1@example1.com>"))
        emails = outlook_reader.get_unread_emails(SimpleNamespace(Items=FakeItems([mail])), processed_ids=set())
        self.assertEqual(emails[0]["message_id"], "<m1@example1.com>")

    def test_an_email_outlook_can_open_gives_its_folder_and_one_it_cannot_gives_none(self):
        item = mock.Mock()
        item.PropertyAccessor.GetProperty.side_effect = RuntimeError("The property is unknown")
        namespace = mock.Mock()
        namespace.GetItemFromID.return_value = item
        with mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace):
            self.assertEqual(outlook_reader.email_status("E1", "POD_STORE"), {"folder": item.Parent, "manual": None})
            namespace.GetItemFromID.side_effect = RuntimeError("The message could not be found")
            self.assertIsNone(outlook_reader.email_status("E1", "POD_STORE"))

    def test_a_moved_email_is_looked_for_in_the_finished_folder_by_its_message_id(self):
        finished_items = FakeItems([SimpleNamespace()])
        parent = SimpleNamespace(Folders=FakeItems([SimpleNamespace(Name="Actioned/Finished", Items=finished_items)]))
        self.assertTrue(outlook_reader.in_child_folder(parent, "Actioned/Finished", "<it's@example1.com>"))
        self.assertIn("= '<it''s@example1.com>'", finished_items.filters[0])  # a quote in the id is escaped
        self.assertFalse(outlook_reader.in_child_folder(parent, "Elsewhere", "<m@example1.com>"))
        self.assertFalse(outlook_reader.in_child_folder(parent, "Actioned/Finished", ""))


class ReplyTest(unittest.TestCase):
    def setUp(self):
        # The company signature comes from a file outside the repository (POD_SIGNATURE_FILE).
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        signature = Path(self._tmp.name) / "signature.html"
        signature.write_text(
            '<div style="color:#1f4e79;">Kind Regards,<br>POD Teams</div>'
            "<p>Example1 Company</p><a href=\"https://www.example.com\">www.example.com</a>",
            encoding="utf-8",
        )
        env = mock.patch.dict(os.environ, {"POD_SIGNATURE_FILE": str(signature)})
        env.start()
        self.addCleanup(env.stop)
        self.moved_response = mock.MagicMock(EntryID="D2")
        self.response = mock.MagicMock(EntryID="D1")
        self.response.Body = "Outlook signature\n\nFrom: customer@example.com\nOriginal message"
        self.response.HTMLBody = (
            '<html xmlns:o="urn:schemas-microsoft-com:office:office">'
            "<head><style>.MsoNormal{font-family:Calibri;}</style></head>"
            '<body style="font-family:Calibri;">'
            "<div>Kind Regards,<br>Jane Doe<br>Personal signature</div>"
            "<hr><div id=\"divRplyFwdMsg\"><b>From:</b> customer@example.com<br>"
            "<b>Sent:</b> Friday<br><b>To:</b> POD<br><b>Subject:</b> POD request"
            "<p>Original message with <b>Outlook formatting</b>.</p></div></body></html>"
        )
        # The POD mailbox: its own Drafts directly under its root, next to Inbox.
        self.pod_drafts = SimpleNamespace(Name="Drafts", FolderPath="\\\\POD\\Drafts")
        pod_store = SimpleNamespace(
            StoreID="POD_STORE", DisplayName="POD",
            GetRootFolder=lambda: SimpleNamespace(Folders=FakeItems([SimpleNamespace(Name="Inbox"), self.pod_drafts])),
        )
        self.moved_response.Parent = SimpleNamespace(Store=pod_store, FolderPath=self.pod_drafts.FolderPath)
        self.response.Move.return_value = self.moved_response
        original = mock.MagicMock(ConversationID="C1")
        original.Parent.Store = pod_store
        original.ReplyAll.return_value = self.response
        namespace = mock.MagicMock()
        namespace.GetItemFromID.return_value = original
        patcher = mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_reply_has_the_pod_mailbox_in_cc_not_to(self):
        recipients = FakeRecipients([])
        recipients.items = [FakeRecipient(recipients, "customer@example.com", outlook_reader.OL_TO),
                            FakeRecipient(recipients, "pod@example.com", outlook_reader.OL_TO)]
        self.response.Recipients = recipients
        body = "Hi\n\n" + outlook_reader.reply_generator.SIGN_OFF
        outlook_reader.reply("E1", body, [], "draft", send_as="pod@example.com", store_id="POD_STORE")
        self.assertEqual((recipients.of(outlook_reader.OL_TO), recipients.of(outlook_reader.OL_CC)),
                         (["customer@example.com"], ["pod@example.com"]))

    def test_draft_mode_saves_a_draft_and_sends_nothing(self):
        body = (
            "Hi\n\n" + outlook_reader.reply_generator.AUTOMATED_NOTICE + "\n\n"
            + outlook_reader.reply_generator.SIGN_OFF
        )
        result = outlook_reader.reply(
            "E1", body, [Path("C:/scans/1234567.pdf")], "draft", send_as="pod@example.com", store_id="POD_STORE"
        )
        namespace = outlook_reader.connect_outlook()
        namespace.GetItemFromID.assert_called_once_with("E1", "POD_STORE")  # opened in the POD mailbox
        original = namespace.GetItemFromID.return_value
        original.ReplyAll.assert_called_once_with()
        self.response.Save.assert_called_once()
        self.response.Move.assert_called_once_with(self.pod_drafts)  # the POD mailbox's own Drafts
        self.response.Send.assert_not_called()
        self.assertEqual(self.response.SentOnBehalfOfName, "pod@example.com")
        html_body = self.response.HTMLBody
        self.assertIn("Hi", html_body)
        self.assertIn(
            '<p style="margin:0 0 12px;font-size:9pt;font-style:italic;color:#666666;">'
            "This email was automatically generated by the POD Email Agent.</p>",
            html_body,
        )
        self.assertIn("POD Teams", html_body)
        self.assertIn("#1f4e79", html_body)
        self.assertIn(".MsoNormal{font-family:Calibri;}", html_body)
        self.assertTrue(html_body.startswith('<html xmlns:o="urn:schemas-microsoft-com:office:office">'))
        self.assertIn('<body style="font-family:Calibri;">', html_body)
        self.assertEqual(html_body.count("<html"), 1)
        self.assertIn("Example1 Company", html_body)  # from the signature file
        self.assertIn("https://www.example.com", html_body)
        self.assertIn("<hr><div id=\"divRplyFwdMsg\">", html_body)
        self.assertIn("<p>Original message with <b>Outlook formatting</b>.</p>", html_body)
        self.assertNotIn("Jane Doe", html_body)
        self.assertNotIn("Personal signature", html_body)
        self.response.Attachments.Add.assert_called_once_with(str(Path("C:/scans/1234567.pdf")))
        self.assertEqual(
            result,
            {
                "draft_entry_id": "D2",
                "draft_store_id": "POD_STORE",
                "conversation_id": "C1",
            },
        )

    def test_a_draft_that_lands_elsewhere_is_an_error_naming_where(self):
        self.moved_response.Parent = SimpleNamespace(
            Store=SimpleNamespace(StoreID="PERSONAL_STORE"), FolderPath="\\\\me@example.com\\Drafts"
        )
        body = "Hi\n\n" + outlook_reader.reply_generator.SIGN_OFF
        with self.assertRaisesRegex(outlook_reader.DraftOutsideMailbox, r"me@example.com.*not in '\\\\POD"):
            outlook_reader.reply("E1", body, [], "draft", store_id="POD_STORE")

    def test_without_a_drafts_folder_in_the_mailbox_no_draft_is_made(self):
        self.pod_drafts.Name = "Something else"
        with self.assertRaisesRegex(LookupError, "No 'Drafts' folder"):
            outlook_reader.reply("E1", "Hi", [], "draft", store_id="POD_STORE")
        self.response.Save.assert_not_called()

    def test_draft_exists_looks_in_the_saved_store(self):
        namespace = outlook_reader.connect_outlook()
        namespace.GetItemFromID.return_value.Sent = False

        self.assertTrue(outlook_reader.draft_exists("D2", "SHARED_STORE"))

        namespace.GetItemFromID.assert_called_once_with("D2", "SHARED_STORE")

    def test_send_mode_sends(self):
        body = (
            "Hi\n\n" + outlook_reader.reply_generator.AUTOMATED_NOTICE + "\n\n"
            + outlook_reader.reply_generator.SIGN_OFF
        )
        result = outlook_reader.reply("E1", body, [], "send")
        self.response.Send.assert_called_once()
        self.response.Save.assert_not_called()
        self.assertEqual(result, {"conversation_id": "C1"})

    def test_an_unknown_mode_is_refused_before_outlook_is_touched(self):
        with self.assertRaises(ValueError):
            outlook_reader.reply("E1", "Hi", [], "maybe")
        outlook_reader.connect_outlook.assert_not_called()

    def test_outlook_quote_without_reply_forward_marker_is_kept_from_its_separator(self):
        original_html = (
            "<html><body><div>Personal signature</div><hr><div><b>From:</b> sender@example.com"
            "<br><b>Sent:</b> Friday<br><b>Subject:</b> POD request<p>Original message</p>"
            "</div></body></html>"
        )
        quoted = outlook_reader._outlook_quoted_html(original_html)
        self.assertTrue(quoted.startswith("<hr>"))
        self.assertIn("<p>Original message</p>", quoted)
        self.assertNotIn("Personal signature", quoted)


    # Classic Outlook's own Reply All markup: the separator is the border of the div around "From:".
    CLASSIC_REPLY = (
        "<html><head></head><body lang=EN-AU><div class=WordSection1>"
        "<p class=MsoNormal><o:p>&nbsp;</o:p></p><p class=MsoNormal>Personal signature</p>"
        "<div><div style='border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm'>"
        "<p class=MsoNormal><a name=\"_MailOriginal\"><b><span lang=EN-US>From:</span></b></a>"
        "<span lang=EN-US> sender@example.com<br>"
        "<b>Sent:</b> Friday<br><b>Subject:</b> POD request<o:p></o:p></span></p></div></div>"
        "<p class=MsoNormal>Original message</p></div></body></html>"
    )

    def test_classic_outlook_separator_is_kept_above_the_quote(self):
        html = outlook_reader._outlook_reply_html(self.CLASSIC_REPLY, "Hi\n\n" + reply_generator.SIGN_OFF)
        reply, quote = html.split("<div><div style='border:none;border-top:solid #E1E1E1")
        self.assertIn("Hi", reply)
        self.assertNotIn("Personal signature", html)
        self.assertNotIn(outlook_reader.QUOTE_SEPARATOR, html)  # Outlook's own is there
        self.assertIn("<b><span lang=EN-US>From:</span></b>", quote)
        self.assertIn("<p class=MsoNormal>Original message</p>", quote)

    def test_a_quote_with_no_separator_gets_one(self):
        original = ("<html><body><p>Personal signature</p><p class=MsoNormal><b><span>From:</span></b> "
                    "sender@example.com<br><b>Subject:</b> POD request</p><p>Original message</p></body></html>")
        html = outlook_reader._outlook_reply_html(original, "Hi\n\n" + reply_generator.SIGN_OFF)
        self.assertIn(outlook_reader.QUOTE_SEPARATOR + "<p class=MsoNormal><b><span>From:</span></b>", html)
        self.assertNotIn("Personal signature", html)


class SignatureTest(unittest.TestCase):
    def test_without_a_signature_file_the_plain_team_signature_is_used(self):
        with mock.patch.dict(os.environ, {"POD_SIGNATURE_FILE": ""}):
            html = reply_generator.build_reply_html("Hi\n\n" + reply_generator.SIGN_OFF, "<hr>quoted")
        self.assertIn("POD Teams", html)
        self.assertIn(reply_generator.DEFAULT_SIGNATURE_HTML, html)

    def test_a_missing_or_placeholder_signature_file_is_refused_naming_the_setting(self):
        for value in ("data/no-such-signature.html", "<path to signature HTML file>"):
            with mock.patch.dict(os.environ, {"POD_SIGNATURE_FILE": value}):
                with self.assertRaisesRegex(config.ConfigError, "POD_SIGNATURE_FILE"):
                    config.signature_html()


class MoveTest(unittest.TestCase):
    def test_an_email_moves_into_the_child_folder_named_with_a_slash(self):
        finished = SimpleNamespace(Name="Actioned/Finished")
        parent = SimpleNamespace(Name="POD", Store=SimpleNamespace(StoreID="STORE1"),
                                 Folders=FakeItems([SimpleNamespace(Name="Other"), finished]))
        item = mock.Mock()
        item.Move.return_value = SimpleNamespace(EntryID="NEW1")
        namespace = mock.Mock()
        namespace.GetItemFromID.return_value = item
        with mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace):
            self.assertEqual(outlook_reader.move_to_child_folder("E1", parent, "Actioned/Finished"), "NEW1")
        namespace.GetItemFromID.assert_called_once_with("E1", "STORE1")
        item.Move.assert_called_once_with(finished)

    def test_a_missing_folder_is_refused_and_nothing_moves(self):
        parent = SimpleNamespace(Name="POD", Store=SimpleNamespace(StoreID="S"), Folders=FakeItems([]))
        with mock.patch.object(outlook_reader, "connect_outlook") as connect:
            with self.assertRaisesRegex(LookupError, "Actioned/Finished"):
                outlook_reader.move_to_child_folder("E1", parent, "Actioned/Finished")
        connect.assert_not_called()


class RepliedManuallyTest(unittest.TestCase):
    def check(self, properties: dict):
        def get(name):
            if name not in properties:
                raise RuntimeError("The property is unknown")  # as Outlook does for a never-replied email
            return properties[name]

        item = mock.Mock()
        item.PropertyAccessor.GetProperty.side_effect = get
        namespace = mock.Mock()
        namespace.GetItemFromID.return_value = item
        with mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace):
            result = outlook_reader.replied_manually("E1", "POD_STORE")
        namespace.GetItemFromID.assert_called_once_with("E1", "POD_STORE")
        return result

    def test_reply_and_reply_all_count_with_their_time_and_forward_does_not(self):
        when = datetime(2026, 10, 21, 2, 15)
        self.assertEqual(self.check({outlook_reader._LAST_VERB: 103, outlook_reader._LAST_VERB_TIME: when}),
                         {"action": "reply all", "at": "2026-10-21T02:15:00+00:00"})
        self.assertEqual(self.check({outlook_reader._LAST_VERB: 102})["action"], "reply")
        self.assertIsNone(self.check({outlook_reader._LAST_VERB: 104}))  # forwarded only
        self.assertIsNone(self.check({}))  # never answered

    def test_an_email_outlook_cannot_open_raises(self):
        namespace = mock.Mock()
        namespace.GetItemFromID.side_effect = RuntimeError("The message could not be found")
        with mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace):
            with self.assertRaises(RuntimeError):
                outlook_reader.replied_manually("E1", "POD_STORE")


class OpenInOutlookTest(unittest.TestCase):
    def test_the_email_is_opened_in_the_pod_mailbox_and_nothing_else_is_done(self):
        item = mock.Mock()
        namespace = mock.Mock()
        namespace.GetItemFromID.return_value = item
        pod = SimpleNamespace(Store=SimpleNamespace(StoreID="POD_STORE"))
        with mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace), \
                mock.patch.object(outlook_reader, "get_folder", return_value=pod) as get_folder:
            outlook_reader.open_in_outlook("E1", "POD")
        get_folder.assert_called_once_with(namespace, "POD")
        namespace.GetItemFromID.assert_called_once_with("E1", "POD_STORE")  # never the personal mailbox
        self.assertEqual(item.method_calls, [mock.call.Display(False)])  # no reply, move, category or save

    def test_an_email_not_in_the_mailbox_raises(self):
        namespace = mock.Mock()
        namespace.GetItemFromID.side_effect = RuntimeError("The message could not be found")
        with mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace), \
                mock.patch.object(outlook_reader, "get_folder",
                                  return_value=SimpleNamespace(Store=SimpleNamespace(StoreID="S"))):
            with self.assertRaisesRegex(RuntimeError, "could not be found"):
                outlook_reader.open_in_outlook("GONE", "POD")


class CategoriesTest(unittest.TestCase):
    def test_categories_are_added_idempotently_without_removing_existing_ones(self):
        item = SimpleNamespace(Categories="Important, Existing")
        item.Save = mock.Mock()
        namespace = mock.Mock()
        namespace.GetItemFromID.return_value = item
        with mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace):
            added = outlook_reader.add_categories(
                "E1", ["On List", "important", "POD Agent Reply"], "STORE1"
            )
            added_again = outlook_reader.add_categories(
                "E1", ["On List", "POD Agent Reply"], "STORE1"
            )

        namespace.GetItemFromID.assert_any_call("E1", "STORE1")
        self.assertEqual(added, ["On List", "POD Agent Reply"])
        self.assertEqual(added_again, [])
        self.assertEqual(item.Categories, "Important, Existing, On List, POD Agent Reply")
        self.assertEqual(item.Save.call_count, 1)


if __name__ == "__main__":
    unittest.main()
