import grpc
import threading
import time
import random
import service_pb2
import service_pb2_grpc
from app_server.document_manager import DocumentManager
# We will use this to run all our outgoing RPCs
from concurrent.futures import ThreadPoolExecutor

# --- Constants ---
ELECTION_TIMEOUT_MIN = 3.0  # seconds
ELECTION_TIMEOUT_MAX = 5.0  # seconds
HEARTBEAT_INTERVAL = 1.0  # seconds
RPC_TIMEOUT = 0.5  # 500ms for RPCs


class RaftNode(service_pb2_grpc.RaftServiceServicer):
    """
    Implements the Raft consensus logic and the RaftService gRPC servicer.
    This version uses a stable, long-lived thread pool for outgoing RPCs
    to prevent resource starvation.
    """

    def __init__(self, node_id, peer_ids, document_manager):
        self.node_id = node_id
        self.peer_ids = peer_ids
        self.document_manager = document_manager

        # --- Persistent Raft State ---
        self.current_term = 0
        self.voted_for = None
        self.log = []

        # --- Volatile Raft State ---
        self.commit_index = -1
        self.last_applied = -1
        self.state = "follower"
        self.leader_id = None

        # --- Leader-Specific State ---
        self.next_index = {peer: 0 for peer in self.peer_ids}
        self.match_index = {peer: -1 for peer in self.peer_ids}

        # --- gRPC Stubs for Peers ---
        self.peer_stubs = {}
        for peer in self.peer_ids:
            channel = grpc.insecure_channel(peer)
            self.peer_stubs[peer] = service_pb2_grpc.RaftServiceStub(channel)

        # --- THIS IS THE FIX ---
        # Create one, long-lived thread pool for all outgoing RPCs.
        # The number of workers should be proportional to peers.
        self.rpc_executor = ThreadPoolExecutor(max_workers=len(self.peer_ids) * 2 + 1)
        # --- END FIX ---

        # --- Timers and Locks ---
        self.lock = threading.Lock()
        self.election_timeout = self._get_new_election_timeout()
        self.last_heartbeat = time.time()

        print(f"[{self.node_id}] Initialized as Follower in Term {self.current_term}")

        self.on_apply_callbacks = []

        print(f"[{self.node_id}] Initialized as Follower in Term {self.current_term}")

    def _get_new_election_timeout(self):
        return random.uniform(ELECTION_TIMEOUT_MIN, ELECTION_TIMEOUT_MAX)

    def _get_last_log_index_term(self):
        # This is a small helper, no need for lock
        if not self.log:
            return -1, 0
        last_index = len(self.log) - 1
        last_term = self.log[last_index].term
        return last_index, last_term

    # -------------------------------------------------
    # Main Raft Loop (Runs in a separate thread)
    # -------------------------------------------------

    def run(self):
        """Main loop that drives the node's state."""
        while True:
            with self.lock:
                current_state = self.state

            if current_state == "follower":
                self._run_follower()
            elif current_state == "candidate":
                self._run_candidate()
            elif current_state == "leader":
                self._run_leader()

            time.sleep(0.1)  # 100ms tick

    def _run_follower(self):
        if time.time() - self.last_heartbeat > self.election_timeout:
            print(f"[{self.node_id}] Follower timeout, becoming candidate.")
            with self.lock:
                self.state = "candidate"

    def _run_candidate(self):
        # --- 1. Setup Election ---
        with self.lock:
            self.current_term += 1
            self.voted_for = self.node_id
            votes_received = 1
            print(f"[{self.node_id}] Starting election for Term {self.current_term}")

            election_term = self.current_term
            self.election_timeout = self._get_new_election_timeout()
            self.last_heartbeat = time.time()

            last_log_index, last_log_term = self._get_last_log_index_term()

        args = service_pb2.RequestVoteArgs(
            term=election_term,
            candidate_id=self.node_id,
            last_log_index=last_log_index,
            last_log_term=last_log_term
        )

        # --- 2. Send RPCs in Parallel ---
        # Use the persistent, class-level executor
        futures = {self.rpc_executor.submit(self._request_vote_from_peer, peer, args): peer for peer in self.peer_ids}

        # --- 3. Tally Votes ---
        with self.lock:
            # Check if we're still a candidate for *this* term
            if self.state != "candidate" or self.current_term != election_term:
                return

            for future in futures:
                reply = future.result()  # Wait for the RPC to return
                if reply:
                    if reply.vote_granted:
                        votes_received += 1

                    if reply.term > self.current_term:
                        self._step_down(reply.term)
                        return  # Abort this election

            # --- 4. Decide Election Outcome ---
            if votes_received > (len(self.peer_ids) + 1) / 2:
                print(f"[{self.node_id}] Won election for Term {election_term} with {votes_received} votes.")
                self.state = "leader"
                self.leader_id = self.node_id

                last_log_index, _ = self._get_last_log_index_term()
                self.next_index = {peer: last_log_index + 1 for peer in self.peer_ids}
                self.match_index = {peer: -1 for peer in self.peer_ids}

                # Immediately send heartbeats to establish authority
                self._run_leader()
            else:
                print(f"[{self.node_id}] Lost election for Term {election_term}.")
                self.state = "follower"

    def _request_vote_from_peer(self, peer, args):
        """Sends RequestVote RPC and returns the reply."""
        try:
            reply = self.peer_stubs[peer].RequestVote(args, timeout=RPC_TIMEOUT)
            return reply
        except grpc.RpcError as e:
            # This is expected if a node is down or busy
            return None

    def _run_leader(self):
        """Sends heartbeats/AppendEntries to all followers."""
        if time.time() - self.last_heartbeat < HEARTBEAT_INTERVAL:
            return

        with self.lock:
            self.last_heartbeat = time.time()
            # Check if we are still the leader (might have stepped down)
            if self.state != "leader":
                return

            # Gather replies
            futures = {self.rpc_executor.submit(self._replicate_log_to_peer, peer): peer for peer in self.peer_ids}

        # Process replies *after* releasing the lock
        # (The _replicate_log_to_peer function is thread-safe)
        for future in futures:
            reply = future.result()
            if reply:
                with self.lock:
                    # Check for higher term
                    if reply.term > self.current_term:
                        self._step_down(reply.term)
                        return  # Stop being leader

        # After all RPCs are done, update commit index
        with self.lock:
            if self.state == "leader":
                self._update_commit_index()
                self._apply_log_entries()

    def _replicate_log_to_peer(self, peer):
        """Sends AppendEntries RPC and updates state based on reply."""
        with self.lock:
            if self.state != "leader":
                return None

            prev_log_index = self.next_index[peer] - 1
            prev_log_term = self.log[prev_log_index].term if prev_log_index >= 0 else 0
            entries_to_send = self.log[self.next_index[peer]:]

            args = service_pb2.AppendEntriesArgs(
                term=self.current_term,
                leader_id=self.node_id,
                prev_log_index=prev_log_index,
                prev_log_term=prev_log_term,
                entries=entries_to_send,
                leader_commit=self.commit_index
            )

        try:
            reply = self.peer_stubs[peer].AppendEntries(args, timeout=RPC_TIMEOUT)

            with self.lock:
                # Need to re-check state, might have stepped down
                if self.state != "leader" or reply.term > self.current_term:
                    return reply  # Will be handled by _run_leader loop

                if reply.success:
                    self.next_index[peer] = len(self.log)
                    self.match_index[peer] = len(self.log) - 1
                else:
                    self.next_index[peer] = max(0, self.next_index[peer] - 1)
            return reply

        except grpc.RpcError as e:
            return None

    def _update_commit_index(self):
        """MUST be called *while holding self.lock*."""
        if self.state != "leader":
            return

        for N in range(len(self.log) - 1, self.commit_index, -1):
            if self.log[N].term == self.current_term:
                majority_count = 1  # Self
                for peer in self.peer_ids:
                    if self.match_index[peer] >= N:
                        majority_count += 1

                if majority_count > (len(self.peer_ids) + 1) / 2:
                    self.commit_index = N
                    break

    def _apply_log_entries(self):
        """MUST be called *while holding self.lock*."""
        while self.commit_index > self.last_applied:
            self.last_applied += 1
            entry = self.log[self.last_applied]

            print(f"[{self.node_id}] Applying to state machine: {entry.command}")

            try:
                parts = entry.command.split('|')
                cmd_type = parts[0]

                if cmd_type == "CREATE":
                    doc_id, username, content = parts[1], parts[2], parts[3]
                    self.document_manager.create_document(doc_id, username, content)
                    self._notify_listeners("CREATE", doc_id, username, content)

                elif cmd_type == "UPDATE":
                    doc_id, content, username = parts[1], parts[2], parts[3]
                    # Raft just applies it. The DocumentManager decides if it's valid.
                    # In a real system, we might want to log failed applications too.
                    success = self.document_manager.update_document(doc_id, content, username)
                    if success:
                        self._notify_listeners("UPDATE", doc_id, username, content)

                # --- NEW COMMANDS ---
                elif cmd_type == "LOCK":
                    # Format: LOCK|doc_id|username
                    doc_id, username = parts[1], parts[2]
                    if self.document_manager.acquire_lock(doc_id, username):
                        self._notify_listeners("LOCK", doc_id, username, "")

                elif cmd_type == "UNLOCK":
                    # Format: UNLOCK|doc_id|username
                    doc_id, username = parts[1], parts[2]
                    if self.document_manager.release_lock(doc_id, username):
                        self._notify_listeners("UNLOCK", doc_id, username, "")
                # --------------------

            except Exception as e:
                print(f"[{self.node_id}] Error applying log: {e}")

    # NEW Helper method
    def _notify_listeners(self, op_type, doc_id, user, content):
        for callback in self.on_apply_callbacks:
            try:
                callback(op_type, doc_id, user, content)
            except Exception as e:
                print(f"Error in on_apply_callback: {e}")

    def _step_down(self, new_term):
        """MUST be called *while holding self.lock*."""
        if new_term > self.current_term:
            print(f"[{self.node_id}] Stepping down. Old Term: {self.current_term}, New Term: {new_term}")
            self.current_term = new_term
            self.state = "follower"
            self.voted_for = None
            self.leader_id = None

        self.last_heartbeat = time.time()

    # -------------------------------------------------
    # gRPC Servicer Methods (Called by peers)
    # -------------------------------------------------

    def RequestVote(self, request, context):
        with self.lock:
            if request.term > self.current_term:
                self._step_down(request.term)

            vote_granted = False
            if request.term == self.current_term and \
                    (self.voted_for is None or self.voted_for == request.candidate_id):

                # Use helper *within* the lock
                last_log_index, last_log_term = self._get_last_log_index_term()
                if request.last_log_term > last_log_term or \
                        (request.last_log_term == last_log_term and request.last_log_index >= last_log_index):
                    vote_granted = True
                    self.voted_for = request.candidate_id
                    self.last_heartkeybeat = time.time()  # Granting vote resets timer

            return service_pb2.RequestVoteReply(
                term=self.current_term,
                vote_granted=vote_granted
            )

    def AppendEntries(self, request, context):
        with self.lock:
            if request.term > self.current_term:
                self._step_down(request.term)

            success = False
            if request.term == self.current_term:
                self.state = "follower"
                self.leader_id = request.leader_id
                self.last_heartbeat = time.time()  # Valid heartbeat

                # Log consistency check
                if request.prev_log_index == -1 or \
                        (request.prev_log_index < len(self.log) and \
                         self.log[request.prev_log_index].term == request.prev_log_term):

                    success = True
                    self.log = self.log[:request.prev_log_index + 1]
                    self.log.extend(request.entries)

                    if request.leader_commit > self.commit_index:
                        self.commit_index = min(request.leader_commit, len(self.log) - 1)
                        self._apply_log_entries()

            return service_pb2.AppendEntriesReply(
                term=self.current_term,
                success=success,
                match_index=len(self.log) - 1
            )

    # -------------------------------------------------
    # Client-Facing Method
    # -------------------------------------------------

    def submit_command(self, command_str):
        with self.lock:
            if self.state != "leader":
                return False, self.leader_id

            log_entry = service_pb2.LogEntry(
                term=self.current_term,
                command=command_str
            )
            self.log.append(log_entry)
            print(f"[{self.node_id}] Leader received command: {command_str}")

            # Replicate this to peers *immediately* instead of waiting for heartbeat
            # This is an optimization for faster client response
            self.rpc_executor.submit(self._run_leader)

            return True, self.node_id