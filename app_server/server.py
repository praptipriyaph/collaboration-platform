import grpc
from concurrent import futures
import sys
import os
import threading
import uuid
import queue  # NEW: For handling stream queues

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service_pb2
import service_pb2_grpc
from app_server.auth import AuthManager
from app_server.document_manager import DocumentManager
from app_server.raft_node import RaftNode


class CollaborationServicer(service_pb2_grpc.CollaborationServiceServicer):
    def __init__(self, llm_stub, raft_node, auth_manager):
        self.llm_stub = llm_stub
        self.raft_node = raft_node
        self.auth_manager = auth_manager

        # NEW: Manage active subscription queues
        self.subscriber_queues = []
        self.subs_lock = threading.Lock()

        # Register our callback with the Raft node
        self.raft_node.on_apply_callbacks.append(self.on_raft_apply)

        print("Application Server initialized")

        # Callback triggered by RaftNode when a log entry is committed & applied
    def on_raft_apply(self, op_type, doc_id, user, content):
            # 1. Create the event object
        event = service_pb2.UpdateEvent(
            type=op_type,
            doc_id=doc_id,
            user=user,
            content=content
            )
            # 2. Use the helper to broadcast it
        self._broadcast_event(event)

        # HELPER: Broadcasts ANY event to all subscribers internally
    def _broadcast_event(self, event):
        with self.subs_lock:
            for q in self.subscriber_queues:
                q.put(event)

    # NEW: Implementation of the Subscribe RPC
    def Subscribe(self, request, context):
        print(f"New subscriber connected with token: {request.token[:8]}...")
        valid, username = self.auth_manager.validate_token(request.token)
        if not valid:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid token")
            return

        # 1. Notify others that this user joined
        join_event = service_pb2.UpdateEvent(type="JOIN", user=username)
        self._broadcast_event(join_event)

        client_queue = queue.Queue()
        with self.subs_lock:
            self.subscriber_queues.append(client_queue)

        try:
            while context.is_active():
                try:
                    event = client_queue.get(timeout=1.0)
                    yield event
                except queue.Empty:
                    continue
        finally:
            with self.subs_lock:
                if client_queue in self.subscriber_queues:
                    self.subscriber_queues.remove(client_queue)

            # 2. Notify others that this user left
            print(f"Subscriber disconnected: {username}")
            leave_event = service_pb2.UpdateEvent(type="LEAVE", user=username)
            self._broadcast_event(leave_event)

    def Login(self, request, context):
        # --- NEW FIX: Only Leader can issue tokens ---
        # If we are not the leader, pretend to be unavailable for login.
        # The smart client will automatically try the next node.
        if self.raft_node.state != "leader":
             context.abort(grpc.StatusCode.UNAVAILABLE, "Not leader")
             return service_pb2.LoginResponse()
        # ---------------------------------------------

        print(f"Login attempt: {request.username}")
        success, token = self.auth_manager.authenticate(request.username, request.password)
        if success:
            self.raft_node.document_manager.add_active_user(request.username)
            print(f"Login successful: {request.username}")
            return service_pb2.LoginResponse(status="SUCCESS", token=token)
        else:
            print(f"Login failed: {request.username}")
            return service_pb2.LoginResponse(status="FAILURE", token="")

    def Logout(self, request, context):
        valid, username = self.auth_manager.validate_token(request.token)
        if valid:
            self.auth_manager.logout(request.token)
            self.raft_node.document_manager.remove_active_user(username)
            return service_pb2.StatusResponse(status="SUCCESS", message="Logged out")
        else:
            return service_pb2.StatusResponse(status="FAILURE", message="Invalid token")

    def Post(self, request, context):
        print(f"Post request: type={request.type}")
        valid, username = self.auth_manager.validate_token(request.token)
        if not valid:
            return service_pb2.StatusResponse(status="FAILURE", message="Invalid token")

        command_str = None
        if request.type == "document":
            doc_id = str(uuid.uuid4())
            command_str = f"CREATE|{doc_id}|{username}|{request.data}"

        elif request.type == "update":
            parts = request.data.split("|", 1)
            if len(parts) == 2:
                doc_id, content = parts
                command_str = f"UPDATE|{doc_id}|{content}|{username}"

        # --- NEW REQUEST TYPES ---
        elif request.type == "lock":
            # request.data should be the doc_id
            doc_id = request.data
            command_str = f"LOCK|{doc_id}|{username}"

        elif request.type == "unlock":
            doc_id = request.data
            command_str = f"UNLOCK|{doc_id}|{username}"
        # -------------------------

        if not command_str:
            return service_pb2.StatusResponse(status="FAILURE", message="Invalid request")

        success, leader_id = self.raft_node.submit_command(command_str)

        if success:
            return service_pb2.StatusResponse(status="SUCCESS", message="Request submitted")
        else:
            return service_pb2.StatusResponse(status="FAILURE", message=f"Not leader. Try connecting to: {leader_id}")
    def Get(self, request, context):
        # (Get implementation remains largely the same, just ensure it uses self.raft_node.document_manager)
        valid, username = self.auth_manager.validate_token(request.token)
        if not valid:
            return service_pb2.GetResponse(status="FAILURE", items=[])

        doc_manager = self.raft_node.document_manager

        if request.type == "documents":
            docs = doc_manager.get_all_documents()
            items = [
                service_pb2.DataItem(
                    id=doc["id"],
                    data=f"{doc['content']} | Author: {doc['author']} | Version: {doc['version']}"
                ) for doc in docs
            ]
            return service_pb2.GetResponse(status="SUCCESS", items=items)
        elif request.type == "active_users":
            users = doc_manager.get_active_users()
            items = [service_pb2.DataItem(id=str(i), data=user) for i, user in enumerate(users)]
            return service_pb2.GetResponse(status="SUCCESS", items=items)
        elif request.type == "llm_query":
            try:
                llm_response = self.llm_stub.GetLLMAnswer(
                    service_pb2.LLMRequest(request_id=str(uuid.uuid4()), query=request.params, context="Doc system")
                )
                return service_pb2.GetResponse(status="SUCCESS",
                                               items=[service_pb2.DataItem(id="llm", data=llm_response.answer)])
            except Exception as e:
                return service_pb2.GetResponse(status="FAILURE", items=[service_pb2.DataItem(id="error", data=str(e))])

        return service_pb2.GetResponse(status="FAILURE", items=[])


def serve(node_id, peer_ids):
    # (Same as before, just ensure max_workers is high enough for many subscribers)
    llm_channel = grpc.insecure_channel('localhost:50052')
    llm_stub = service_pb2_grpc.LLMServiceStub(llm_channel)
    doc_manager = DocumentManager()
    auth_manager = AuthManager()
    raft_node = RaftNode(node_id, peer_ids, doc_manager)
    collab_servicer = CollaborationServicer(llm_stub, raft_node, auth_manager)

    # Increased max_workers to handle blocking Subscribe calls
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=50))
    service_pb2_grpc.add_CollaborationServiceServicer_to_server(collab_servicer, server)
    service_pb2_grpc.add_RaftServiceServicer_to_server(raft_node, server)

    port = node_id.split(':')[1]
    server.add_insecure_port(f'[::]:{port}')
    server.start()
    print(f"Application Server (Node {node_id}) started on port {port}")

    raft_thread = threading.Thread(target=raft_node.run, daemon=True)
    raft_thread.start()
    server.wait_for_termination()