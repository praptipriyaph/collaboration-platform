from datetime import datetime
import uuid

class DocumentManager:
    def __init__(self):
        self.documents = {}
        self.active_users = set()
        self.locks = {}

    def create_document(self, doc_id, username, content):
        self.documents[doc_id] = {
            "id": doc_id,
            "author": username,
            "created": datetime.now().isoformat(),
            "version": 1,
            "content_history": [{
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "author": username
            }]
        }
        return doc_id

    def get_document(self, doc_id):
        doc = self.documents.get(doc_id)
        if doc:
            # Create a copy to avoid modifying the original
            doc_copy = doc.copy()
            doc_copy["locked_by"] = self.locks.get(doc_id)
            # Inject the *latest* content into the top level for compatibility
            if doc_copy.get("content_history"):
                doc_copy["content"] = doc_copy["content_history"][-1]["content"]
            return doc_copy
        return None

    def get_all_documents(self):
        docs = []
        for doc_id, doc in self.documents.items():
            doc_copy = doc.copy()
            doc_copy["locked_by"] = self.locks.get(doc_id)

            latest_content = ""
            if doc_copy.get("content_history"):
                latest_content = doc_copy["content_history"][-1]["content"]

            doc_copy["display_data"] = f"{latest_content} | Author: {doc['author']} | Version: {doc['version']}"
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

        new_version_data = {
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "author": username
        }
        self.documents[doc_id]["content_history"].append(new_version_data)
        self.documents[doc_id]["version"] += 1

        print(f"✅ DEBUG: Document {doc_id} successfully updated to v{self.documents[doc_id]['version']}.")
        return True

    def delete_document(self, doc_id):
        if doc_id in self.documents:
            del self.documents[doc_id]
            if doc_id in self.locks:
                del self.locks[doc_id]
            return True
        return False

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

    def get_document_history(self, doc_id):

        doc = self.documents.get(doc_id)
        return doc.get("content_history", []) if doc else []