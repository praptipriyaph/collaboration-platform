import grpc
from concurrent import futures
import sys
import os
import threading
import uuid
import queue

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service_pb2
import service_pb2_grpc
from app_server.auth import AuthManager
from app_server.document_manager import DocumentManager
from app_server.raft_node import RaftNode


class CollaborationServicer(service_pb2_grpc.CollaborationServiceServicer):
    def __init__(self, llm_stub, raft_node, auth_manager):
        self.llm_stub=llm_stub
        self.raft_node=raft_node
        self.auth_manager=auth_manager

        self.subscriber_queues=[]
        self.subs_lock=threading.Lock()

        self.raft_node.on_apply_callbacks.append(self.on_raft_apply)

        print("Application Server initialized")

    def _get_not_leader_response(self, response_type):

        peers=list(self.raft_node.peer_ids) + [self.raft_node.node_id]
        peer_str=",".join(list(dict.fromkeys(peers)))

        leader_id=self.raft_node.leader_id
        msg=f"NOT_LEADER|leader_id={leader_id}|peers={peer_str}"

        if response_type=="Login":
            return service_pb2.LoginResponse(status=msg, token="")
        elif response_type=="Post":
            return service_pb2.StatusResponse(status="FAILURE", message=msg)

        return service_pb2.StatusResponse(status="FAILURE", message=msg)

    def on_raft_apply(self, op_type, doc_id, user, content):
        event=service_pb2.UpdateEvent(
            type=op_type,
            doc_id=doc_id,
            user=user,
            content=content
            )
        self._broadcast_event(event)

    def _broadcast_event(self, event):
        with self.subs_lock:
            for q in self.subscriber_queues:
                q.put(event)

    def Subscribe(self, request, context):
        print(f"New subscriber connected with token: {request.token[:8]}...")
        valid, username=self.auth_manager.validate_token(request.token)
        if not valid:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid token")
            return

        join_event=service_pb2.UpdateEvent(type="JOIN", user=username)
        self._broadcast_event(join_event)

        client_queue=queue.Queue()
        with self.subs_lock:
            self.subscriber_queues.append(client_queue)

        try:
            while context.is_active():
                try:
                    event=client_queue.get(timeout=1.0)
                    yield event
                except queue.Empty:
                    continue
        finally:
            with self.subs_lock:
                if client_queue in self.subscriber_queues:
                    self.subscriber_queues.remove(client_queue)

            print(f"Subscriber disconnected: {username}")
            leave_event=service_pb2.UpdateEvent(type="LEAVE", user=username)
            self._broadcast_event(leave_event)

    def Login(self, request, context):
        if self.raft_node.state != "leader":
            return self._get_not_leader_response("Login")

        print(f"Login attempt: {request.username}")
        success, token = self.auth_manager.authenticate(request.username, request.password)

        if success:
            command_str = f"CREATE_SESSION|{token}|{request.username}"
            success, _ = self.raft_node.submit_command(command_str)

            if success:
                import time
                start_time = time.time()
                # Wait up to 2 seconds for the user to appear in the active list
                while time.time() - start_time < 2.0:
                    if request.username in self.raft_node.document_manager.active_users:
                        print(f"Login successful: {request.username}, session active.")
                        return service_pb2.LoginResponse(status="SUCCESS", token=token)
                    time.sleep(0.1)

                return service_pb2.LoginResponse(status="SUCCESS", token=token)
            else:
                self.auth_manager.logout(token)
                return self._get_not_leader_response("Login")
        else:
            print(f"Login failed: {request.username}")
            return service_pb2.LoginResponse(status="FAILURE", message="Invalid credentials", token="")

    def Logout(self, request, context):
        if self.raft_node.state!="leader":
            return self._get_not_leader_response("Post")

        valid, username=self.auth_manager.validate_token(request.token)
        if valid:
            command_str=f"DELETE_SESSION|{request.token}"
            self.raft_node.submit_command(command_str)

            return service_pb2.StatusResponse(status="SUCCESS",message="Logged out request submitted")
        else:
            return service_pb2.StatusResponse(status="FAILURE",message="Invalid token")

    def Post(self, request, context):
        print(f"Post request: type={request.type}")
        valid, username=self.auth_manager.validate_token(request.token)
        if not valid:
            return service_pb2.StatusResponse(status="FAILURE", message="Invalid token")

        command_str = None
        if request.type=="document":
            parts = request.data.split("|", 1)
            if len(parts)==2:
                doc_id, content=parts
                command_str=f"CREATE|{doc_id}|{username}|{content}"
            else:
                return service_pb2.StatusResponse(status="FAILURE", message="Invalid document data format")
        elif request.type=="update":
            parts = request.data.split("|", 1)
            if len(parts)==2:
                doc_id, content=parts
                command_str=f"UPDATE|{doc_id}|{content}|{username}"

        elif request.type == "editing":
            doc_id = request.data
            command_str = f"EDITING|{doc_id}|{username}"

        elif request.type=="lock":
            doc_id=request.data
            command_str=f"LOCK|{doc_id}|{username}"

        elif request.type=="unlock":
            doc_id=request.data
            command_str=f"UNLOCK|{doc_id}|{username}"

        elif request.type=="add_node":
            new_node_id=request.data
            print(f"[{self.raft_node.node_id}] Received admin request to add node: {new_node_id}")
            command_str=f"ADD_NODE|{new_node_id}"

        if not command_str:
            return service_pb2.StatusResponse(status="FAILURE", message="Invalid request")

        success, leader_id=self.raft_node.submit_command(command_str)

        if success:
            return service_pb2.StatusResponse(status="SUCCESS", message="Request submitted")
        else:
            return self._get_not_leader_response("Post")

    def Get(self, request, context):
        print(f"Get request: type={request.type}, params={request.params}")
        valid, username=self.auth_manager.validate_token(request.token)
        if not valid:
            print(f"Get request failed: Invalid token for {request.type}")  # Added logging
            return service_pb2.GetResponse(status="FAILURE", items=[])

        doc_manager=self.raft_node.document_manager

        if request.type=="document_content":
             doc_id=request.params
             doc=doc_manager.get_document(doc_id)
             if doc:
                 return service_pb2.GetResponse(status="SUCCESS", items=[service_pb2.DataItem(id=doc_id, data=doc["content"])])
             else:
                 return service_pb2.GetResponse(status="FAILURE", items=[service_pb2.DataItem(id="error", data="Document not found")])

        elif request.type=="documents":
            docs=doc_manager.get_all_documents()
            items=[
                service_pb2.DataItem(
                    id=doc["id"],
                    data=doc["display_data"]
                ) for doc in docs
            ]
            return service_pb2.GetResponse(status="SUCCESS", items=items)

        elif request.type=="active_users":
            users=doc_manager.get_active_users()
            print(f"Returning active users: {users}")  # Added logging
            items=[service_pb2.DataItem(id=str(i), data=user) for i, user in enumerate(users)]
            return service_pb2.GetResponse(status="SUCCESS", items=items)

        elif request.type=="llm_query":
            try:
                llm_context=request.context if request.context else "General collaboration system query."

                llm_response=self.llm_stub.GetLLMAnswer(
                    service_pb2.LLMRequest(
                        request_id=str(uuid.uuid4()),
                        query=request.params,
                        context=llm_context
                    )
                )
                return service_pb2.GetResponse(status="SUCCESS",items=[service_pb2.DataItem(id="llm", data=llm_response.answer)])
            except Exception as e:
                return service_pb2.GetResponse(status="FAILURE",items=[service_pb2.DataItem(id="error", data=str(e))])

        elif request.type=="document_history":
            doc_id=request.params
            history=doc_manager.get_document_history(doc_id)
            items=[]
            for i, version_data in enumerate(history):
                items.append(service_pb2.DataItem(
                    id=str(i+1), 
                    data=f"V{i+1} ({version_data['timestamp']}) by {version_data['author']}: {version_data['content']}"
                ))
            return service_pb2.GetResponse(status="SUCCESS", items=items)

        return service_pb2.GetResponse(status="FAILURE", items=[])

def serve(node_id, peer_ids):
    llm_channel=grpc.insecure_channel('localhost:50052')
    llm_stub=service_pb2_grpc.LLMServiceStub(llm_channel)
    doc_manager=DocumentManager()
    auth_manager=AuthManager()
    raft_node=RaftNode(node_id, peer_ids, doc_manager, auth_manager)
    collab_servicer=CollaborationServicer(llm_stub, raft_node, auth_manager)
    server=grpc.server(futures.ThreadPoolExecutor(max_workers=50))
    service_pb2_grpc.add_CollaborationServiceServicer_to_server(collab_servicer, server)
    service_pb2_grpc.add_RaftServiceServicer_to_server(raft_node, server)

    port=node_id.split(':')[1]
    server.add_insecure_port(f'[::]:{port}')
    server.start()
    print(f"Application Server (Node {node_id}) started on port {port}")

    raft_thread=threading.Thread(target=raft_node.run, daemon=True)
    raft_thread.start()
    server.wait_for_termination()

