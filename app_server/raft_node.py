import grpc
import threading
import time
import random
import json
import os
import service_pb2
import service_pb2_grpc
from app_server.document_manager import DocumentManager
from app_server.auth import AuthManager
from concurrent.futures import ThreadPoolExecutor

# --- Constants ---
ELECTION_TIMEOUT_MIN=3.0
ELECTION_TIMEOUT_MAX=5.0
HEARTBEAT_INTERVAL=1.0
RPC_TIMEOUT=0.5
SNAPSHOT_THRESHOLD=50


class RaftNode(service_pb2_grpc.RaftServiceServicer):
    """
    Implements Raft with log compaction via snapshotting.
    """

    def __init__(self, node_id, peer_ids, document_manager, auth_manager):
        self.node_id=node_id
        self.peer_ids=peer_ids
        self.document_manager=document_manager
        self.auth_manager=auth_manager

        self.initial_peer_ids=peer_ids 
        self.state_file=f"raft_state_{node_id.replace(':', '_')}.json"
        self.snapshot_file=f"raft_snapshot_{node_id.replace(':', '_')}.json"
        self.current_term=0
        self.voted_for=None
        self.log=[]
        self.last_snapshot_index=-1
        self.last_snapshot_term=0
        self.commit_index=-1
        self.last_applied=-1
        self.state="follower"
        self.leader_id=None

        self.next_index={peer: 0 for peer in self.peer_ids}
        self.match_index={peer: -1 for peer in self.peer_ids}
        self.peer_stubs={}
        for peer in self.peer_ids:
            channel=grpc.insecure_channel(peer)
            self.peer_stubs[peer]=service_pb2_grpc.RaftServiceStub(channel)
        self.rpc_executor=ThreadPoolExecutor(max_workers=len(self.peer_ids)*2+1)
        self.lock=threading.Lock()

        self._load_state()
        self._load_snapshot_from_disk()
        self._apply_log_entries()
        self.election_timeout=self._get_new_election_timeout()
        self.last_heartbeat=time.time()
        self.on_apply_callbacks=[]

        print(f"[{self.node_id}] Initialized as Follower in Term {self.current_term}")

    # Indexing Helper Methods (Crucial for Snapshotting)

    def _get_list_index(self, absolute_index):
        return absolute_index-(self.last_snapshot_index+1)

    def _get_absolute_index(self, list_index):
        return self.last_snapshot_index + 1 + list_index

    def _get_log_term(self, absolute_index):
        if absolute_index==self.last_snapshot_index:
            return self.last_snapshot_term
        if absolute_index<self.last_snapshot_index:
            return -1

        list_index=self._get_list_index(absolute_index)
        if list_index<0 or list_index>=len(self.log):
            return -1

        return self.log[list_index].term

    def _get_last_log_index_term(self):
        if not self.log:
            return self.last_snapshot_index, self.last_snapshot_term

        absolute_index=self._get_absolute_index(len(self.log)-1)
        term=self.log[-1].term
        return absolute_index, term

    # Persistence & Snapshotting Methods

    def _save_state(self):
        log_data=[{'term': e.term, 'command': e.command} for e in self.log]
        state={
            'current_term': self.current_term,
            'voted_for': self.voted_for,
            'log': log_data,
            'last_snapshot_index': self.last_snapshot_index,
            'last_snapshot_term': self.last_snapshot_term,
            'peer_ids': self.peer_ids
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[{self.node_id}] Failed to save state:{e}")

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file,'r') as f:
                    state=json.load(f)
                    self.current_term=state.get('current_term', 0)
                    self.voted_for=state.get('voted_for')
                    self.last_snapshot_index=state.get('last_snapshot_index', -1)
                    self.last_snapshot_term=state.get('last_snapshot_term', 0)
                    self.peer_ids=state.get('peer_ids', self.initial_peer_ids)

                    self.commit_index=self.last_snapshot_index
                    self.last_applied=self.last_snapshot_index

                    self.log=[]
                    for entry_data in state.get('log', []):
                        self.log.append(service_pb2.LogEntry(
                            term=entry_data['term'],
                            command=entry_data['command']
                        ))
                print(
                    f"[{self.node_id}] Loaded persisted state: Term {self.current_term}, Log size {len(self.log)}, Snapshot Index {self.last_snapshot_index}")
            except Exception as e:
                print(f"[{self.node_id}] Failed to load state: {e}")
        else:
            self.peer_ids=self.initial_peer_ids
            print(f"[{self.node_id}] No persistent state found. Starting fresh.")

    def _create_snapshot(self):
        # This function MUST be called while holding self.lock
        doc_state=self.document_manager.get_state()
        auth_state=self.auth_manager.get_state()
        snapshot_index=self.last_applied
        snapshot_term=self._get_log_term(snapshot_index)

        print(f"[{self.node_id}] Creating snapshot up to index {snapshot_index} (Term {snapshot_term})")
        snapshot_data={
            'doc_manager': doc_state,
            'auth_manager': auth_state
        }

        try:
            with open(self.snapshot_file, 'w') as f:
                json.dump(snapshot_data, f)
        except Exception as e:
            print(f"[{self.node_id}] Failed to save snapshot: {e}")
            return 

        self.last_snapshot_index=snapshot_index
        self.last_snapshot_term=snapshot_term
        list_index_to_compact=self._get_list_index(snapshot_index)
        self.log = self.log[list_index_to_compact + 1:]
        self._save_state()

    def _load_snapshot_from_disk(self):
        if os.path.exists(self.snapshot_file):
            try:
                with open(self.snapshot_file, 'r') as f:
                    snapshot_data=json.load(f)
                    self.document_manager.load_state(snapshot_data['doc_manager'])
                    self.auth_manager.load_state(snapshot_data['auth_manager'])
                print(f"[{self.node_id}] Loaded state from snapshot file.")
            except Exception as e:
                print(f"[{self.node_id}] Failed to load snapshot: {e}")

    # Raft Core Logic (Modified for Snapshotting)

    def _get_new_election_timeout(self):
        return random.uniform(ELECTION_TIMEOUT_MIN,ELECTION_TIMEOUT_MAX)

    def run(self):
        while True:
            with self.lock:
                current_state=self.state
            if current_state=="follower":
                self._run_follower()
            elif current_state=="candidate":
                self._run_candidate()
            elif current_state=="leader":
                self._run_leader()

            time.sleep(0.1)

    def _run_follower(self):
        if time.time()-self.last_heartbeat > self.election_timeout:
            print(f"[{self.node_id}] Follower timeout, becoming candidate.")
            with self.lock:
                self.state="candidate"

    def _run_candidate(self):
        with self.lock:
            self.current_term+=1
            self.voted_for=self.node_id
            self._save_state()
            votes_received=1
            print(f"[{self.node_id}] Starting election for Term {self.current_term}")

            election_term=self.current_term
            self.election_timeout=self._get_new_election_timeout()
            self.last_heartbeat=time.time()

            last_log_index,last_log_term=self._get_last_log_index_term()

        args=service_pb2.RequestVoteArgs(
            term=election_term,
            candidate_id=self.node_id,
            last_log_index=last_log_index,
            last_log_term=last_log_term
        )

        futures={self.rpc_executor.submit(self._request_vote_from_peer, peer, args): peer for peer in self.peer_ids}

        with self.lock:
            if self.state!="candidate" or self.current_term!=election_term:
                return

            for future in futures:
                reply=future.result()
                if reply:
                    if reply.vote_granted:
                        votes_received+=1
                    if reply.term>self.current_term:
                        self._step_down(reply.term)
                        return

            if votes_received>(len(self.peer_ids) + 1)/2:
                print(f"[{self.node_id}] Won election for Term {election_term} with {votes_received} votes.")
                self.state="leader"
                self.leader_id=self.node_id

                last_log_index, _=self._get_last_log_index_term()
                self.next_index={peer: last_log_index + 1 for peer in self.peer_ids}
                self.match_index={peer: -1 for peer in self.peer_ids}
                self._run_leader()
            else:
                print(f"[{self.node_id}] Lost election for Term {election_term}.")
                self.state="follower"

    def _request_vote_from_peer(self, peer, args):
        try:
            return self.peer_stubs[peer].RequestVote(args, timeout=RPC_TIMEOUT)
        except grpc.RpcError:
            return None

    def _run_leader(self):
        if time.time()-self.last_heartbeat < HEARTBEAT_INTERVAL:
            return

        with self.lock:
            self.last_heartbeat=time.time()
            if self.state!="leader":
                return

            futures={self.rpc_executor.submit(self._replicate_log_to_peer, peer): peer for peer in self.peer_ids}

        for future in futures:
            reply_term=future.result()
            if reply_term and reply_term>self.current_term:
                with self.lock:
                    self._step_down(reply_term)
                    return

        with self.lock:
            if self.state=="leader":
                self._update_commit_index()
                self._apply_log_entries()

    def _replicate_log_to_peer(self, peer):
        with self.lock:
            if self.state!="leader":
                return None

            peer_next_index=self.next_index[peer]
            if peer_next_index<=self.last_snapshot_index:
                print(
                    f"[{self.node_id}] Follower {peer} too far behind (next_index={peer_next_index}, snapshot_index={self.last_snapshot_index}). Sending snapshot.")
                return self.rpc_executor.submit(self._send_snapshot_to_peer, peer).result()

            prev_log_index=peer_next_index - 1
            prev_log_term=self._get_log_term(prev_log_index)

            list_start_index=self._get_list_index(peer_next_index)
            entries_to_send=self.log[list_start_index:]

            args=service_pb2.AppendEntriesArgs(
                term=self.current_term,
                leader_id=self.node_id,
                prev_log_index=prev_log_index,
                prev_log_term=prev_log_term,
                entries=entries_to_send,
                leader_commit=self.commit_index
            )

        try:
            reply=self.peer_stubs[peer].AppendEntries(args, timeout=RPC_TIMEOUT)

            with self.lock:
                if self.state!="leader" or reply.term>self.current_term:
                    return reply.term

                if reply.success:
                    new_match_index=prev_log_index+len(entries_to_send)
                    self.match_index[peer]=new_match_index
                    self.next_index[peer]=new_match_index+1
                else:
                    self.next_index[peer]=max(0, self.next_index[peer]-1)
            return reply.term
        except grpc.RpcError:
            return None

    def _send_snapshot_to_peer(self, peer):
        # This function MUST be called while holding self.lock

        try:
            with open(self.snapshot_file, 'rb') as f:
                snapshot_bytes=f.read()
        except Exception as e:
            print(f"[{self.node_id}] Failed to read snapshot for {peer}: {e}")
            return self.current_term

        args=service_pb2.InstallSnapshotArgs(
            term=self.current_term,
            leader_id=self.node_id,
            last_snapshot_index=self.last_snapshot_index,
            last_snapshot_term=self.last_snapshot_term,
            snapshot_data=snapshot_bytes
        )

        try:
            reply=self.peer_stubs[peer].InstallSnapshot(args,timeout=5.0)

            if reply.term>self.current_term:
                return reply.term

            if reply.success:
                print(f"[{self.node_id}] Successfully installed snapshot on {peer}")
                self.match_index[peer]=self.last_snapshot_index
                self.next_index[peer]=self.last_snapshot_index+1

            return reply.term
        except grpc.RpcError as e:
            print(f"[{self.node_id}] InstallSnapshot RPC to {peer} failed: {e}")
            return self.current_term

    def _update_commit_index(self):
        if self.state!="leader":
            return

        last_log_index, _=self._get_last_log_index_term()

        for N in range(last_log_index, self.commit_index, -1):
            if self._get_log_term(N)==self.current_term:
                majority_count=1
                for peer in self.peer_ids:
                    if self.match_index[peer]>=N:
                        majority_count+=1

                if majority_count>(len(self.peer_ids)+1)/2:
                    self.commit_index=N
                    break

    def _apply_log_entries(self):

        while self.commit_index>self.last_applied:
            self.last_applied+=1
            list_index=self._get_list_index(self.last_applied)
            entry=self.log[list_index]

            print(f"[{self.node_id}] Applying to state machine (Index {self.last_applied}): {entry.command}")

            try:
                parts=entry.command.split('|')
                cmd_type=parts[0]
                if cmd_type=="CREATE":
                    doc_id, username, content=parts[1], parts[2], parts[3]
                    self.document_manager.create_document(doc_id, username, content)
                    self._notify_listeners("CREATE", doc_id, username, content)
                elif cmd_type=="UPDATE":
                    doc_id, content, username=parts[1], parts[2], parts[3]
                    success=self.document_manager.update_document(doc_id, content, username)
                    if success:
                        self._notify_listeners("UPDATE", doc_id, username, content)
                elif cmd_type=="LOCK":
                    doc_id, username=parts[1], parts[2]
                    if self.document_manager.acquire_lock(doc_id, username):
                        self._notify_listeners("LOCK", doc_id, username, "")
                elif cmd_type=="UNLOCK":
                    doc_id, username=parts[1], parts[2]
                    if self.document_manager.release_lock(doc_id, username):
                        self._notify_listeners("UNLOCK", doc_id, username, "")

                elif cmd_type=="CREATE_SESSION":
                    token, username=parts[1], parts[2]
                    self.auth_manager.apply_create_session(token, username)
                    self.document_manager.add_active_user(username)
                    print(f"[{self.node_id}] Applied CREATE_SESSION for {username}")


                elif cmd_type=="DELETE_SESSION":
                    token=parts[1]
                    valid, username=self.auth_manager.validate_token(token)
                    if valid:
                        if self.auth_manager.apply_delete_session(token):
                            self.document_manager.remove_active_user(username)
                            print(f"[{self.node_id}] Applied DELETE_SESSION for {username}")

                elif cmd_type=="ADD_NODE":
                    new_node_id=parts[1]
                    self._add_peer(new_node_id)

            except Exception as e:
                print(f"[{self.node_id}] Error applying log: {e}")

        if self.last_applied-self.last_snapshot_index>SNAPSHOT_THRESHOLD:
            self._create_snapshot()

    def _notify_listeners(self, op_type, doc_id, user, content):
        for callback in self.on_apply_callbacks:
            try:
                callback(op_type, doc_id, user, content)
            except Exception as e:
                print(f"Error in on_apply_callback: {e}")

    def _add_peer(self, new_node_id):
        if new_node_id in self.peer_ids or new_node_id==self.node_id:
            print(f"[{self.node_id}] Node {new_node_id} is already in peer list.")
            return

        print(f"[{self.node_id}] Applying config change: ADD_NODE {new_node_id}")
        self.peer_ids.append(new_node_id)

        if new_node_id not in self.peer_stubs:
            channel=grpc.insecure_channel(new_node_id)
            self.peer_stubs[new_node_id]=service_pb2_grpc.RaftServiceStub(channel)

        if self.state=="leader":
            last_log_index, _=self._get_last_log_index_term()
            self.next_index[new_node_id]=last_log_index+1
            self.match_index[new_node_id]=-1
            print(f"[{self.node_id}] Leader: Initialized tracking for new peer {new_node_id}")

        self._save_state()

    def _step_down(self, new_term):
        if new_term>self.current_term:
            print(f"[{self.node_id}] Stepping down. Old Term: {self.current_term}, New Term: {new_term}")
            self.current_term=new_term
            self.state="follower"
            self.voted_for=None
            self.leader_id=None
            self._save_state()

        self.last_heartbeat=time.time()

    # gRPC Servicer Methods (Called by peers)

    def RequestVote(self, request, context):
        with self.lock:
            if request.term>self.current_term:
                self._step_down(request.term)

            vote_granted=False
            if request.term==self.current_term and \
                    (self.voted_for is None or self.voted_for==request.candidate_id):

                last_log_index, last_log_term=self._get_last_log_index_term()
                if request.last_log_term > last_log_term or \
                        (request.last_log_term==last_log_term and request.last_log_index>=last_log_index):
                    vote_granted=True
                    self.voted_for=request.candidate_id
                    self._save_state()
                    self.last_heartbeat=time.time()

            return service_pb2.RequestVoteReply(
                term=self.current_term,
                vote_granted=vote_granted
            )

    def AppendEntries(self, request, context):
        with self.lock:
            if request.term>self.current_term:
                self._step_down(request.term)

            success=False
            match_index=-1

            if request.term == self.current_term:
                self.state="follower"
                self.leader_id=request.leader_id
                self.last_heartbeat=time.time()
                last_log_index, _=self._get_last_log_index_term()
                if request.prev_log_index>last_log_index:
                    return service_pb2.AppendEntriesReply(term=self.current_term,success=False,match_index=last_log_index)

                if self._get_log_term(request.prev_log_index) != request.prev_log_term:
                    return service_pb2.AppendEntriesReply(term=self.current_term, success=False,match_index=last_log_index)

                success=True


                list_truncate_index=self._get_list_index(request.prev_log_index+1)
                self.log=self.log[:list_truncate_index]
                self.log.extend(request.entries)
                self._save_state()

                match_index, _=self._get_last_log_index_term()

                if request.leader_commit>self.commit_index:
                    self.commit_index=min(request.leader_commit,match_index)
                    self._apply_log_entries()

            return service_pb2.AppendEntriesReply(
                term=self.current_term,
                success=success,
                match_index=match_index
            )

    def InstallSnapshot(self,request,context):
        with self.lock:
            if request.term>self.current_term:
                self._step_down(request.term)

            if request.term<self.current_term:
                return service_pb2.InstallSnapshotReply(term=self.current_term, success=False)

            print(f"[{self.node_id}] Received snapshot from leader {request.leader_id}")

            try:
                with open(self.snapshot_file, 'wb') as f:
                    f.write(request.snapshot_data)
            except Exception as e:
                print(f"[{self.node_id}] Failed to write snapshot to disk: {e}")
                return service_pb2.InstallSnapshotReply(term=self.current_term, success=False)

            self._load_snapshot_from_disk()

            self.last_snapshot_index=request.last_snapshot_index
            self.last_snapshot_term=request.last_snapshot_term

            self.last_applied=max(self.last_applied, self.last_snapshot_index)
            self.commit_index=max(self.commit_index, self.last_snapshot_index)

            new_log=[]
            for i, entry in enumerate(self.log):
                if self._get_absolute_index(i)>self.last_snapshot_index and \
                        self.log[i].term>self.last_snapshot_term:
                    new_log.append(entry)
            self.log=new_log

            self._save_state()

            return service_pb2.InstallSnapshotReply(term=self.current_term, success=True)

    # Client-Facing Method

    def submit_command(self,command_str):
        with self.lock:
            if self.state !="leader":
                return False,self.leader_id

            last_log_index, _=self._get_last_log_index_term()
            log_entry=service_pb2.LogEntry(
                term=self.current_term,
                command=command_str
            )
            self.log.append(log_entry)
            self._save_state()
            print(f"[{self.node_id}] Leader received command (Index {last_log_index+1}): {command_str}")

            return True, self.node_id