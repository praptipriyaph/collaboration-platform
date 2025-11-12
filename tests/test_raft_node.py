import sys
import os
import pytest
import time
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_server.raft_node import RaftNode, ELECTION_TIMEOUT_MAX, RPC_TIMEOUT
from app_server.document_manager import DocumentManager
from app_server.auth import AuthManager
import service_pb2

mock_channel=MagicMock()
mock_stub=MagicMock()

@pytest.fixture
def raft_dependencies():
    doc_manager=DocumentManager()
    auth_manager=AuthManager()
    return doc_manager, auth_manager


@pytest.fixture
def raft_node(mocker, raft_dependencies):
    mocker.patch('app_server.raft_node.grpc.insecure_channel', return_value=mock_channel)
    mocker.patch('app_server.raft_node.service_pb2_grpc.RaftServiceStub', return_value=mock_stub)
    mocker.patch.object(RaftNode, '_load_state', MagicMock())
    mocker.patch.object(RaftNode, '_save_state', MagicMock())
    mocker.patch('os.path.exists', return_value=False)


    doc_manager, auth_manager=raft_dependencies

    node=RaftNode(
        node_id="localhost:50051",
        peer_ids=["localhost:50052", "localhost:50053"],
        document_manager=doc_manager,
        auth_manager=auth_manager
    )
    return node


def test_initial_state(raft_node):
    assert raft_node.state=="follower"
    assert raft_node.current_term==0
    assert raft_node.voted_for is None
    assert raft_node.last_snapshot_index==-1


@patch('time.time')
def test_follower_becomes_candidate(mock_time, raft_node):
    mock_time.return_value=0.0
    raft_node.last_heartbeat=0.0
    raft_node._run_follower()
    assert raft_node.state=="follower"

    mock_time.return_value=ELECTION_TIMEOUT_MAX+1.0
    raft_node._run_follower()
    assert raft_node.state=="candidate"


def test_request_vote_logic_grants_vote(raft_node):
    assert raft_node.current_term==0
    assert raft_node.voted_for is None

    # Receive a request for a higher term
    args=service_pb2.RequestVoteArgs(
        term=1,
        candidate_id="localhost:50052",
        last_log_index=0,
        last_log_term=0
    )
    
    reply=raft_node.RequestVote(args, None)
    assert reply.vote_granted is True
    assert reply.term==1
    assert raft_node.current_term==1
    assert raft_node.voted_for=="localhost:50052"


def test_request_vote_logic_denies_vote_if_already_voted(raft_node):
    args1=service_pb2.RequestVoteArgs(
        term=1, candidate_id="localhost:50052", last_log_index=0, last_log_term=0
    )
    raft_node.RequestVote(args1,None)

    # A second candidate requests a vote in the *same term*
    args2=service_pb2.RequestVoteArgs(
        term=1, candidate_id="localhost:50053", last_log_index=0, last_log_term=0
    )

    reply=raft_node.RequestVote(args2,None)
    assert reply.vote_granted is False
    assert reply.term==1
    assert raft_node.voted_for=="localhost:50052"


def test_request_vote_logic_denies_vote_if_older_term(raft_node):
    raft_node.current_term=1

    # Receive a request from a candidate in an older term
    args=service_pb2.RequestVoteArgs(
        term=0,candidate_id="localhost:50052",last_log_index=0,last_log_term=0
    )

    reply=raft_node.RequestVote(args, None)
    assert reply.vote_granted is False
    assert reply.term==1  


def test_request_vote_logic_log_safety_check(raft_node):
    raft_node.log=[service_pb2.LogEntry(term=1,command="test")]
    raft_node.current_term=1
    raft_node.last_snapshot_index=-1

    # Candidate A has an older log
    args_stale=service_pb2.RequestVoteArgs(
        term=2,candidate_id="stale_candidate",last_log_index=0,last_log_term=0
    )
    # Candidate B has a more up-to-date log
    args_fresh=service_pb2.RequestVoteArgs(
        term=2,candidate_id="fresh_candidate",last_log_index=0,last_log_term=1
    )

    # Granting vote to fresh candidate first
    reply_fresh=raft_node.RequestVote(args_fresh,None)
    assert reply_fresh.vote_granted is True
    assert raft_node.current_term==2
    assert raft_node.voted_for=="fresh_candidate"

    raft_node.current_term=1
    raft_node.voted_for=None
    reply_stale = raft_node.RequestVote(args_stale,None)
    assert reply_stale.vote_granted is False
    assert raft_node.current_term==2
    assert raft_node.voted_for is None