from . import google

class EmailProvider:
    """Provider boundary: scheduler and approvals depend on this contract, not Gmail URLs."""
    def profile(self, connection): raise NotImplementedError
    def list_messages(self, connection, query="", cursor=None, limit=25): raise NotImplementedError
    def get_thread(self, connection, thread_id): raise NotImplementedError
    def create_draft(self, connection, recipients, subject, body, thread_id=None): raise NotImplementedError
    def update_draft(self, connection, draft_id, recipients, subject, body, thread_id=None): raise NotImplementedError
    def send_message(self, connection, scheduled_email): raise NotImplementedError
    def archive(self, connection, message_id): raise NotImplementedError
    def modify_labels(self, connection, message_id, add=(), remove=()): raise NotImplementedError
    def revoke(self, connection): raise NotImplementedError

class GmailProvider(EmailProvider):
    def profile(self, connection): return google.profile(connection)
    def list_messages(self, connection, query="", cursor=None, limit=25): return google.list_messages(connection,query,cursor,limit)
    def get_thread(self, connection, thread_id): return google.get_thread(connection,thread_id)
    def create_draft(self, connection, recipients, subject, body, thread_id=None): return google.create_draft(connection,recipients,subject,body,thread_id)
    def update_draft(self, connection, draft_id, recipients, subject, body, thread_id=None): return google.update_draft(connection,draft_id,recipients,subject,body,thread_id)
    def send_message(self, connection, scheduled_email): return google.send(connection,scheduled_email)
    def archive(self, connection, message_id): return google.archive_message(connection,message_id)
    def modify_labels(self, connection, message_id, add=(), remove=()): return google.modify_message(connection,message_id,add,remove)
    def revoke(self, connection): return google.revoke(connection)

def provider_for(connection):
    if connection.provider == "gmail": return GmailProvider()
    raise ValueError("Unsupported mail provider.")
