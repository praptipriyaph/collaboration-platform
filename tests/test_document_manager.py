import sys
import os
import pytest
from datetime import datetime
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app_server.document_manager import DocumentManager


@pytest.fixture
def doc_manager():
    return DocumentManager()

def test_create_document(doc_manager):
    doc_id=doc_manager.create_document("doc1", "user1", "Hello")

    assert doc_id=="doc1"
    assert "doc1" in doc_manager.documents
    doc = doc_manager.documents["doc1"]
    assert doc["author"]=="user1"
    assert doc["version"]==1
    assert len(doc["content_history"])==1
    assert doc["content_history"][0]["content"]=="Hello"


def test_get_document(doc_manager):
    doc_manager.create_document("doc1","user1","Hello")

    doc=doc_manager.get_document("doc1")
    assert doc is not None
    assert doc["id"]=="doc1"
    assert doc["content"]=="Hello"
    assert doc["locked_by"] is None

    doc_none=doc_manager.get_document("nonexistent")
    assert doc_none is None


def test_update_document_simple(doc_manager):
    doc_manager.create_document("doc1","user1","Hello")
    success = doc_manager.update_document("doc1","World","user1")

    assert success is True
    doc = doc_manager.get_document("doc1")
    assert doc["version"]==2
    assert doc["content"]=="World"
    assert len(doc["content_history"])==2
    assert doc["content_history"][1]["author"]=="user1"


def test_lock_and_update(doc_manager):
    doc_manager.create_document("doc1","user1","Hello")

    # user1 acquires lock
    lock_success=doc_manager.acquire_lock("doc1", "user1")
    assert lock_success is True
    assert doc_manager.locks["doc1"]=="user1"

    # user2 fails to update
    update_fail=doc_manager.update_document("doc1","New Content","user2")
    assert update_fail is False

    # user1 can update
    update_success=doc_manager.update_document("doc1","My Content","user1")
    assert update_success is True

    doc=doc_manager.get_document("doc1")
    assert doc["content"]=="My Content"
    assert doc["version"]==2


def test_lock_conflict(doc_manager):
    doc_manager.create_document("doc1","user1","Hello")
    doc_manager.acquire_lock("doc1","user1")
    lock_fail=doc_manager.acquire_lock("doc1","user2")
    assert lock_fail is False
    assert doc_manager.locks["doc1"]=="user1"


def test_release_lock(doc_manager):
    doc_manager.create_document("doc1","user1","Hello")
    doc_manager.acquire_lock("doc1","user1")
    assert "doc1" in doc_manager.locks


    release_fail=doc_manager.release_lock("doc1","user2")
    assert release_fail is False

    release_success=doc_manager.release_lock("doc1","user1")
    assert release_success is True
    assert "doc1" not in doc_manager.locks


def test_get_and_load_state(doc_manager):
    doc_manager.create_document("doc1","user1","Hello")
    doc_manager.acquire_lock("doc1","user1")
    doc_manager.add_active_user("user1")

    state=doc_manager.get_state()
    new_manager=DocumentManager()
    new_manager.load_state(state)

    assert len(new_manager.documents)==1
    assert "doc1" in new_manager.locks
    assert "user1" in new_manager.active_users

    doc=new_manager.get_document("doc1")
    assert doc["content"]=="Hello"