from datetime import datetime
import uuid

class DocumentManager:
    def __init__(self):
        self.documents = {}
        self.active_users = set()
    
    def create_document(self, username, content):
        doc_id = str(uuid.uuid4())
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
        return self.documents.get(doc_id)
    
    def get_all_documents(self):
        return list(self.documents.values())
    
    def update_document(self, doc_id, content, username):
        if doc_id in self.documents:
            self.documents[doc_id]["content"] = content
            self.documents[doc_id]["modified"] = datetime.now().isoformat()
            self.documents[doc_id]["version"] += 1
            return True
        return False
    
    def delete_document(self, doc_id):
        if doc_id in self.documents:
            del self.documents[doc_id]
            return True
        return False
    
    def add_active_user(self, username):
        self.active_users.add(username)
    
    def remove_active_user(self, username):
        self.active_users.discard(username)
    
    def get_active_users(self):
        return list(self.active_users)