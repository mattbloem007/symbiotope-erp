"""Account mail: send it as the site, not as whoever pressed Save.

Frappe decides the From address of welcome and password-reset mail from the
*logged-in user*. `User.send_login_mail` stamps

    sender = session.user not in STANDARD_USERS and get_formatted_email(session.user)

so when an admin creates a user in Desk, the registration link goes out as
the admin's own personal address:

    From: Matthew Bloemetje <matthew.bloemetje@proton.me>

The message then leaves the site through Frappe Cloud's Email Delivery
Service, which signs and sends it under the site's own domain. From domain and
sending domain no longer match, so DMARC fails at the far end: Gmail and
Outlook quarantine or drop it, the relay still reports the message as
accepted, and the Email Queue row reads "Sent". Nothing arrives and nothing
errors -- the silent failure this override exists to remove.

What it does instead is what every other mail from this site already does: send
from the site's own address, which the relay is authorised for. The person who
created the account is kept as Reply-To, so a reply still reaches a human, and
their name still appears in the body ("created by ...").

The two addresses can be handed to the business instead, on Company (see
`setup_user_mail_fields`). That is a two-part change, and the field is only
half of it: a From address on your own domain is not authorised by the site's
relay just because it is typed somewhere. Until the sending domain is verified
with whoever sends the mail, an `info@symbiotope.com` From fails SPF and DKIM
and lands in spam -- the same failure, wearing a better-looking address. So the
fields start empty and this module keeps sending as the site until someone who
knows the mail setup fills them in.

Why a `User` controller override rather than a rewritten mail flow: Frappe
builds these messages itself -- subject from the `welcome_email` hook, the
system's own templates, the reset link, the wrapper -- and rebuilding that here
would drift from upstream. Only the sender is changed, and only for the length
of that one call.
"""

import email.utils
from contextlib import contextmanager

import frappe
from frappe import STANDARD_USERS
from frappe.core.doctype.user.user import User
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import get_formatted_email


class SymbiotopeUser(User):
    def send_login_mail(self, *args, **kwargs):
        """Everything Frappe does, sent as the site. See the module docstring."""
        with _sent_as_site():
            return super().send_login_mail(*args, **kwargs)


# ---------------------------------------------------------------------------
# Setup (runs after every migrate)
# ---------------------------------------------------------------------------


def setup_user_mail_fields():
    """Where account mail is addressed from, and where a reply goes.

    Two fields rather than constants in this file because the From address is
    only worth setting once its domain is authorised to send. On Frappe Cloud
    the relay signs for the site's own subdomain and nothing else, so an address
    on the company's domain needs that domain added to the Email Delivery
    Service (or sending moved off it entirely) before it is used here.

    Left empty -- which is how they are created -- mail goes out exactly as
    before: from the site's address, with the person who created the account as
    Reply-To. Filling them in is therefore a deliberate act, not a side effect of
    installing the app or of migrating a site that happens to own a domain.
    """
    create_custom_fields(
        {
            "Company": [
                {
                    "fieldname": "user_mail_from",
                    "fieldtype": "Data",
                    "options": "Email",
                    "label": "User Mail From",
                    "insert_after": "email",
                    "description": (
                        "The address welcome and password-reset mail is sent "
                        "from. Empty means the site's own sending address, which "
                        "always works. Set it to an address on your own domain "
                        "only after that domain is verified with the mail "
                        "service, or the mail is delivered as spam."
                    ),
                },
                {
                    "fieldname": "user_mail_reply_to",
                    "fieldtype": "Data",
                    "options": "Email",
                    "label": "User Mail Reply-To",
                    "insert_after": "user_mail_from",
                    "description": (
                        "Where a reply to that mail is sent, such as a shared "
                        "mailbox. Empty means the person who created the account."
                    ),
                },
            ],
        },
        update=True,
    )


# ---------------------------------------------------------------------------
# Who the mail is from, and where the answer goes
# ---------------------------------------------------------------------------


def _from_address():
    """`Name <address>` the account mail is sent from, or None.

    Company's "User Mail From" wins when it is set. Otherwise this is read from
    the default outgoing Email Account rather than written down here: on Frappe
    Cloud no Email Account row exists -- the account is assembled from the site
    config -- so that is also where a later move to a real mailbox would be
    picked up with no code change. Returning None lets Frappe fall back on the
    same account anyway; the reason for passing it explicitly is the display
    name, which would otherwise be the account's own ("Frappe").
    """
    # Imported here rather than at module level: this module is loaded as the
    # User controller, which Frappe imports on almost every request, and the
    # email package has no business being pulled in that early.
    from frappe.email.doctype.email_account.email_account import EmailAccount

    address = _company_value("user_mail_from")
    if address:
        return email.utils.formataddr((_site_name() or "Symbiotope", address))

    account = EmailAccount.find_default_outgoing()
    address = (account and account.email_id) or ""
    if not address:
        return None
    return email.utils.formataddr((_site_name() or account.name, address))


def _reply_to():
    """Where a reply should land, as `Name <address>`, or None.

    Company's "User Mail Reply-To" wins when it is set -- a shared inbox reads
    better than one person's desk for anything sent in the company's name.
    Otherwise it is the person whose action triggered the mail: on a welcome
    mail, the admin who created the account. It is a Reply-To, not a sender: it
    says where an answer should go, not who the mail is from.
    """
    address = _company_value("user_mail_reply_to")
    if address:
        return address

    user = frappe.session.user
    if not user or user in STANDARD_USERS:
        return None
    return get_formatted_email(user) or user


def _company_value(fieldname):
    """One of the fields above, as a stripped string or None.

    Read through the cached document rather than as a column query: the fields
    are created by after_migrate, so on the first run after an install the
    column may not exist yet, and `Document.get` on a missing field is simply
    None instead of an SQL error.
    """
    company = frappe.db.get_default("company")
    if not company:
        companies = frappe.get_all("Company", pluck="name", limit=1)
        company = companies[0] if companies else None
    if not company:
        return None

    value = frappe.get_cached_doc("Company", company).get(fieldname)
    return (value or "").strip() or None


def _site_name():
    """The site's name, looked up exactly as Frappe does for the subject line."""
    return frappe.db.get_default("site_name") or frappe.get_conf().get("site_name")


@contextmanager
def _sent_as_site():
    """Route `frappe.sendmail` through the site's address for the block.

    Frappe's own send path has no per-message hook for this: the sender is
    decided inside `send_login_mail`, and the only other lever is the
    Email Account setting "Always use Account's Email Address as Sender", which
    is site configuration rather than code. Wrapping the call is the narrowest
    version of that: the swap lasts one statement and is put back in `finally`.
    """
    original = frappe.sendmail

    def sendmail(*args, **kwargs):
        # Always assigned, even when it resolves to None: None means "use the
        # site's default account", which is right, and it guarantees the
        # inviter's address can never end up as From.
        kwargs["sender"] = _from_address()
        if not kwargs.get("reply_to") and (reply_to := _reply_to()):
            kwargs["reply_to"] = reply_to
        return original(*args, **kwargs)

    frappe.sendmail = sendmail
    try:
        yield
    finally:
        frappe.sendmail = original
