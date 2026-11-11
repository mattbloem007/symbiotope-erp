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
from frappe.utils import get_formatted_email


class SymbiotopeUser(User):
    def send_login_mail(self, *args, **kwargs):
        """Everything Frappe does, sent as the site. See the module docstring."""
        with _sent_as_site():
            return super().send_login_mail(*args, **kwargs)


def _site_sender():
    """`Name <address>` for the address this site sends from, or None.

    Read from the default outgoing Email Account rather than written down here.
    On Frappe Cloud no Email Account row exists -- the account is assembled from
    the site config -- so this is also where a later move to a real mailbox
    (info@symbiotope.com, say) would be picked up with no code change. Returning
    None would let Frappe fall back on the same account anyway; the reason for
    passing it explicitly is the display name, which would otherwise be the
    account's own ("Frappe").
    """
    # Imported here rather than at module level: this module is loaded as the
    # User controller, which Frappe imports on almost every request, and the
    # email package has no business being pulled in that early.
    from frappe.email.doctype.email_account.email_account import EmailAccount

    account = EmailAccount.find_default_outgoing()
    address = (account and account.email_id) or ""
    if not address:
        return None
    return email.utils.formataddr((_site_name() or account.name, address))


def _site_name():
    """The site's name, looked up exactly as Frappe does for the subject line."""
    return frappe.db.get_default("site_name") or frappe.get_conf().get("site_name")


def _inviter():
    """The person whose action triggered the mail, as `Name <address>`.

    None for Guest (a password reset asked for on the login page names nobody)
    and for Administrator, which is not a mailbox. It is a Reply-To, not a
    sender: it says where an answer should go, not who the mail is from.
    """
    user = frappe.session.user
    if not user or user in STANDARD_USERS:
        return None
    return get_formatted_email(user) or user


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
        kwargs["sender"] = _site_sender()
        if not kwargs.get("reply_to") and (inviter := _inviter()):
            kwargs["reply_to"] = inviter
        return original(*args, **kwargs)

    frappe.sendmail = sendmail
    try:
        yield
    finally:
        frappe.sendmail = original
