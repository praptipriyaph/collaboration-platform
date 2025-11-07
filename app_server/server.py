import grpc
from concurrent import futures
import sys
import os
import threading  # Added
import uuid

# --- Add project root ---
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service_pb2
import service_pb2_grpc
from app_server.auth import AuthManager
from app_server.document_manager import DocumentManager
from app_server.raft_node import RaftNode  # Added


class CollaborationServicer(service_pb2_grpc.CollaborationServiceServicer):

    # Modified __init__
    def __init__(self, llm_stub, raft_node, auth_manager):
        self.llm_stub = llm_stub
        self.raft_node = raft_node  # The Raft node
        self.auth_manager = auth_manager  # AuthManager is stateless, can be shared
        # The doc_manager is now OWNED by the raft_node.
        # Reads will come from raft_node.document_manager
        print("Application Server initialized")

    def Login(self, request, context):
        print(f"Login attempt: {request.username}")
        success, token = self.auth_manager.authenticate(request.username, request.password)

        if success:
            # We can still add/remove active users directly,
            # as this is non-replicated "presence" state.
            self.raft_node.document_manager.add_active_user(request.username)
            print(f"Login successful: {request.username}")
            return service_pb2.LoginResponse(
                status="SUCCESS",
                token=token
            )
        else:
            print(f"Login failed: {request.username}")
            return service_pb2.LoginResponse(
                status="FAILURE",
                token=""
            )

    def Logout(self, request, context):
        print(f"Logout request with token: {request.token[:8]}...")
        valid, username = self.auth_manager.validate_token(request.token)

        if valid:
            self.auth_manager.logout(request.token)
            # Remove from non-replicated presence set
            self.raft_node.document_manager.remove_active_user(username)
            print(f"Logout successful: {username}")
            return service_pb2.StatusResponse(
                status="SUCCESS",
                message="Logged out successfully"
            )
        else:
            return service_pb2.StatusResponse(
                status="FAILURE",
                message="Invalid token"
            )

    # -------------------------------------------------
    # --- CRITICAL CHANGE: Post() ---
    # -------------------------------------------------
    def Post(self, request, context):
        print(f"Post request: type={request.type}")
        valid, username = self.auth_manager.validate_token(request.token)

        if not valid:
            return service_pb2.StatusResponse(
                status="FAILURE",
                message="Invalid token"
            )

        command_str = None
        if request.type == "document":
            # "CREATE|username|content"
            command_str = f"CREATE|{username}|{request.data}"

        elif request.type == "update":
            parts = request.data.split("|", 1)
            if len(parts) == 2:
                doc_id, content = parts
                # "UPDATE|doc_id|content|username"
                command_str = f"UPDATE|{doc_id}|{content}|{username}"

        if not command_str:
            return service_pb2.StatusResponse(
                status="FAILURE",
                message="Invalid request format"
            )

        # --- Delegate WRITE to Raft ---
        success, leader_id = self.raft_node.submit_command(command_str)

        if success:
            # The command was accepted by the leader
            # It will be replicated and applied eventually
            return service_pb2.StatusResponse(
                status="SUCCESS",
                message="Request submitted"
            )
        else:
            # We are not the leader. Redirect client.
            return service_pb2.StatusResponse(
                status="FAILURE",
                message=f"Not leader. Try connecting to: {leader_id}"
            )

    # -------------------------------------------------
    # --- Get() reads from the Raft-controlled state machine ---
    # -------------------------------------------------
    def Get(self, request, context):
        print(f"Get request: type={request.type}")
        valid, username = self.auth_manager.validate_token(request.token)

        if not valid:
            return service_pb2.GetResponse(
                status="FAILURE",
                items=[]
            )

        # Get the DocumentManager from the Raft node
        doc_manager = self.raft_node.document_manager

        if request.type == "documents":
            # This is a STALE READ. It reads this node's *current* state.
            # For strong consistency, this request would also need to
            # go through the Raft leader (more complex).
            docs = doc_manager.get_all_documents()
            items = [
                service_pb2.DataItem(
                    id=doc["id"],
                    data=f"{doc['content']} | Author: {doc['author']} | Version: {doc['version']}"
                )
                for doc in docs
            ]
            return service_pb2.GetResponse(status="SUCCESS", items=items)

        elif request.type == "active_users":
            # Presence is non-replicated, so we just read it
            users = doc_manager.get_active_users()
            items = [
                service_pb2.DataItem(id=str(i), data=user)
                for i, user in enumerate(users)
            ]
            return service_pb2.GetResponse(status="SUCCESS", items=items)

        elif request.type == "llm_query":
            # LLM query does not change state, so we can call it directly
            try:
                llm_response = self.llm_stub.GetLLMAnswer(
                    service_pb2.LLMRequest(
                        request_id=str(uuid.uuid4()),
                        query=request.params,
                        context="Document collaboration system"
                    )
                )
                items = [service_pb2.DataItem(id="llm_response", data=llm_response.answer)]
                return service_pb2.GetResponse(status="SUCCESS", items=items)
            except Exception as e:
                print(f"LLM query error: {e}")
                return service_pb2.GetResponse(
                    status="FAILURE",
                    items=[service_pb2.DataItem(id="error", data=str(e))]
                )

        return service_pb2.GetResponse(status="FAILURE", items=[])


# --- Modified serve() function ---
def serve(node_id, peer_ids):
    print(f"Starting server for node {node_id}...")

    # 1. Connect to LLM server
    llm_channel = grpc.insecure_channel('localhost:50052')
    llm_stub = service_pb2_grpc.LLMServiceStub(llm_channel)

    # 2. Create the (shared) state machine and managers
    doc_manager = DocumentManager()
    auth_manager = AuthManager()

    # 3. Create the Raft Node
    # The RaftNode *owns* the DocumentManager
    raft_node = RaftNode(node_id, peer_ids, doc_manager)

    # 4. Create the Collaboration Servicer, giving it the RaftNode
    collab_servicer = CollaborationServicer(llm_stub, raft_node, auth_manager)

    # 5. Create the main gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))

    # 6. Add *both* services to the server
    service_pb2_grpc.add_CollaborationServiceServicer_to_server(
        collab_servicer, server
    )
    service_pb2_grpc.add_RaftServiceServicer_to_server(
        raft_node, server
    )

    # 7. Start the server on this node's port
    port = node_id.split(':')[1]
    server.add_insecure_port(f'[::]:{port}')
    server.start()
    print(f"Application Server (Node {node_id}) started on port {port}")

    # 8. Start the Raft node's main loop in a background thread
    raft_thread = threading.Thread(target=raft_node.run, daemon=True)
    raft_thread.start()
    print(f"RaftNode thread started for {node_id}")

    # 9. Wait for server termination
    server.wait_for_termination()

# This file is now run via run_app_server.py
# if __name__ == '__main__':
#     # This part will be handled by run_app_server.py
#     pass