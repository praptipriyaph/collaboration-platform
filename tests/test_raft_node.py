import sys
import os
import pytest
import time
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_server.raft_node import RaftNode, ELECTION_TIMEOUT_MAX, RPC_TIMEOUT
from app_server.document_manager import DocumentManager
from app_server.auth import AuthManager
import service_pb2

# We must mock gRPC before RaftNode is imported,
# but it's complex. We'll mock the stub creation instead.
# Mock the gRPC channel and stub creation
mock_channel = MagicMock()
mock_stub = MagicMock()


@pytest.fixture
def raft_dependencies():
    """Provides real instances of dependencies that don't need mocking."""
    doc_manager = DocumentManager()
    auth_manager = AuthManager()
    return doc_manager, auth_manager


@pytest.fixture
def raft_node(mocker, raft_dependencies):  # 'mocker' is a fixture provided by pytest-mock
    """
    Provides a RaftNode instance with mocked-out network and file I/O.
    """
    # 1. Use 'mocker' to patch all external dependencies
    mocker.patch('app_server.raft_node.grpc.insecure_channel', return_value=mock_channel)
    mocker.patch('app_server.raft_node.service_pb2_grpc.RaftServiceStub', return_value=mock_stub)
    mocker.patch.object(RaftNode, '_load_state', MagicMock())
    mocker.patch.object(RaftNode, '_save_state', MagicMock())
    mocker.patch('os.path.exists', return_value=False)

    # 2. Get the real dependencies
    doc_manager, auth_manager = raft_dependencies

    # 3. Create and return the node in a clean, mocked state
    node = RaftNode(
        node_id="localhost:50051",
        peer_ids=["localhost:50052", "localhost:50053"],
        document_manager=doc_manager,
        auth_manager=auth_manager
    )
    return node


def test_initial_state(raft_node):
    assert raft_node.state == "follower"
    assert raft_node.current_term == 0
    assert raft_node.voted_for is None
    assert raft_node.last_snapshot_index == -1


@patch('time.time')
def test_follower_becomes_candidate(mock_time, raft_node):
    # 1. Initial state, time is 0.0
    mock_time.return_value = 0.0
    raft_node.last_heartbeat = 0.0

    # 2. Run follower logic, should not time out
    raft_node._run_follower()
    assert raft_node.state == "follower"

    # 3. Advance time past the election timeout
    mock_time.return_value = ELECTION_TIMEOUT_MAX + 1.0

    # 4. Run follower logic again, should time out and become candidate
    raft_node._run_follower()
    assert raft_node.state == "candidate"


def test_request_vote_logic_grants_vote(raft_node):
    # Node is in Term 0, has not voted
    assert raft_node.current_term == 0
    assert raft_node.voted_for is None

    # 1. Receive a request for a higher term (Term 1)
    args = service_pb2.RequestVoteArgs(
        term=1,
        candidate_id="localhost:50052",
        last_log_index=0,
        last_log_term=0
    )
    reply = raft_node.RequestVote(args, None)

    # 2. Should grant the vote and update its own state
    assert reply.vote_granted is True
    assert reply.term == 1
    assert raft_node.current_term == 1  # Node updates its term
    assert raft_node.voted_for == "localhost:50052"


def test_request_vote_logic_denies_vote_if_already_voted(raft_node):
    # 1. Grant a vote to the first candidate in Term 1
    args1 = service_pb2.RequestVoteArgs(
        term=1, candidate_id="localhost:50052", last_log_index=0, last_log_term=0
    )
    raft_node.RequestVote(args1, None)

    # 2. A second candidate requests a vote in the *same term*
    args2 = service_pb2.RequestVoteArgs(
        term=1, candidate_id="localhost:50053", last_log_index=0, last_log_term=0
    )
    reply = raft_node.RequestVote(args2, None)

    # 3. Should deny the vote
    assert reply.vote_granted is False
    assert reply.term == 1
    assert raft_node.voted_for == "localhost:50052"  # Vote is still for the first guy


def test_request_vote_logic_denies_vote_if_older_term(raft_node):
    # 1. Node is in Term 1
    raft_node.current_term = 1

    # 2. Receive a request from a candidate in an older term (Term 0)
    args = service_pb2.RequestVoteArgs(
        term=0, candidate_id="localhost:50052", last_log_index=0, last_log_term=0
    )
    reply = raft_node.RequestVote(args, None)

    # 3. Should deny the vote and report its own, higher term
    assert reply.vote_granted is False
    assert reply.term == 1  # It tells the candidate to update


def test_request_vote_logic_log_safety_check(raft_node):
    # 1. Node has a log entry from Term 1 at index 0
    raft_node.log = [service_pb2.LogEntry(term=1, command="test")]
    raft_node.current_term = 1
    # Manually set internal indices (since we skip persistence)
    raft_node.last_snapshot_index = -1

    # 2. Candidate A has an older log (e.g., Term 0 at index 0)
    args_stale = service_pb2.RequestVoteArgs(
        term=2, candidate_id="stale_candidate", last_log_index=0, last_log_term=0
    )
    # 3. Candidate B has a more up-to-date log (Term 1 at index 0)
    args_fresh = service_pb2.RequestVoteArgs(
        term=2, candidate_id="fresh_candidate", last_log_index=0, last_log_term=1
    )

    # 4. Granting vote to fresh candidate first
    reply_fresh = raft_node.RequestVote(args_fresh, None)
    assert reply_fresh.vote_granted is True
    assert raft_node.current_term == 2
    assert raft_node.voted_for == "fresh_candidate"

    # 5. Reset and test stale candidate
    raft_node.current_term = 1
    raft_node.voted_for = None
    reply_stale = raft_node.RequestVote(args_stale, None)
    assert reply_stale.vote_granted is False
    assert raft_node.current_term == 2  # Term gets updated, but vote denied
    assert raft_node.voted_for is None