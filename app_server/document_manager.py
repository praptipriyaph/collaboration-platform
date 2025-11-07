from datetime import datetime
import uuid


class DocumentManager:
    def __init__(self):
        self.documents = {}
        self.active_users = set()
        # NEW: Store locks (doc_id -> username)
        self.locks = {}

    def create_document(self, doc_id, username, content):
        # doc_id is now passed in from the leader
        self.documents[doc_id] = {
            "id": doc_id,
            "content": content,
            "author": username,
            "created": datetime.now().isoformat(),
            "modified": datetime.now().isoformat(),
            "version": 1
        }
        return doc_id

    def get_document(self, doc_id):
        doc = self.documents.get(doc_id)
        if doc:
            # Add lock info to the document data for clients to see
            doc["locked_by"] = self.locks.get(doc_id)
        return doc

    def get_all_documents(self):
        # Add lock info to all documents
        docs = []
        for doc_id, doc in self.documents.items():
            doc_copy = doc.copy()
            doc_copy["locked_by"] = self.locks.get(doc_id)
            docs.append(doc_copy)
        return docs

    def update_document(self, doc_id, content, username):
        if doc_id not in self.documents:
            print(f"⚠️ DEBUG: Update failed. Document {doc_id} not found.")
            return False

        # Conflict Resolution Check
        if doc_id in self.locks and self.locks[doc_id] != username:
            print(
                f"⚠️ DEBUG: Update failed. Doc is locked by '{self.locks[doc_id]}', but '{username}' tried to update it.")
            return False

        self.documents[doc_id]["content"] = content
        self.documents[doc_id]["modified"] = datetime.now().isoformat()
        self.documents[doc_id]["version"] += 1
        print(f"✅ DEBUG: Document {doc_id} successfully updated by {username}.")
        return True

    def delete_document(self, doc_id):
        if doc_id in self.documents:
            del self.documents[doc_id]
            # Also remove any lock
            if doc_id in self.locks:
                del self.locks[doc_id]
            return True
        return False

    # --- NEW: Locking Methods ---
    def acquire_lock(self, doc_id, username):
        if doc_id not in self.documents:
            print(f"⚠️ DEBUG: Lock failed. Document {doc_id} not found.")
            return False
        if doc_id not in self.locks or self.locks[doc_id] == username:
            self.locks[doc_id] = username
            print(f"✅ DEBUG: {username} acquired lock on {doc_id}.")
            return True
        print(f"⚠️ DEBUG: Lock failed. Already locked by {self.locks[doc_id]}.")
        return False

    def release_lock(self, doc_id, username):
        if doc_id in self.locks and self.locks[doc_id] == username:
            del self.locks[doc_id]
            print(f"✅ DEBUG: {username} released lock on {doc_id}.")
            return True
        print(f"⚠️ DEBUG: Unlock failed. Doc is locked by {self.locks.get(doc_id, 'NO ONE')}, requester was {username}.")
        return False

    def add_active_user(self, username):
        self.active_users.add(username)

    def remove_active_user(self, username):
        self.active_users.discard(username)

    def get_active_users(self):
        return list(self.active_users)